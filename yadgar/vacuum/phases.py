"""Vacuum phase helpers: snapshot/drop, export, cleanup.

Functions here are not patched by tests — only _wait_for_health,
_wait_for_yadgar_health, _log_consolidation_row, and ServiceController
are patched (those live in __init__.py so patches intercept calls).
"""

from __future__ import annotations

import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

from yadgar.ops import ServiceController
from yadgar.vacuum.strip import strip_action_log


def _dir_bytes(path: Path) -> int:
    """Return total size of all files in path (recursive). Returns 0 if missing."""
    if not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _run_cleanup_script(yadgar_home: Path, pattern: str, keep_n: int) -> None:
    """Prune old pre-vacuum snapshots, keeping only the `keep_n` most recent.

    Inline Python implementation (C-3): replaces the broken subprocess call to
    cleanup-backups.sh, which only accepted --dry-run and rejected positional args.

    Files matching `pattern` inside `yadgar_home` are sorted by mtime (newest
    first); any beyond the first `keep_n` are deleted.
    """
    import glob

    glob_pattern = str(yadgar_home / pattern)
    candidates = sorted(glob.glob(glob_pattern), key=os.path.getmtime, reverse=True)
    to_delete = candidates[keep_n:]
    for path in to_delete:
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            print(f"[vacuum] pruned snapshot: {path}", file=sys.stderr)
        except OSError as exc:
            print(f"[vacuum] failed to prune {path}: {exc}", file=sys.stderr)


def _surreal_headers() -> dict[str, str]:
    """SurrealDB v2+ /export rejects with HTTP 400 'Specify a namespace' without
    these headers, and basic-auth without YADGAR_DB_USER/PASS. Module-level
    httpx.get keeps test monkeypatches working (cf. test_vacuum.py).
    """
    import base64

    user = os.environ.get("YADGAR_DB_USER", "root")
    password = os.environ.get("YADGAR_DB_PASS", "root")
    auth = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {
        "Authorization": f"Basic {auth}",
        "surreal-ns": "yadgar",
        "surreal-db": "main",
        "Accept": "application/json",
    }


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
    resp = httpx.get(
        f"{backend_url}/export",
        headers=_surreal_headers(),
        timeout=300.0,
    )
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
