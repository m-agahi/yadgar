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
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from yadgar.ops import ServiceController, detect_service_mode
from yadgar.vacuum.phases import (
    _dir_bytes,
    _run_cleanup_script,
    _vacuum_export,
    _vacuum_snapshot_and_drop,
)
from yadgar.vacuum.strip import strip_action_log

__all__ = [
    "cmd_vacuum_impl",
    "strip_action_log",
    "_run_cleanup_script",
    "ServiceController",
    "_wait_for_health",
    "_wait_for_yadgar_health",
    "_log_consolidation_row",
]


# ---------------------------------------------------------------------------
# HTTP helpers (patched by tests via yadgar.vacuum._wait_for_health etc.)
# ---------------------------------------------------------------------------


def _build_http_client(backend_url: str) -> httpx.Client:
    """Build an httpx.Client with SurrealDB root credentials.

    Credential precedence (vacuum is an admin operation, needs root IAM):
      1. SURREAL_USER / SURREAL_PASS  (preferred — same creds used by entrypoint)
      2. YADGAR_DB_USER / YADGAR_DB_PASS  (backward compat)
      3. root / root  (built-in SurrealDB default)
    """
    import base64

    if os.environ.get("SURREAL_USER"):
        user = os.environ["SURREAL_USER"]
        password = os.environ.get("SURREAL_PASS", "root")
    elif os.environ.get("YADGAR_DB_USER"):
        user = os.environ["YADGAR_DB_USER"]
        password = os.environ.get("YADGAR_DB_PASS", "root")
    else:
        user = "root"
        password = "root"
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
# Phase 3: restart + reimport (calls _wait_for_health — must live here)
# ---------------------------------------------------------------------------


def _vacuum_restart_and_import(
    backend_url: str,
    filtered_path: Path,
    db_path: Path,
    bloated_path: Path,
    svc: ServiceController,
) -> bool:
    """Phase 3: rename live DB to .bloated, start backend, POST /import.

    Safe-swap pattern:
      1. Rename surreal_db → .bloated-<ts>  (backend will start on empty dir).
      2. Start backend; wait for /health.
      3. POST /import.
      4. If /import OK → return True.
      5. If /import fails → stop backend, rename .bloated-<ts> → surreal_db
         (atomic restore), restart backend, return False.

    Returns:
        True on success, False on failure (original DB restored).
    """
    # Step 1: rename live DB so backend starts on an empty directory
    print(f"[vacuum] phase 3: renaming {db_path} → {bloated_path} ...", flush=True)
    db_path.rename(bloated_path)

    # Step 2: start backend
    print("[vacuum] phase 3: starting yadgar-backend ...", flush=True)
    svc.start_backend()

    print(f"[vacuum] waiting for {backend_url}/health ...", flush=True)
    if not _wait_for_health(backend_url, timeout_s=120.0):
        print(
            "[vacuum] ERROR: yadgar-backend did not become healthy after 120 s.\n"
            "Attempting DB restore ...",
            file=sys.stderr,
        )
        _restore_db(bloated_path, db_path, svc, backend_url)
        return False

    # Step 2b: Bootstrap the namespace/database on the fresh empty DB.
    # SurrealDB's /import endpoint requires the target namespace to exist —
    # it does NOT auto-create it from the headers.  The export does not include
    # DEFINE NAMESPACE / DEFINE DATABASE, so we must create them here.
    print("[vacuum] bootstrapping namespace 'yadgar' on fresh DB ...", flush=True)
    try:
        ns_client = _build_http_client(backend_url)
        # Use root-level headers (no ns/db) so DEFINE NAMESPACE succeeds.
        ns_resp = ns_client.post(
            "/sql",
            content="DEFINE NAMESPACE IF NOT EXISTS yadgar; USE NS yadgar; DEFINE DATABASE IF NOT EXISTS main;",
            headers={"Content-Type": "text/plain"},
        )
        ns_client.close()
        if ns_resp.status_code != 200:
            print(
                f"[vacuum] WARNING: namespace bootstrap returned HTTP {ns_resp.status_code}: "
                f"{ns_resp.text[:200]}",
                file=sys.stderr,
            )
    except Exception as exc:
        print(f"[vacuum] WARNING: namespace bootstrap failed: {exc}", file=sys.stderr)

    # Step 3: POST /import
    surql_content = filtered_path.read_bytes()
    print(
        f"[vacuum] POST {backend_url}/import ({len(surql_content):,} bytes) ...",
        flush=True,
    )

    # /import requires root IAM — use admin credentials
    client = _build_http_client(backend_url)
    import_headers = {
        "Content-Type": "text/plain",
        **dict(client.headers),
    }
    client.close()

    resp = httpx.post(
        f"{backend_url}/import",
        content=surql_content,
        headers=import_headers,
        timeout=300.0,
    )

    if resp.status_code != 200:
        print(
            f"[vacuum] ERROR: /import returned HTTP {resp.status_code}:\n"
            f"{resp.text[:1000]}\n\n"
            "Attempting DB restore ...",
            file=sys.stderr,
        )
        _restore_db(bloated_path, db_path, svc, backend_url)
        return False

    print("[vacuum] import successful.", flush=True)
    return True


def _restore_db(
    bloated_path: Path,
    db_path: Path,
    svc: ServiceController,
    backend_url: str,
) -> None:
    """Stop backend, rename .bloated back to surreal_db, restart backend.

    Called on /import failure to leave yadgar running against the original DB.
    Errors here are logged but not re-raised — caller already returns non-zero.
    """
    try:
        print(
            f"[vacuum] restore: stopping backend and renaming {bloated_path} → {db_path} ...",
            file=sys.stderr,
        )
        svc.stop_backend()
        # SurrealDB may have created a fresh empty db_path on startup before
        # /import ran.  Remove it so we can rename .bloated back atomically.
        if db_path.exists():
            shutil.rmtree(str(db_path))
        bloated_path.rename(db_path)
        svc.start_backend()
        print("[vacuum] restore: backend restarted on original DB.", file=sys.stderr)
    except Exception as exc:
        print(
            f"[vacuum] CRITICAL: restore failed: {exc}\n"
            f"Manual recovery needed: rename {bloated_path} → {db_path}",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# Finalize (calls _wait_for_yadgar_health — must live here)
# ---------------------------------------------------------------------------


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
    import_ok = _vacuum_restart_and_import(backend_url, filtered_path, db_path, bloated_path, svc)
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
