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
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

# ---------------------------------------------------------------------------
# v5.72: suppress OTLP exporter BEFORE any yadgar import.
#
# yadgar/server/_app.py calls setup_tracing("yadgar-core") at module import
# time. setup_tracing reads Settings via get_settings() (lru_cache) which
# pulls YADGAR_OTLP_ENDPOINT from ~/.yadgar/config.yaml. In production that
# endpoint (http://host.containers.internal:4318) doesn't resolve on the host,
# so BatchSpanProcessor hangs at exit retrying failed exports (~10 s backoff),
# flooding WARN logs and blocking the nightly process.
#
# Setting env before any yadgar import makes pydantic-settings prefer env over
# yaml, disabling OTLP for this nightly process only.
os.environ.setdefault("YADGAR_OTLP_ENDPOINT", "")

import yadgar._shared.paths as _paths
from yadgar._shared.config import Settings
from yadgar._shared.observability.exception_telemetry import record_exception
from yadgar._shared.observability.log_config import configure_logging
from yadgar._shared.storage import StorageEngine
from yadgar.core.backup import create_snapshot, default_retention, prune_snapshots
from yadgar.core.consolidation import run_nightly_consolidation
from yadgar.core.vacuum import cmd_vacuum_impl

_log = logging.getLogger("yadgar.nightly_cycle")

_UNIT_CORE = "yadgar"

# R3 Car 1 D3: _make_embedding_engine removed — the consolidation compute (its
# only caller) is now forwarded to the backend, which builds its own embedding
# engine. The nightly script no longer constructs a local embedding engine.

_UNIT_BACKEND = "yadgar-backend"

# Bounded retry/backoff around systemctl --user (v5.69 P5).  The host has a
# history of transient systemd/D-Bus flakiness (the 06-16 restore failed on a
# systemctl --user/D-Bus error); a small bounded retry absorbs that without
# masking a genuinely-down unit.  Kept tiny — NOT infinite.
_SYSTEMCTL_RETRIES = 3
_SYSTEMCTL_BACKOFF_SEC = 0.5

# Default URL for the running yadgar core process (port from settings.PORT = 8765).
_CORE_URL = os.environ.get("YADGAR_CORE_URL", "http://127.0.0.1:8765")

# ---------------------------------------------------------------------------
# Maintenance mode HTTP helper (v5.50.3)
#
# Replaces systemctl stop/start for the CORE unit.  Core stays UP during the
# nightly cycle — an in-process flag gates all MCP tools so they fast-fail
# with a clean structured error instead of hanging or racing the backend.
# ---------------------------------------------------------------------------


def _maintenance_http(action: str, core_url: str = _CORE_URL) -> None:
    """POST /api/control/maintenance/{action} to the running core.

    action: "enter" or "exit"

    Raises on HTTP error or connection failure — caller converts to exit codes.
    Auth token from YADGAR_MCP_AUTH_TOKEN if set (same as MCP clients use).

    Patch seam: tests replace this function via patch.multiple(_MODULE, ...).
    """
    url = f"{core_url}/api/control/maintenance/{action}"
    data = b"{}"
    headers: dict[str, str] = {"Content-Type": "application/json"}
    token = os.environ.get("YADGAR_MCP_AUTH_TOKEN", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status not in (200, 202):
                body = resp.read(256).decode(errors="replace")
                raise RuntimeError(f"maintenance {action} returned HTTP {resp.status}: {body}")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"maintenance {action} HTTP error {exc.code}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise ConnectionError(
            f"maintenance {action} connection failed to {core_url}: {exc.reason}"
        ) from exc


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


