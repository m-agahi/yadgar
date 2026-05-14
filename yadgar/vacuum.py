"""yadgar vacuum — export → snapshot → swap → reimport.

Mirrors the verified manual procedure from 2026-05-12:

  1. Preflight: confirm surreal_db/ exists, backend reachable.
  2. Phase 1 — Export: GET /export, pipe through strip_action_log().
  3. Phase 2 — Snapshot + Drop: cp -r surreal_db pre-vacuum snapshot,
               stop daemons, mv surreal_db → surreal_db.bloated-<ts>.
  4. Phase 3 — Restart + Reimport: start yadgar-backend, wait for /health,
               POST /import, on success start yadgar + remove bloated dir.
  5. Report: log before/after bytes, duration, insert consolidation_log row.

Public entry point:
    cmd_vacuum_impl(args) -> int   (0 = success, non-zero = failure)

The top-level cmd_vacuum(args) in __main__.py delegates here.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from yadgar.ops import ServiceController, detect_service_mode

# ---------------------------------------------------------------------------
# Strip action_log from SurrealQL export
# ---------------------------------------------------------------------------

# Strip the action_log section from a SurrealDB v3.0.5 /export response.
#
# Real export format (observed on v3.0.5 / surrealkv):
#   -- ------------------------------
#   -- TABLE: action_log
#   -- ------------------------------
#
#   DEFINE TABLE action_log TYPE ANY SCHEMALESS PERMISSIONS NONE;
#
#
#
#   -- ------------------------------
#   -- TABLE DATA: action_log
#   -- ------------------------------
#
#   INSERT [ { ... }, { ... } ];
#
#   -- ------------------------------
#   -- TABLE: <next_table>
#
# The data rows are a single INSERT [...]; statement (JSON array, all rows).
# The raw shell-command text in tool_input_summary breaks the SurrealQL
# re-parser on import.  We strip both the TABLE: and TABLE DATA: blocks.

# Strip the TABLE: action_log block (schema definition + surrounding dashes)
_ACTION_LOG_SCHEMA_BLOCK_RE = re.compile(
    r"-- -{20,}\n"  # opening dashes line
    r"-- TABLE: action_log\n"  # TABLE comment
    r"-- -{20,}\n"  # closing dashes line
    r"\n?"  # optional blank line
    r".*?"  # schema content (DEFINE TABLE, indexes...)
    r"(?=\n-- -{20,}\n|\Z)",  # stop at next dashes block or EOF
    re.DOTALL,
)

# Strip the TABLE DATA: action_log block (data rows + surrounding dashes)
_ACTION_LOG_DATA_BLOCK_RE = re.compile(
    r"-- -{20,}\n"  # opening dashes line
    r"-- TABLE DATA: action_log\n"  # TABLE DATA comment
    r"-- -{20,}\n"  # closing dashes line
    r"\n?"  # optional blank line
    r".*?"  # INSERT [...]; statement(s)
    r"(?=\n-- -{20,}\n|\Z)",  # stop at next dashes block or EOF
    re.DOTALL,
)

# Fallback: match the single DEFINE TABLE action_log line (test fixtures
# may not use the full dashes format).
_ACTION_LOG_DEFINE_RE = re.compile(
    r"^DEFINE TABLE action_log\b[^\n]*;\s*\n?",
    re.MULTILINE,
)

# Fallback: match a bare TABLE DATA: action_log header (test fixtures)
_ACTION_LOG_DATA_BARE_RE = re.compile(
    r"-- TABLE DATA: action_log\b[^\n]*\n"  # bare header line
    r".*?"  # rows
    r"(?=\n-- TABLE DATA:|\Z)",  # stop at next TABLE DATA or EOF
    re.DOTALL,
)


def strip_action_log(surql: str) -> str:
    """Remove the action_log section from a SurrealDB /export response.

    Handles both the real SurrealDB v3.0.5 dashes-block format and the
    simpler per-line format used in test fixtures.

    The action_log rows contain raw shell-command text that breaks the
    SurrealQL re-parser on import. Both the TABLE: (schema) block and the
    TABLE DATA: (rows) block are stripped.

    Args:
        surql: Raw .surql export content from SurrealDB /export.

    Returns:
        Filtered content safe to POST to /import.
    """
    result = surql

    # -- Real SurrealDB v3.0.5 format: dashes-framed blocks --
    result = _ACTION_LOG_SCHEMA_BLOCK_RE.sub("", result)
    result = _ACTION_LOG_DATA_BLOCK_RE.sub("", result)

    # -- Fallback for test fixtures / older format --
    result = _ACTION_LOG_DEFINE_RE.sub("", result)
    result = _ACTION_LOG_DATA_BARE_RE.sub("", result)

    return result


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _build_http_client(backend_url: str) -> httpx.Client:
    """Build an httpx.Client with SurrealDB root credentials."""
    import base64

    user = os.environ.get("YADGAR_DB_USER", "root")
    password = os.environ.get("YADGAR_DB_PASS", "root")
    auth = base64.b64encode(f"{user}:{password}".encode()).decode()
    return httpx.Client(
        base_url=backend_url,
        headers={
            "Authorization": f"Basic {auth}",
            "surreal-ns": "yadgar",
            "surreal-db": "main",
            "Accept": "application/json",
        },
        timeout=120.0,
    )


def _wait_for_health(
    url: str,
    timeout_s: float = 120.0,
    poll_interval: float = 1.0,
) -> bool:
    """Poll GET <url>/health until 200 or timeout. Returns True on success."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{url}/health", timeout=2.0)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(poll_interval)
    return False


