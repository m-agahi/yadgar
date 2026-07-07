#!/usr/bin/env python3
"""v5.10.2 — Backfill secret scan: scan existing DB memories + wiki pages for leaked secrets.

Reads every memory and wiki page from storage, runs check_secrets() on content/tags/reason,
reports hits. NEVER mutates DB — read-only at all times.

Exit codes:
  0  no secrets found
  1  one or more secret patterns detected (see report)
  2  fatal error (DB unreachable, bad args, etc.)

Report written to:
  $YADGAR_SCAN_REPORT_DIR/secret-leak-scan-<TS>.txt   (if env set)
  ~/.yadgar/secret-leak-scan-<TS>.txt                  (default)

Usage:
  python scripts/scan_db_for_secrets.py [options]

Options:
  --dry-run             Read-only scan (default; included for explicitness)
  --storage-mock        Use built-in mock data instead of real DB (clean, exits 0)
  --storage-mock-leak   Like --storage-mock but with known secret (exits 1)
  --report-dir PATH     Override report output directory
  --limit N             Max rows to scan per table (default: unlimited)
  --quiet               Suppress stdout progress; only write report file
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# v5.10.3: suppress OTLP exporter BEFORE any yadgar import.
#
# yadgar/server/_app.py calls setup_tracing("yadgar-core") at module import
# time. setup_tracing reads Settings via get_settings() (lru_cache) which
# pulls YADGAR_OTLP_ENDPOINT from ~/.yadgar/config.yaml. In production that
# endpoint (http://host.containers.internal:4318) doesn't resolve on the host,
# so BatchSpanProcessor hangs at exit retrying failed exports (~10 s backoff),
# pushing the HITS/Clean output line past `| tail -N` and blocking the process.
#
# Setting env before any yadgar import makes pydantic-settings prefer env over
# yaml, disabling OTLP for this scan process only.
os.environ.setdefault("YADGAR_OTLP_ENDPOINT", "")

# ---------------------------------------------------------------------------
# Import secrets checker (must be on sys.path when invoked from repo root)
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from yadgar._shared.secrets import check_secrets  # noqa: E402

# ---------------------------------------------------------------------------
# Mock data for --storage-mock mode (safe dummy rows, no real secrets)
# ---------------------------------------------------------------------------
_MOCK_MEMORIES: list[dict[str, Any]] = [
    {
        "id": "mock:mem:1",
        "content": "This is a safe memory with no secrets.",
        "tags": ["test", "safe"],
        "reason": "unit test fixture",
    },
    {
        "id": "mock:mem:2",
        "content": "Another safe entry.",
        "tags": ["test"],
        "reason": "",
    },
]

_MOCK_WIKI: list[dict[str, Any]] = [
    {
        "id": "mock:wiki:1",
        "slug": "test-page",
        "content": "Safe wiki content for testing.",
        "tags": ["test"],
    },
]

# Mock data WITH a known-bad secret for --storage-mock-leak (tests exit-1 path).
# Uses a synthetic ghp_ token long enough to trip the {20,} threshold.
_MOCK_MEMORIES_WITH_LEAK: list[dict[str, Any]] = [
    {
        "id": "mock:mem:leak:1",
        "content": "ghp_TESTTOKEN1234567890abcdefghijkl slipped through old threshold",
        "tags": ["test"],
        "reason": "unit test fixture with leak",
    },
    {
        "id": "mock:mem:leak:2",
        "content": "Safe memory alongside the leaked one.",
        "tags": ["test"],
        "reason": "",
    },
]


def _scan_rows(
    rows: list[dict[str, Any]],
    table: str,
    id_field: str = "id",
) -> list[dict[str, Any]]:
    """Scan a list of rows, returning hit records."""
    hits: list[dict[str, Any]] = []
    for row in rows:
        row_id = str(row.get(id_field, "<no-id>"))

        # Fields to scan
        fields_to_check = {
            "content": str(row.get("content", "") or ""),
            "tags": " ".join(str(t) for t in (row.get("tags") or [])),
            "reason": str(row.get("reason", "") or ""),
        }

        for field_name, value in fields_to_check.items():
            if not value.strip():
                continue
            blocked, reason, preview = check_secrets(value)
            if blocked:
                hits.append(
                    {
                        "table": table,
                        "id": row_id,
                        "field": field_name,
                        "reason": reason,
                        "preview": preview,
                    }
                )
                # Only report the first hit per row (avoid cascading dupes)
                break

    return hits


def _ensure_storage_initialized() -> None:
    """Initialize StorageEngine if not already (v5.10.3 fix).

    The scan script runs OUTSIDE the yadgar server process, so the singleton
    StorageEngine never gets bootstrapped via the normal lifespan.

    StorageEngine has two modes (yadgar/storage/__init__.py:178):
      - YADGAR_DB_URL set → HTTP mode (talks to running yadgar-backend)
      - YADGAR_DB_URL unset → embedded mode (opens SurrealKV file directly)

    Embedded mode CONFLICTS with the running yadgar-backend container which
    holds an exclusive lock on the SurrealKV file. The scan script MUST use
    HTTP mode against the live backend.

    Defaults: YADGAR_DB_URL=http://127.0.0.1:8000, YADGAR_ALLOW_ROOT=1.
    Operator can override via env before invoking the script.
    """
    from yadgar._shared.runtime import state as _st

    if _st._storage is not None:
        return  # already initialized

    # Force HTTP mode if operator didn't set env explicitly.
    os.environ.setdefault("YADGAR_DB_URL", "http://127.0.0.1:8000")

    # Auto-source ~/.config/yadgar/secrets.env if creds not in env.
    # The nix yadgar.service uses EnvironmentFile= to load these; scan
    # script runs outside systemd so must read manually.
    if not os.environ.get("YADGAR_DB_USER") or not os.environ.get("YADGAR_DB_PASS"):
        _secrets_env = Path.home() / ".config" / "yadgar" / "secrets.env"
        if _secrets_env.exists():
            for _line in _secrets_env.read_text(encoding="utf-8").splitlines():
                _line = _line.strip()
                if not _line or _line.startswith("#") or "=" not in _line:
                    continue
                _k, _v = _line.split("=", 1)
                _k = _k.strip()
                _v = _v.strip().strip('"').strip("'")
                if _k.startswith("YADGAR_"):
                    os.environ.setdefault(_k, _v)

    # Last-resort fallback: allow root creds (read-only SELECT only).
    if not os.environ.get("YADGAR_DB_USER") or not os.environ.get("YADGAR_DB_PASS"):
        os.environ.setdefault("YADGAR_ALLOW_ROOT", "1")

    from yadgar._shared.runtime.lifecycle import init_engines  # noqa: PLC0415

    init_engines()


def _fetch_memories_real(limit: int | None) -> list[dict[str, Any]]:
    """Fetch memory rows from real storage. Returns list of row dicts."""
    try:
        _ensure_storage_initialized()
        from yadgar._shared.runtime.lifecycle import _get_storage  # noqa: PLC0415

        storage = _get_storage()
        # ORDER BY id DESC: scan newest rows first.
        # Leaked secrets are typically recent; this ensures --limit N covers
        # them without requiring a full table scan (e.g. mem 519107 is at
        # position 2994/3147, would never appear with --limit 200 ASC).
        sql = "SELECT id, content, tags, reason FROM memory ORDER BY id DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        rows = storage._q(sql, {})
        return rows or []
    except Exception as exc:
        raise RuntimeError(f"Failed to query memory table: {exc}") from exc


def _fetch_wiki_real(limit: int | None) -> list[dict[str, Any]]:
    """Fetch wiki page rows from real storage."""
    try:
        _ensure_storage_initialized()
        from yadgar._shared.runtime.lifecycle import _get_storage  # noqa: PLC0415

        storage = _get_storage()
        # ORDER BY id DESC — same rationale as memory query.
        sql = "SELECT id, slug, content, tags FROM wiki_page ORDER BY id DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        rows = storage._q(sql, {})
        return rows or []
    except Exception as exc:
        raise RuntimeError(f"Failed to query wiki_page table: {exc}") from exc


def _write_report(hits: list[dict[str, Any]], report_path: Path, dry_run: bool) -> None:
    """Write scan report to file."""
    ts = datetime.datetime.now(datetime.UTC).isoformat()
    lines = [
        f"# Yadgar Secret-Leak Backfill Scan — {ts}",
        f"# Mode: {'dry-run (read-only)' if dry_run else 'READ-ONLY SCAN'}",
        f"# Hits: {len(hits)}",
        "",
    ]
    if hits:
        lines.append("## Detected secrets:")
        for h in hits:
            lines.append(
                f"  table={h['table']} id={h['id']} field={h['field']} "
                f"reason={h['reason']!r} preview={h['preview']!r}"
            )
    else:
        lines.append("## No secrets detected.")

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """Run backfill secret scan. Returns 0=clean, 1=hits, 2=error."""
    parser = argparse.ArgumentParser(
        description="Backfill scan: check existing DB rows for leaked secrets (read-only)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Read-only scan (default: always on; flag is explicit/documentation only)",
    )
    parser.add_argument(
        "--storage-mock",
        action="store_true",
        help="Use built-in mock data instead of real DB (for tests/CI — clean data, exits 0)",
    )
    parser.add_argument(
        "--storage-mock-leak",
        action="store_true",
        help=(
            "Like --storage-mock but data contains a known secret "
            "(for tests/CI — exercises exit-1 path)"
        ),
    )
    parser.add_argument(
        "--report-dir",
        default=None,
        help="Directory to write report file (default: $YADGAR_SCAN_REPORT_DIR or ~/.yadgar/)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max rows to scan per table (default: unlimited)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress stdout progress; only write report file",
    )
    args = parser.parse_args(argv)

    # Determine report directory
    if args.report_dir:
        report_dir = Path(args.report_dir)
    elif os.environ.get("YADGAR_SCAN_REPORT_DIR"):
        report_dir = Path(os.environ["YADGAR_SCAN_REPORT_DIR"])
    else:
        report_dir = Path.home() / ".yadgar"

    ts_str = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    report_path = report_dir / f"secret-leak-scan-{ts_str}.txt"

    if not args.quiet:
        print("Yadgar secret-leak backfill scan starting...")
        print(f"Report will be written to: {report_path}")
        if args.storage_mock:
            print("Mode: --storage-mock (using built-in test data, clean)")
        elif args.storage_mock_leak:
            print("Mode: --storage-mock-leak (built-in data with known secret, exits 1)")

    try:
        if args.storage_mock_leak:
            memories = _MOCK_MEMORIES_WITH_LEAK
            wiki_pages = _MOCK_WIKI
        elif args.storage_mock:
            memories = _MOCK_MEMORIES
            wiki_pages = _MOCK_WIKI
        else:
            if not args.quiet:
                print("Fetching memory rows...")
            memories = _fetch_memories_real(args.limit)
            if not args.quiet:
                print(f"  {len(memories)} memory rows fetched.")
                print("Fetching wiki_page rows...")
            wiki_pages = _fetch_wiki_real(args.limit)
            if not args.quiet:
                print(f"  {len(wiki_pages)} wiki page rows fetched.")

        hits: list[dict] = []
        hits.extend(_scan_rows(memories, table="memory"))
        hits.extend(_scan_rows(wiki_pages, table="wiki_page"))

        _write_report(hits, report_path, dry_run=True)

        if hits:
            if not args.quiet:
                print(f"\nWARNING: {len(hits)} row(s) with potential secret leaks detected.")
                print(f"See report: {report_path}")
            else:
                print(f"HITS: {len(hits)} — see {report_path}", file=sys.stderr)
            return 1
        else:
            if not args.quiet:
                print(f"\nClean — no secrets detected. Report: {report_path}")
            return 0

    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