def _step_stop_core(core_url: str = _CORE_URL) -> int:
    """Step 1: Enter nightly maintenance mode in core. Always returns 0 (best-effort).

    v5.72 (#62): Core STAYS UP — we flip an in-process maintenance flag via HTTP
    instead of systemctl stop.  All MCP tools fast-fail with a structured error
    while the flag is on, so connected Claude instances keep their MCP connection
    without experiencing a reconnect.  Backend stays up throughout (#51 / BC-D1).

    Entering maintenance is BEST-EFFORT, never fatal: if core is unreachable, on
    an older build without the /maintenance route (404), or otherwise errors, we
    log a warning and PROCEED. Rationale: a down/old core has no external writers
    to gate, and aborting the whole nightly (losing consolidation + backups +
    vacuum) is strictly worse than running with a small write-race window. This
    also avoids a rollout chicken-and-egg (the pre-5.72 core has no endpoint).
    """
    _log_start("stop_core")
    t0 = time.monotonic()
    try:
        _maintenance_http("enter", core_url)
        _log_step("stop_core", "ok", (time.monotonic() - t0) * 1000)
    except Exception as exc:
        record_exception("nightly_cycle.stop_core", exc)
        _log_step("stop_core", "warn", (time.monotonic() - t0) * 1000, error=str(exc))
        _log.warning(
            "step 1 (maintenance enter) unreachable — proceeding without write-gate: %s",
            exc,
            extra={"component": "nightly_cycle", "action": "stop_core", "outcome": "degraded"},
        )
    return 0


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

    R3 Car 1 D3: the consolidation COMPUTE (cycle + gated sleep) lives in the
    backend. This step calls the CORE nightly orchestrator, which forwards the
    compute to the backend ``/consolidate`` endpoint (mode="nightly") and then
    runs the core viz/admin tail (graph-layout precompute + invariant check +
    auto-vacuum) against the local StorageEngine.

    YADGAR_DB_URL remains set so StorageEngine opens in server mode (HTTP
    connection to the live backend). No embedded open, no surrealkv file lock —
    eliminates the SDK 2.0.0 vs server 3.0.5 format-skew failure (BC-D1 / #51).
    """
    _log_start("consolidation")
    t0 = time.monotonic()
    storage = None
    try:
        # Storage is still opened locally for the core tail (invariants +
        # auto-vacuum + graph-layout precompute); the compute itself is forwarded
        # to the backend, which builds its own engines.
        storage = StorageEngine(str(db_path))
        stats = run_nightly_consolidation(storage=storage, settings=settings)
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
        if vac_code == 2:
            # P0 #37 item 3: exit 2 now means the swap was ROLLED BACK (the
            # post-swap verification failed) — data is safe on the original DB,
            # the compaction was discarded. Surface as a step FAILURE (40) so
            # the nightly unit goes red; the 07-09 incident hid exactly this
            # state behind a warn-only "[vacuum] complete." for 16 h.
            _log_step("vacuum", "rolled_back", (time.monotonic() - t0) * 1000)
            _log.error(
                "step 4 (vacuum) ROLLED BACK — swap could not be verified; "
                "data safe on the original DB, compaction discarded (P0 #37)",
                extra={"component": "nightly_cycle", "action": "vacuum", "outcome": "rolled_back"},
            )
            return 40
        if vac_code != 0:
            raise RuntimeError(f"cmd_vacuum_impl returned exit code {vac_code}")
        _log_step("vacuum", "ok", (time.monotonic() - t0) * 1000)
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


def _step_start_core(core_url: str = _CORE_URL) -> int:
    """Step 7: Exit nightly maintenance mode in core. Always returns 0 (best-effort).

    v5.72 (#62): Core was never stopped — flip the maintenance flag back OFF via
    HTTP so MCP tool dispatch resumes normally. Called in a finally block in
    main() to guarantee the flag is cleared even if earlier steps failed.

    Exiting maintenance is BEST-EFFORT, never fatal: if core is unreachable or on
    an older build (404), we log a warning and return 0. A failed exit must not
    mask the real cycle outcome; and if maintenance was never successfully entered
    (core down/old), there is nothing to clear.
    """
    _log_start("start_core")
    t0 = time.monotonic()
    try:
        _maintenance_http("exit", core_url)
        _log_step("start_core", "ok", (time.monotonic() - t0) * 1000)
    except Exception as exc:
        record_exception("nightly_cycle.start_core", exc)
        _log_step("start_core", "warn", (time.monotonic() - t0) * 1000, error=str(exc))
        _log.warning(
            "step 7 (maintenance exit) unreachable — flag may remain set if it was on: %s",
            exc,
            extra={"component": "nightly_cycle", "action": "start_core", "outcome": "degraded"},
        )
    return 0


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main(args=None) -> int:  # type: ignore[no-untyped-def]
    """Run one nightly cycle. Returns exit code.

    args attributes consumed (all have defaults):
      - db_path (str | None)   — override default from yadgar.paths.DB_PATH
                                 (respects YADGAR_DATA_DIR / XDG; do NOT use
                                  Settings.DB_PATH which reads stale config.yaml)
      - backend_url (str)      — SurrealDB backend URL (default: YADGAR_DB_URL env, else http://127.0.0.1:8000)
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
    # ADR-0076 D4: surql backups go to <data-root>/backups/surql/.
    # Derive from db_path.parent (always the data root, whether set from env or args)
    # so tests with custom db_path get the correct relative layout without
    # requiring YADGAR_DATA_DIR to be monkeypatched.
    snapshot_dir = db_path.parent / "backups" / "surql"
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    backend_url: str = getattr(args, "backend_url", None) or os.environ.get(
        "YADGAR_DB_URL", "http://127.0.0.1:8000"
    )
    service_mode: str | None = getattr(args, "service_mode", None)
    retention: int = getattr(args, "retention", None) or default_retention()

    first_failure: int = 0

    # Step 1: enter maintenance mode (FATAL on failure).
    # If enter fails, core is NOT in maintenance — skip the finally-exit entirely.
    code = _step_stop_core()
    if code != 0:
        return code

    # Steps 2-7: maintenance mode is now ON.
    # _step_start_core (step 7, maintenance exit) MUST run in finally to guarantee
    # the flag is cleared even when earlier steps fail (including FATAL step 2).
    try:
        # Step 2: pre-backup (FATAL on failure — exits try, finally still runs)
        code = _step_pre_backup(db_path, snapshot_dir, backend_url)
        if code != 0:
            first_failure = code
            return code  # Python runs finally on return

        # Steps 3-6: non-fatal — always attempt all
        for step_fn in [
            lambda: _step_consolidation(db_path, settings),
            lambda: _step_vacuum(db_path, backend_url, service_mode),
            lambda: _step_post_backup(db_path, snapshot_dir, backend_url),
            lambda: _step_prune(snapshot_dir, retention),
        ]:
            result = step_fn()
            if result != 0 and first_failure == 0:
                first_failure = result

    finally:
        # Step 7: exit maintenance mode — always runs (return, exception, or normal flow).
        exit_code = _step_start_core()
        if exit_code != 0 and first_failure == 0:
            first_failure = exit_code

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