def _wait_for_yadgar_health(
    url: str,
    timeout_s: float = 60.0,
    poll_interval: float = 1.0,
) -> bool:
    """Poll GET <url>/healthz until 200 or timeout. Returns True on success."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{url}/healthz", timeout=2.0)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(poll_interval)
    return False


# ---------------------------------------------------------------------------
# Consolidation log row
# ---------------------------------------------------------------------------


def _log_consolidation_row(row: dict) -> None:
    """Insert a consolidation_log row via the SurrealDB SQL API.

    Non-fatal: if this fails we just warn. The vacuum itself succeeded.
    """
    backend_url = row.pop("_backend_url", "http://127.0.0.1:8080")
    try:
        client = _build_http_client(backend_url)
        stmt = (
            "INSERT INTO consolidation_log {"
            "kind: $kind,"
            "started_at: $started_at,"
            "finished_at: $finished_at,"
            "duration_seconds: $duration_seconds,"
            "before_bytes: $before_bytes,"
            "after_bytes: $after_bytes,"
            "saved_bytes: $saved_bytes,"
            "saved_pct: $saved_pct"
            "}"
        )
        client.post(
            "/sql",
            content=stmt,
            headers={"Content-Type": "text/plain"},
            params=row,
        )
        client.close()
    except Exception as exc:
        print(f"[vacuum] warning: could not insert consolidation_log row: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Dir-size helper
# ---------------------------------------------------------------------------


def _dir_bytes(path: Path) -> int:
    """Return total size of all files in path (recursive). Returns 0 if missing."""
    if not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


# ---------------------------------------------------------------------------
# Snapshot retention
# ---------------------------------------------------------------------------


def _run_cleanup_script(yadgar_home: Path, pattern: str, keep_n: int) -> None:
    """Run scripts/cleanup-backups.sh to prune old pre-vacuum snapshots.

    The script path is resolved from YADGAR_CLEANUP_SCRIPT env var (for
    tests) or the canonical scripts/ location next to the yadgar package.
    """
    import subprocess

    script = os.environ.get("YADGAR_CLEANUP_SCRIPT")
    if not script:
        # Find cleanup-backups.sh relative to this file's package root
        pkg_root = Path(__file__).parent.parent
        candidate = pkg_root / "scripts" / "cleanup-backups.sh"
        if candidate.exists():
            script = str(candidate)

    if not script or not Path(script).exists():
        print(
            "[vacuum] cleanup-backups.sh not found — skipping snapshot pruning",
            file=sys.stderr,
        )
        return

    try:
        subprocess.run(
            [script, str(yadgar_home), pattern, str(keep_n)],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode(errors="replace").strip()
        print(f"[vacuum] cleanup script returned non-zero: {stderr}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Phase helpers
# ---------------------------------------------------------------------------


def _vacuum_export(backend_url: str, yadgar_home: Path) -> tuple[Path, Path]:
    """Phase 1: GET /export, strip action_log, write .surql files.

    Returns:
        (raw_path, filtered_path)
    Raises:
        RuntimeError on non-200 response.
    """
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    raw_path = yadgar_home / f"vacuum_export_{ts}.surql"
    filtered_path = yadgar_home / f"vacuum_export_{ts}.filtered.surql"

    print(f"[vacuum] phase 1: GET {backend_url}/export ...", flush=True)
    resp = httpx.get(f"{backend_url}/export", timeout=300.0)
    if resp.status_code != 200:
        raise RuntimeError(f"Export failed: HTTP {resp.status_code}\n{resp.text[:500]}")

    raw_surql = resp.text
    raw_path.write_text(raw_surql, encoding="utf-8")
    print(f"[vacuum] export saved: {raw_path} ({len(raw_surql):,} bytes)", flush=True)

    filtered = strip_action_log(raw_surql)
    filtered_path.write_text(filtered, encoding="utf-8")
    print(f"[vacuum] filtered:     {filtered_path} ({len(filtered):,} bytes)", flush=True)

    return raw_path, filtered_path


def _vacuum_snapshot_and_drop(
    db_path: Path,
    yadgar_home: Path,
    svc: ServiceController,
    before_bytes: int,
) -> tuple[Path, Path]:
    """Phase 2: snapshot, stop daemons, mv to .bloated.

    Returns:
        (snapshot_path, bloated_path)
    """
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    snapshot_path = yadgar_home / f"surreal_db.pre-vacuum-{ts}"
    bloated_path = yadgar_home / f"surreal_db.bloated-{ts}"

    print(
        f"[vacuum] phase 2: snapshot {db_path} → {snapshot_path} "
        f"({before_bytes / 1024 / 1024:.0f} MB) ...",
        flush=True,
    )
    shutil.copytree(str(db_path), str(snapshot_path))

    print("[vacuum] stopping daemons ...", flush=True)
    svc.stop()

    print(f"[vacuum] renaming {db_path} → {bloated_path} ...", flush=True)
    db_path.rename(bloated_path)

    return snapshot_path, bloated_path


def _vacuum_restart_and_import(
    backend_url: str,
    filtered_path: Path,
    bloated_path: Path,
    svc: ServiceController,
) -> bool:
    """Phase 3: start backend, POST /import.

    Returns:
        True on success, False on failure (bloated dir must be kept).
    """
    print("[vacuum] phase 3: starting yadgar-backend ...", flush=True)
    svc.start_backend()

    print(f"[vacuum] waiting for {backend_url}/health ...", flush=True)
    if not _wait_for_health(backend_url, timeout_s=120.0):
        print(
            f"[vacuum] ERROR: yadgar-backend did not become healthy after 120 s.\n"
            f"Bloated dir retained: {bloated_path}\n"
            "Start yadgar-backend manually and re-run `yadgar vacuum`.",
            file=sys.stderr,
        )
        return False

    surql_content = filtered_path.read_bytes()
    print(
        f"[vacuum] POST {backend_url}/import ({len(surql_content):,} bytes) ...",
        flush=True,
    )
    resp = httpx.post(
        f"{backend_url}/import",
        content=surql_content,
        headers={"Content-Type": "text/plain"},
        timeout=300.0,
    )
    if resp.status_code != 200:
        print(
            f"[vacuum] ERROR: /import returned HTTP {resp.status_code}:\n"
            f"{resp.text[:1000]}\n\n"
            f"Bloated dir retained: {bloated_path}\n"
            "yadgar NOT started.",
            file=sys.stderr,
        )
        return False

    print("[vacuum] import successful.", flush=True)
    return True


def _vacuum_finalize(
    backend_url: str,
    yadgar_home: Path,
    bloated_path: Path,
    snapshot_path: Path,
    svc: ServiceController,
    keep_n: int = 3,
) -> bool:
    """Start yadgar, wait for health, run check_invariants, clean up bloated dir.

    Returns:
        True if all checks pass and cleanup succeeded.
    """
    yadgar_url = f"http://127.0.0.1:{os.environ.get('YADGAR_PORT', '8765')}"

    print("[vacuum] starting yadgar ...", flush=True)
    svc.start_yadgar()

    print(f"[vacuum] waiting for {yadgar_url}/healthz ...", flush=True)
    if not _wait_for_yadgar_health(yadgar_url, timeout_s=60.0):
        print(
            f"[vacuum] WARNING: yadgar did not become healthy. "
            f"Bloated dir retained: {bloated_path}",
            file=sys.stderr,
        )
        return False

    # Run check_invariants
    try:
        ci_resp = httpx.post(
            f"{yadgar_url}/api/check_invariants",
            timeout=120.0,
        )
        if ci_resp.status_code == 200 and ci_resp.json().get("ok"):
            print("[vacuum] check_invariants: ok", flush=True)
            # Safe to remove bloated dir
            print(f"[vacuum] removing bloated dir: {bloated_path}", flush=True)
            shutil.rmtree(str(bloated_path))
        else:
            body = ci_resp.text[:300] if ci_resp.status_code != 200 else str(ci_resp.json())
            print(
                f"[vacuum] WARNING: check_invariants returned non-ok: {body}\n"
                f"Bloated dir retained for rollback: {bloated_path}",
                file=sys.stderr,
            )
            return False
    except Exception as exc:
        print(
            f"[vacuum] WARNING: check_invariants failed: {exc}\n"
            f"Bloated dir retained for rollback: {bloated_path}",
            file=sys.stderr,
        )
        return False

    # Prune pre-vacuum snapshots
    _run_cleanup_script(yadgar_home, "surreal_db.pre-vacuum-*", keep_n)

    return True


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def cmd_vacuum_impl(args) -> int:  # type: ignore[no-untyped-def]
    """Implement the new vacuum flow. Returns exit code (0 = success).

    args attributes consumed:
      - backend_url (str)     default "http://127.0.0.1:8080"
      - service_mode (str)    "systemd" | "docker" | "manual" | None (auto-detect)
      - db_path (str | None)  override default ~/.yadgar/surreal_db
      - yes (bool)            skip confirmation prompt
    """
    from yadgar.config import Settings

    settings = Settings()
    started_at = time.monotonic()
    started_ts = datetime.now(UTC).isoformat()

    # -- Resolve paths --
    backend_url: str = (
        getattr(args, "backend_url", "http://127.0.0.1:8080") or "http://127.0.0.1:8080"
    )
    db_path_arg: str | None = getattr(args, "db_path", None)
    db_path = Path(db_path_arg).expanduser() if db_path_arg else Path(settings.DB_PATH).expanduser()
    yadgar_home = db_path.parent

    # -- Preflight --
    if not db_path.exists():
        print(
            f"[vacuum] ERROR: DB dir not found: {db_path}\n"
            "Is yadgar configured correctly? Check DB_PATH in config.",
            file=sys.stderr,
        )
        return 1

    # Confirm backend is alive
    try:
        r = httpx.get(f"{backend_url}/health", timeout=5.0)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
    except Exception as exc:
        print(
            f"[vacuum] ERROR: backend at {backend_url} is not reachable: {exc}\n"
            "Start yadgar-backend first, then run `yadgar vacuum`.",
            file=sys.stderr,
        )
        return 1

    before_bytes = _dir_bytes(db_path)
    print(f"[vacuum] DB size before: {before_bytes / 1024 / 1024:.1f} MB ({db_path})", flush=True)

    keep_n: int = getattr(settings, "VACUUM_SNAPSHOT_RETENTION", 3)

    # -- Service controller --
    mode = getattr(args, "service_mode", None) or detect_service_mode()
    svc = ServiceController(mode)

    # -- Phase 1: Export --
    try:
        _raw_path, filtered_path = _vacuum_export(backend_url, yadgar_home)
    except Exception as exc:
        print(f"[vacuum] ERROR in export phase: {exc}", file=sys.stderr)
        return 1

    # -- Phase 2: Snapshot + Drop --
    try:
        snapshot_path, bloated_path = _vacuum_snapshot_and_drop(
            db_path, yadgar_home, svc, before_bytes
        )
    except Exception as exc:
        print(f"[vacuum] ERROR in snapshot/drop phase: {exc}", file=sys.stderr)
        return 1

    # -- Phase 3: Restart + Reimport --
    import_ok = _vacuum_restart_and_import(backend_url, filtered_path, bloated_path, svc)
    if not import_ok:
        return 1

    # -- Finalize --
    after_bytes = _dir_bytes(db_path)
    saved_bytes = before_bytes - after_bytes
    saved_pct = int(100 * saved_bytes / before_bytes) if before_bytes else 0
    duration_s = round(time.monotonic() - started_at, 1)

    finalize_ok = _vacuum_finalize(
        backend_url, yadgar_home, bloated_path, snapshot_path, svc, keep_n
    )

    # -- Report --
    finished_ts = datetime.now(UTC).isoformat()
    print(
        f"\n[vacuum] complete.\n"
        f"  Before:   {before_bytes / 1024 / 1024:.1f} MB\n"
        f"  After:    {after_bytes / 1024 / 1024:.1f} MB\n"
        f"  Saved:    {saved_bytes / 1024 / 1024:.1f} MB ({saved_pct}%)\n"
        f"  Duration: {duration_s} s",
        flush=True,
    )

    # Log to consolidation_log (best-effort)
    _log_consolidation_row(
        {
            "_backend_url": backend_url,
            "kind": "vacuum",
            "started_at": started_ts,
            "finished_at": finished_ts,
            "duration_seconds": duration_s,
            "before_bytes": before_bytes,
            "after_bytes": after_bytes,
            "saved_bytes": saved_bytes,
            "saved_pct": saved_pct,
        }
    )

    return 0 if finalize_ok else 2  # 2 = succeeded but check_invariants warn
