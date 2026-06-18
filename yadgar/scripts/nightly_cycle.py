"""Nightly cycle script: backup → consolidation → vacuum → backup (v5.7.0 PR-1a).

Runs once and exits. systemd timer (PR-1b) handles scheduling.

Lifecycle (steps 1-7):
  1. Stop yadgar CORE only. Backend stays up to serve HTTP consolidation + backup.
  2. Pre-backup snapshot (label="nightly-pre") — logical HTTP export via backend_url
     (GET /export), taken while core is down and backend is still up.
  3. Consolidation — StorageEngine opens in SERVER mode (YADGAR_DB_URL is set;
     backend is up). ConsolidationScheduler.run_nightly_consolidation() runs via
     the live HTTP connection — no embedded file lock, no surrealkv format skew.
     Fixes BC-D1: eliminates the embedded SDK 2.0.0 vs server 3.0.5 format error.
  4. Vacuum — cmd_vacuum_impl manages the backend within step 4 (export/swap/import),
     restarts core, runs check_invariants.
  5. Post-backup snapshot (label="nightly-post") — another logical HTTP export.
     Backend stays up; no second stop needed.
  6. Prune old snapshots.
  7. Start yadgar CORE. Backend was never stopped, so only core needs starting.

Exit codes:
  0  — full success
  10 — stop core failed (FATAL — abort)
  20 — pre-backup failed (FATAL — abort)
  30 — consolidation failed (non-fatal — continue)
  40 — vacuum failed (non-fatal — continue)
  50 — post-backup failed (non-fatal — continue)
  60 — prune failed (non-fatal — continue)
  70 — final core restart failed

When multiple steps fail the FIRST failing step's exit code is returned.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import yadgar.paths as _paths
from yadgar.backup import create_snapshot, default_retention, prune_snapshots
from yadgar.config import Settings
from yadgar.consolidation import ConsolidationScheduler
from yadgar.embeddings import EmbeddingEngine
from yadgar.exception_telemetry import record_exception
from yadgar.log_config import configure_logging
from yadgar.storage import StorageEngine
from yadgar.vacuum import cmd_vacuum_impl

_log = logging.getLogger("yadgar.nightly_cycle")

_UNIT_CORE = "yadgar"
_UNIT_BACKEND = "yadgar-backend"

# Bounded retry/backoff around systemctl --user (v5.69 P5).  The host has a
# history of transient systemd/D-Bus flakiness (the 06-16 restore failed on a
# systemctl --user/D-Bus error); a small bounded retry absorbs that without
# masking a genuinely-down unit.  Kept tiny — NOT infinite.
_SYSTEMCTL_RETRIES = 3
_SYSTEMCTL_BACKOFF_SEC = 0.5

# ---------------------------------------------------------------------------
# Low-level helpers (patched in tests)
# ---------------------------------------------------------------------------


def _run_systemctl(action: str, unit: str) -> None:
    """Run ``systemctl --user <action> <unit>`` with bounded retry.

    Retries up to ``_SYSTEMCTL_RETRIES`` times with linear backoff to absorb
    transient systemd/D-Bus flakiness (v5.69 P5).  Raises RuntimeError with the
    LAST failure's stderr if every attempt fails.
    """
    cmd = ["systemctl", "--user", action, unit]
    last_stderr = ""
    for attempt in range(1, _SYSTEMCTL_RETRIES + 1):
        result = subprocess.run(cmd, capture_output=True)
        if result.returncode == 0:
            return
        last_stderr = result.stderr.decode(errors="replace").strip()
        if attempt < _SYSTEMCTL_RETRIES:
            _log.warning(
                "systemctl --user %s %s failed (attempt %d/%d): %s — retrying",
                action,
                unit,
                attempt,
                _SYSTEMCTL_RETRIES,
                last_stderr,
            )
            time.sleep(_SYSTEMCTL_BACKOFF_SEC * attempt)
    raise RuntimeError(
        f"systemctl --user {action} {unit} failed after {_SYSTEMCTL_RETRIES} "
        f"attempts: {last_stderr}"
    )


# ---------------------------------------------------------------------------
# Service-control seam (v5.69 P0)
#
# Module-level wrappers around _run_systemctl so the e2e harness can patch a
# single, real, importable seam (yadgar.scripts.nightly_cycle._stop_service /
# ._start_service) instead of the no-longer-matching guard that silently
# no-opped.  Pure refactor — every nightly start/stop now routes through these.
# Behavior is identical to calling _run_systemctl directly.
# ---------------------------------------------------------------------------


def _stop_service(unit: str) -> None:
    """Stop a systemd --user unit. Wraps _run_systemctl('stop', unit).

    Patch seam: the e2e service_stub replaces this with a no-op recorder so no
    test can trigger a real systemctl stop.
    """
    _run_systemctl("stop", unit)


def _start_service(unit: str) -> None:
    """Start a systemd --user unit. Wraps _run_systemctl('start', unit).

    Patch seam: the e2e service_stub replaces this with a no-op recorder so no
    test can trigger a real systemctl start.
    """
    _run_systemctl("start", unit)


# ---------------------------------------------------------------------------
# Structured log helpers
# ---------------------------------------------------------------------------


def _log_step(step: str, outcome: str, duration_ms: float, **extra) -> None:
    """Emit one I14-conformant structured log line via the nightly_cycle logger."""
    _log.info(
        "%s %s",
        step,
        outcome,
        extra={
            "component": "nightly_cycle",
            "action": step,
            "outcome": outcome,
            "latency_ms": duration_ms,
            **extra,
        },
    )


def _log_start(action: str) -> None:
    """Log the start of a step."""
    _log.info(
        "nightly_cycle %s start",
        action,
        extra={"component": "nightly_cycle", "action": action, "outcome": "start"},
    )


# ---------------------------------------------------------------------------
# Step implementations
# ---------------------------------------------------------------------------


def _step_stop_core() -> int:
    """Step 1: Stop yadgar CORE only. Returns 0 on success, 10 on failure (FATAL).

    Backend stays up throughout the nightly cycle so that consolidation (step 3)
    and backups (steps 2 + 5) can run over HTTP. Stopping only core releases
    core's connection without touching the backend's surrealkv lock — no embedded
    open is attempted at any step (#51 / BC-D1 fix).
    """
    _log_start("stop_core")
    t0 = time.monotonic()
    try:
        _stop_service(_UNIT_CORE)
        _log_step("stop_core", "ok", (time.monotonic() - t0) * 1000)
        return 0
    except Exception as exc:
        record_exception("nightly_cycle.stop_core", exc)
        _log_step("stop_core", "error", (time.monotonic() - t0) * 1000, error=str(exc))
        _log.error(
            "step 1 (stop core) failed — aborting: %s",
            exc,
            extra={"component": "nightly_cycle", "action": "stop_core", "outcome": "error"},
        )
        return 10


def _step_pre_backup(db_path: Path, snapshot_dir: Path, backend_url: str) -> int:
    """Step 2: Pre-backup snapshot via HTTP export. Returns 0 on success, 20 on failure (FATAL).

    Uses backend_url to call GET /export — a transactionally consistent logical
    snapshot taken while the backend is live. No copytree (no surrealkv lock needed).
    """
    _log_start("pre_backup")
    t0 = time.monotonic()
    try:
        create_snapshot(
            db_path, snapshot_dir=snapshot_dir, label="nightly-pre", backend_url=backend_url
        )
        _log_step("pre_backup", "ok", (time.monotonic() - t0) * 1000)
        return 0
    except Exception as exc:
        record_exception("nightly_cycle.pre_backup", exc)
        _log_step("pre_backup", "error", (time.monotonic() - t0) * 1000, error=str(exc))
        _log.error(
            "step 2 (pre-backup) failed — aborting: %s",
            exc,
            extra={"component": "nightly_cycle", "action": "pre_backup", "outcome": "error"},
        )
        return 20


def _step_consolidation(db_path: Path, settings: Settings) -> int:
    """Step 3: Consolidation in server mode. Returns 0 on success, 30 on failure.

    YADGAR_DB_URL remains set so StorageEngine opens in server mode (HTTP
    connection to the live backend). No embedded open, no surrealkv file lock —
    eliminates the SDK 2.0.0 vs server 3.0.5 format-skew failure (BC-D1 / #51).
    """
    _log_start("consolidation")
    t0 = time.monotonic()
    storage = None
    try:
        storage = StorageEngine(str(db_path))
        embeddings = EmbeddingEngine()
        scheduler = ConsolidationScheduler(storage, embeddings, settings)
        # PR-1 wiring (#37): nightly path runs consolidation + the gated sleep
        # cycle (dream/community/cluster/reembed/compress/narrate). The sleep
        # cycle had been dead since v5.7.0 PR-0 dropped the daemon loop — no
        # call site invoked _maybe_sleep_cycle until this entrypoint.
        stats = scheduler.run_nightly_consolidation()
        _log_step("consolidation", "ok", (time.monotonic() - t0) * 1000, stats=str(stats))
        return 0
    except Exception as exc:
        record_exception("nightly_cycle.consolidation", exc)
        _log_step("consolidation", "error", (time.monotonic() - t0) * 1000, error=str(exc))
        _log.warning(
            "step 3 (consolidation) failed — continuing: %s",
            exc,
            extra={"component": "nightly_cycle", "action": "consolidation", "outcome": "error"},
        )
        return 30
    finally:
        if storage is not None:
            try:
                storage.close()
            except Exception:
                pass


def _step_vacuum(db_path: Path, backend_url: str, service_mode: str | None) -> int:
    """Step 4: Vacuum. Returns 0 on success, 40 on failure (non-fatal).

    Backend is already up (was never stopped). The _start_service(_UNIT_BACKEND)
    call below is a safety no-op — starting an already-active unit is harmless —
    kept so that if a prior step somehow left the backend down, vacuum can still
    recover rather than failing silently.  ``cmd_vacuum_impl`` requires the backend
    reachable (GET /export + preflight) before it performs the swap.
    """
    _log_start("vacuum")
    t0 = time.monotonic()
    try:
        _start_service(_UNIT_BACKEND)  # safety no-op: backend was never stopped (#51)
        vacuum_args = SimpleNamespace(
            backend_url=backend_url,
            service_mode=service_mode,
            db_path=str(db_path),
            yes=True,
        )
        vac_code = cmd_vacuum_impl(vacuum_args)
        if vac_code not in (0, 2):
            raise RuntimeError(f"cmd_vacuum_impl returned exit code {vac_code}")
        outcome = "degraded" if vac_code == 2 else "ok"
        _log_step("vacuum", outcome, (time.monotonic() - t0) * 1000)
        return 0
    except Exception as exc:
        record_exception("nightly_cycle.vacuum", exc)
        _log_step("vacuum", "error", (time.monotonic() - t0) * 1000, error=str(exc))
        _log.warning(
            "step 4 (vacuum) failed — continuing: %s",
            exc,
            extra={"component": "nightly_cycle", "action": "vacuum", "outcome": "error"},
        )
        return 40


def _step_post_backup(db_path: Path, snapshot_dir: Path, backend_url: str) -> int:
    """Step 5: Post-backup snapshot via HTTP export. Returns 0 on success, 50 on failure.

    Backend stays up after vacuum (it was never stopped by the nightly cycle).
    Export via GET /export gives a transactionally consistent artifact — no need
    to stop the backend first. The old stop-both-for-copytree logic is removed
    (#51 / BC-D1 fix).
    """
    _log_start("post_backup")
    t0 = time.monotonic()
    try:
        create_snapshot(
            db_path, snapshot_dir=snapshot_dir, label="nightly-post", backend_url=backend_url
        )
        _log_step("post_backup", "ok", (time.monotonic() - t0) * 1000)
        return 0
    except Exception as exc:
        record_exception("nightly_cycle.post_backup", exc)
        _log_step("post_backup", "error", (time.monotonic() - t0) * 1000, error=str(exc))
        _log.warning(
            "step 5 (post-backup snapshot) failed — continuing: %s",
            exc,
            extra={"component": "nightly_cycle", "action": "post_backup", "outcome": "error"},
        )
        return 50


def _step_prune(snapshot_dir: Path, retention: int) -> int:
    """Step 6: Prune old snapshots. Returns 0 on success, 60 on failure."""
    _log_start("prune")
    t0 = time.monotonic()
    try:
        removed = prune_snapshots(snapshot_dir, "surreal_db.nightly-*", retention=retention)
        _log_step("prune", "ok", (time.monotonic() - t0) * 1000, removed=len(removed))
        return 0
    except Exception as exc:
        record_exception("nightly_cycle.prune", exc)
        _log_step("prune", "error", (time.monotonic() - t0) * 1000, error=str(exc))
        _log.warning(
            "step 6 (prune) failed — continuing: %s",
            exc,
            extra={"component": "nightly_cycle", "action": "prune", "outcome": "error"},
        )
        return 60


def _step_start_core() -> int:
    """Step 7: Start yadgar CORE. Returns 0 on success, 70 on failure.

    Backend was never stopped, so only core needs to be started. The backend
    is already running and serving connections — this restores only the half
    that was taken down in step 1.
    """
    _log_start("start_core")
    t0 = time.monotonic()
    try:
        _start_service(_UNIT_CORE)
        _log_step("start_core", "ok", (time.monotonic() - t0) * 1000)
        return 0
    except Exception as exc:
        record_exception("nightly_cycle.start_core", exc)
        _log_step("start_core", "error", (time.monotonic() - t0) * 1000, error=str(exc))
        _log.error(
            "step 7 (start core) failed: %s",
            exc,
            extra={"component": "nightly_cycle", "action": "start_core", "outcome": "error"},
        )
        return 70


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main(args=None) -> int:  # type: ignore[no-untyped-def]
    """Run one nightly cycle. Returns exit code.

    args attributes consumed (all have defaults):
      - db_path (str | None)   — override default from yadgar.paths.DB_PATH
                                 (respects YADGAR_DATA_DIR / XDG; do NOT use
                                  Settings.DB_PATH which reads stale config.yaml)
      - backend_url (str)      — SurrealDB backend URL (default: YADGAR_DB_URL env, else http://127.0.0.1:8080)
      - service_mode (str)     — "systemd" | "docker" | "manual" | None (auto-detect)
      - retention (int)        — snapshot retention count (default YADGAR_BACKUP_RETENTION)
    """
    configure_logging(log_format="json", level="INFO")

    settings = Settings()
    # Derive db_path from yadgar.paths.DB_PATH (respects YADGAR_DATA_DIR / XDG
    # default ~/.local/share/yadgar).  Do NOT fall back to settings.DB_PATH —
    # config.yaml may carry a stale legacy value (e.g. ~/.yadgar/surreal_db)
    # that points at a non-existent directory, causing snapshot to fail.
    db_path_str: str = getattr(args, "db_path", None) or str(_paths.DB_PATH)
    db_path = Path(db_path_str).expanduser()
    snapshot_dir = db_path.parent

    backend_url: str = getattr(args, "backend_url", None) or os.environ.get(
        "YADGAR_DB_URL", "http://127.0.0.1:8080"
    )
    service_mode: str | None = getattr(args, "service_mode", None)
    retention: int = getattr(args, "retention", None) or default_retention()

    first_failure: int = 0

    # Step 1: stop core (FATAL on failure)
    code = _step_stop_core()
    if code != 0:
        return code

    # Step 2: pre-backup (FATAL on failure)
    code = _step_pre_backup(db_path, snapshot_dir, backend_url)
    if code != 0:
        return code

    # Steps 3-7: non-fatal — always attempt all remaining steps
    for step_fn in [
        lambda: _step_consolidation(db_path, settings),
        lambda: _step_vacuum(db_path, backend_url, service_mode),
        lambda: _step_post_backup(db_path, snapshot_dir, backend_url),
        lambda: _step_prune(snapshot_dir, retention),
        lambda: _step_start_core(),
    ]:
        result = step_fn()
        if result != 0 and first_failure == 0:
            first_failure = result

    outcome = "ok" if first_failure == 0 else "error"
    _log.info(
        "nightly_cycle complete",
        extra={
            "component": "nightly_cycle",
            "action": "complete",
            "outcome": outcome,
            "exit_code": first_failure,
        },
    )
    return first_failure


# ---------------------------------------------------------------------------
# Module entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sys.exit(main())
