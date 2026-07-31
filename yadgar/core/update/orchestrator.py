"""v5.49.0 Phase 9 — Upgrade orchestrator state machine.

Implements the routine-upgrade state machine: file lock with PID stale-detection,
snapshot capture, image pull, env-file rewrite, graceful stop, service restart,
health-check, CLI upgrade, and re-exec handoff.

STATE MACHINE NOTE — RE_EXECING is the last in-process state:
  In production, run_install() ends at RE_EXECING because os.execvp() never returns.
  The DONE state is set by the --finalize subcommand (Phase 10) after re-exec.
  Both test_orchestrator_happy_path and test_orchestrator_records_re_exec_state
  assert final_state == RE_EXECING for this reason.

Rollback policy:
  - Failures at PULLING_IMAGE → HEALTH_CHECKING trigger daemon rollback.
  - Failure at CLI_UPGRADING triggers CLI-only rollback (daemon healthy on new image).
  - Failures after RE_EXECING are handled by --rollback CLI subcommand (Phase 10).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from yadgar._shared import paths
from yadgar._shared.observability.observe import observe

logger = logging.getLogger(__name__)

# Default paths (overridable via DI parameters for tests)
_DEFAULT_LOCK_PATH = paths.STATE_DIR / "upgrade.lock"
_DEFAULT_UPGRADE_ENV_PATH = paths.STATE_DIR / "upgrade.env"
_DEFAULT_UNIT_FILE_PATH = Path.home() / ".config" / "systemd" / "user" / "yadgar.service"


class OrchestratorState(Enum):
    IDLE = "idle"
    ACQUIRING_LOCK = "acquiring_lock"
    PROBING_PYPI = "probing_pypi"
    SNAPSHOTTING = "snapshotting"
    PULLING_IMAGE = "pulling_image"
    WRITING_ENV_FILE = "writing_env_file"
    GRACEFUL_STOPPING = "graceful_stopping"
    RESTARTING_SERVICE = "restarting_service"
    HEALTH_CHECKING = "health_checking"
    CLI_UPGRADING = "cli_upgrading"
    RE_EXECING = "re_execing"
    DONE = "done"
    # Rollback states
    ROLLING_BACK_DAEMON = "rolling_back_daemon"
    ROLLED_BACK_OK = "rolled_back_ok"
    ROLLED_BACK_FAILED = "rolled_back_failed"
    ROLLING_BACK_CLI_ONLY = "rolling_back_cli_only"
    DONE_CLI_ROLLBACK_OK = "done_cli_rollback_ok"
    DONE_CLI_ROLLBACK_FAILED = "done_cli_rollback_failed"
    # Finalize handoff state (set after exec, observed by --finalize CLI subcommand, Phase 10)
    DONE_BUT_FINALIZE_FAILED = "done_but_finalize_failed"


@dataclass
class OrchestratorResult:
    final_state: OrchestratorState
    snapshot_path: Path | None
    from_version: str
    to_version: str
    forward_log: list[dict]
    rollback_log: list[dict] | None
    error: str | None


@dataclass
class _Hooks:
    """Dependency-injection bundle for all external callables."""

    image_pull: Callable
    graceful_stop: Callable
    service_restart: Callable
    health_check: Callable
    cli_upgrade: Callable
    cli_rollback: Callable
    re_exec: Callable


@dataclass
class _RunCtx:
    """Mutable orchestrator run context — carries state across phase helpers."""

    lock_path: Path
    env_path: Path
    snaps_dir: Path
    retention: int
    hooks: _Hooks
    prev_image_tag_override: str | None
    prev_unit_file_override: str | None
    prev_cli_version_override: str | None
    # Mutable run-time fields
    from_version: str = "unknown"
    to_version: str = "unknown"
    current_state: OrchestratorState = OrchestratorState.IDLE
    forward_log: list[dict] = field(default_factory=list)
    snapshot: object = None  # yadgar.update.snapshot.Snapshot | None

    @observe(tier="stage")
    def log(self, state: OrchestratorState, detail: dict | None = None) -> None:
        self.current_state = state
        entry = {"ts": _now_isoformat(), "state": state.value, "detail": detail}
        self.forward_log.append(entry)
        if self.snapshot is not None:
            self.snapshot.append_forward_log(state.value, detail)

    def result(
        self,
        final_state: OrchestratorState,
        *,
        error: str | None,
        rollback_log: list[dict] | None = None,
    ) -> OrchestratorResult:
        return OrchestratorResult(
            final_state=final_state,
            snapshot_path=self.snapshot.path if self.snapshot is not None else None,
            from_version=self.from_version,
            to_version=self.to_version,
            forward_log=self.forward_log,
            rollback_log=rollback_log,
            error=error,
        )

    def rollback_daemon(self, *, pulled_new: bool) -> list[dict]:
        return _rollback_daemon(
            prev_tag=self._prev_tag(),
            env_path=self.env_path,
            service_restart_fn=self.hooks.service_restart,
            health_check_fn=self.hooks.health_check,
            snapshot=self.snapshot,
            pulled_new=pulled_new,
        )

    @observe(tier="stage")
    def _prev_tag(self) -> str:
        if self.snapshot is not None:
            return self.snapshot.read_prev_image_tag() or ""
        return ""


# ---------------------------------------------------------------------------
# File lock helpers
# ---------------------------------------------------------------------------


@observe(tier="stage")
def _acquire_lock(
    lock_path: Path,
    version_from: str,
    version_to: str,
    lock_max_age_seconds: int,
) -> str | None:
    """Acquire the upgrade lock.

    Returns None on success. Returns an error string if lock is held by a live process.
    Overwrites stale locks (dead PID or age > lock_max_age_seconds).
    """
    if lock_path.exists():
        try:
            data = json.loads(lock_path.read_text())
            pid = int(data.get("pid", 0))
            start_ts = float(data.get("start_ts", 0))
        except (json.JSONDecodeError, ValueError, KeyError):  # fmt: skip
            logger.warning("upgrade.lock is corrupt; treating as stale and overwriting")
            pid = 0
            start_ts = 0

        pid_alive = False
        if pid > 0:
            try:
                os.kill(pid, 0)
                pid_alive = True
            except OSError:
                pid_alive = False

        age_seconds = time.time() - start_ts
        age_ok = age_seconds <= lock_max_age_seconds

        if pid_alive and age_ok:
            started_ago = int(age_seconds)
            return f"concurrent upgrade in progress (pid={pid}, started {started_ago}s ago)"

        logger.info(
            "upgrade.lock is stale (pid=%d alive=%s age=%ds); overwriting",
            pid,
            pid_alive,
            int(age_seconds),
        )

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "start_ts": time.time(),
                "version_from": version_from,
                "version_to": version_to,
            }
        )
    )
    return None


@observe(tier="stage")
def _release_lock(lock_path: Path) -> None:
    try:
        lock_path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Failed to release upgrade lock at %s: %s", lock_path, exc)


# ---------------------------------------------------------------------------
# Phase helpers (each raises on failure; caller handles exceptions)
# ---------------------------------------------------------------------------


@observe(tier="stage")
def _phase_probe_pypi(ctx: _RunCtx, target_version: str | None) -> str:
    """Return resolved target version (from override or PyPI)."""
    if target_version is not None:
        return target_version
    from yadgar.core.update.check import probe_latest_version  # noqa: PLC0415

    info = probe_latest_version()
    return info.available_version


@observe(tier="stage")
def _phase_snapshot(ctx: _RunCtx) -> None:
    """Create snapshot directory and write prev-state files."""
    from yadgar.core.update.snapshot import create_snapshot  # noqa: PLC0415

    ctx.snapshot = create_snapshot(base_dir=ctx.snaps_dir)

    # Replay pre-snapshot log entries into the snapshot's forward_log.json
    for entry in ctx.forward_log:
        ctx.snapshot.append_forward_log(entry["state"], entry.get("detail"))

    prev_tag = ctx.prev_image_tag_override or _read_current_image_tag(ctx.env_path)
    prev_unit = ctx.prev_unit_file_override or _read_unit_file()
    prev_cli = ctx.prev_cli_version_override or ctx.from_version

    ctx.snapshot.write_prev_image_tag(prev_tag or "")
    ctx.snapshot.write_prev_unit_file(prev_unit or "")
    ctx.snapshot.write_prev_cli_version(prev_cli or "")


@observe(tier="stage")
def _phase_cli_upgrade(ctx: _RunCtx) -> str | None:
    """Attempt CLI upgrade; return error string on failure, None on success."""
    try:
        ctx.hooks.cli_upgrade(ctx.to_version)
        return None
    except Exception as exc:
        return str(exc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _disabled_result(target_version: str | None) -> OrchestratorResult:
    """Return the 'install disabled' refusal result."""
    return OrchestratorResult(
        final_state=OrchestratorState.IDLE,
        snapshot_path=None,
        from_version="unknown",
        to_version=target_version or "unknown",
        forward_log=[],
        rollback_log=None,
        error=(
            "yadgar update --install is disabled. "
            f"Set update.install_enabled=true in {paths.CONFIG_YAML_PATH} "
            "after reading docs/plans/archive/PLAN_V5_49_0.md § Rollout."
        ),
    )


@dataclass
class InstallConfig:
    """Configuration bundle for run_install — groups all non-hook parameters.

    All fields default to None; None means "use the production default" so tests
    need only supply the fields they want to override.
    """

    target_version: str | None = None
    enabled_override: bool | None = None
    lock_file_path: Path | None = None
    snapshots_base_dir: Path | None = None
    upgrade_env_path: Path | None = None
    snapshot_retention: int | None = None
    prev_image_tag_override: str | None = None
    prev_unit_file_override: str | None = None
    prev_cli_version_override: str | None = None


def _build_hooks(
    image_pull: Callable | None,
    graceful_stop: Callable | None,
    service_restart: Callable | None,
    health_check: Callable | None,
    cli_upgrade: Callable | None,
    cli_rollback: Callable | None,
    re_exec: Callable | None,
) -> _Hooks:  # noqa: PLR0913 — one param per injectable hook; no way to reduce without losing DI
    """Resolve DI callables to defaults where not provided."""
    return _Hooks(
        image_pull=image_pull if image_pull is not None else _default_image_pull,
        graceful_stop=graceful_stop if graceful_stop is not None else _default_graceful_stop,
        service_restart=service_restart
        if service_restart is not None
        else _default_service_restart,
        health_check=health_check if health_check is not None else _default_health_check,
        cli_upgrade=cli_upgrade if cli_upgrade is not None else _default_cli_upgrade,
        cli_rollback=cli_rollback if cli_rollback is not None else _default_cli_rollback,
        re_exec=re_exec if re_exec is not None else _default_re_exec,
    )


@observe(tier="boundary")
def run_install(
    config: InstallConfig | None = None,
    hooks: _Hooks | None = None,
) -> OrchestratorResult:
    """Routine upgrade state machine.

    Acquires lock, snapshots prev state, pulls image, writes env-file,
    gracefully stops daemon, restarts service, health-checks, upgrades CLI,
    re-execs.

    Rollback fires on failure at states >= PULLING_IMAGE and <= HEALTH_CHECKING.
    Post-CLI_UPGRADING failure attempts CLI revert (best-effort).
    Post-RE_EXECING failure is NOT auto-rolled (--rollback CLI handles it; Phase 10).

    NOTE: RE_EXECING is the last in-process state — os.execvp never returns.
    DONE is set by Phase 10 --finalize subcommand, not by this function.

    Args:
        config: InstallConfig grouping all path/version/retention overrides.
                Defaults to InstallConfig() (all production defaults).
        hooks:  _Hooks bundle for test-injectable callables.
                Defaults to _build_hooks() (all production implementations).
    """
    if config is None:
        config = InstallConfig()
    if hooks is None:
        hooks = _build_hooks(None, None, None, None, None, None, None)

    from yadgar._shared.config import get_settings  # noqa: PLC0415

    settings = get_settings()

    install_enabled = (
        config.enabled_override
        if config.enabled_override is not None
        else settings.UPDATE_INSTALL_ENABLED
    )
    if not install_enabled:
        return _disabled_result(config.target_version)

    ctx = _RunCtx(
        lock_path=config.lock_file_path
        if config.lock_file_path is not None
        else _DEFAULT_LOCK_PATH,
        env_path=config.upgrade_env_path
        if config.upgrade_env_path is not None
        else _DEFAULT_UPGRADE_ENV_PATH,
        snaps_dir=config.snapshots_base_dir
        if config.snapshots_base_dir is not None
        else (paths.STATE_DIR / "upgrade-snapshots"),
        retention=config.snapshot_retention
        if config.snapshot_retention is not None
        else settings.UPDATE_SNAPSHOT_RETENTION,
        hooks=hooks,
        prev_image_tag_override=config.prev_image_tag_override,
        prev_unit_file_override=config.prev_unit_file_override,
        prev_cli_version_override=config.prev_cli_version_override,
        to_version=config.target_version if config.target_version is not None else "unknown",
    )

    ctx.log(OrchestratorState.ACQUIRING_LOCK)
    lock_error = _acquire_lock(
        ctx.lock_path, ctx.from_version, ctx.to_version, settings.UPDATE_LOCK_MAX_AGE_SECONDS
    )
    if lock_error:
        return OrchestratorResult(
            final_state=OrchestratorState.IDLE,
            snapshot_path=None,
            from_version=ctx.from_version,
            to_version=ctx.to_version,
            forward_log=ctx.forward_log,
            rollback_log=None,
            error=lock_error,
        )

    try:
        return _run_forward(ctx, config.target_version)
    finally:
        _release_lock(ctx.lock_path)


@observe(tier="stage")
def _run_forward(ctx: _RunCtx, target_version: str | None) -> OrchestratorResult:
    """Execute the forward upgrade path (steps 2–10)."""
    from yadgar.core.update.snapshot import prune_old_snapshots  # noqa: PLC0415

    # Step 2: PROBING_PYPI
    ctx.log(OrchestratorState.PROBING_PYPI)
    try:
        ctx.to_version = _phase_probe_pypi(ctx, target_version)
    except Exception as exc:
        return ctx.result(ctx.current_state, error=f"PyPI probe failed: {exc}")
    ctx.from_version = _get_current_cli_version()

    # Step 3: SNAPSHOTTING
    ctx.log(OrchestratorState.SNAPSHOTTING)
    _phase_snapshot(ctx)
    prev_tag = ctx.snapshot.read_prev_image_tag() or ""  # type: ignore[union-attr]
    prev_cli = ctx.snapshot.read_prev_cli_version() or ctx.from_version  # type: ignore[union-attr]
    ctx.snapshot.write_target_version(ctx.to_version)  # type: ignore[union-attr]
    ctx.log(OrchestratorState.SNAPSHOTTING, {"prev_image_tag": prev_tag})

    # Step 4: PULLING_IMAGE
    ctx.log(OrchestratorState.PULLING_IMAGE, {"version": ctx.to_version})
    try:
        ctx.hooks.image_pull(ctx.to_version)
    except Exception as exc:
        rlog = ctx.rollback_daemon(pulled_new=False)
        return ctx.result(
            OrchestratorState.ROLLED_BACK_OK, error=f"image pull failed: {exc}", rollback_log=rlog
        )

    # Step 5: WRITING_ENV_FILE
    ctx.log(OrchestratorState.WRITING_ENV_FILE, {"version": ctx.to_version})
    try:
        _write_env_file(ctx.env_path, ctx.to_version)
    except Exception as exc:
        rlog = ctx.rollback_daemon(pulled_new=True)
        return ctx.result(
            _rollback_final_state(rlog), error=f"env-file write failed: {exc}", rollback_log=rlog
        )

    # Step 6: GRACEFUL_STOPPING
    ctx.log(OrchestratorState.GRACEFUL_STOPPING)
    try:
        ctx.hooks.graceful_stop(30)
    except Exception as exc:
        rlog = ctx.rollback_daemon(pulled_new=True)
        return ctx.result(
            _rollback_final_state(rlog), error=f"graceful stop failed: {exc}", rollback_log=rlog
        )

    # Step 7: RESTARTING_SERVICE
    ctx.log(OrchestratorState.RESTARTING_SERVICE)
    try:
        ctx.hooks.service_restart()
    except Exception as exc:
        rlog = ctx.rollback_daemon(pulled_new=True)
        return ctx.result(
            _rollback_final_state(rlog), error=f"service restart failed: {exc}", rollback_log=rlog
        )

    # Step 8: HEALTH_CHECKING
    ctx.log(OrchestratorState.HEALTH_CHECKING)
    if not ctx.hooks.health_check():
        rlog = ctx.rollback_daemon(pulled_new=True)
        return ctx.result(
            _rollback_final_state(rlog),
            error="health check failed after restart",
            rollback_log=rlog,
        )

    # Step 9: CLI_UPGRADING
    ctx.log(OrchestratorState.CLI_UPGRADING, {"version": ctx.to_version})
    cli_error = _phase_cli_upgrade(ctx)
    if cli_error:
        return _handle_cli_rollback(ctx, prev_cli, cli_error)

    # Step 10: RE_EXECING (last in-process state — execvp never returns in production)
    ctx.log(OrchestratorState.RE_EXECING, {"version": ctx.to_version})
    try:
        prune_old_snapshots(retention=ctx.retention, base_dir=ctx.snaps_dir)
    except Exception as exc:
        logger.warning("Snapshot pruning failed (non-fatal): %s", exc)

    snap_path = ctx.snapshot.path if ctx.snapshot is not None else Path("/tmp")  # type: ignore[union-attr]
    ctx.hooks.re_exec(ctx.to_version, snap_path)

    # In production: never reached (execvp replaced the process).
    # In tests (mock re_exec returns normally): return RE_EXECING as final state.
    return ctx.result(OrchestratorState.RE_EXECING, error=None)


@observe(tier="stage")
def _handle_cli_rollback(ctx: _RunCtx, prev_cli: str, cli_error: str) -> OrchestratorResult:
    """Attempt CLI-only rollback after a failed CLI upgrade."""
    ctx.log(OrchestratorState.ROLLING_BACK_CLI_ONLY, {"prev_version": prev_cli})
    if ctx.snapshot is not None:
        ctx.snapshot.append_rollback_log(
            OrchestratorState.ROLLING_BACK_CLI_ONLY.value,
            {"prev_version": prev_cli, "error": cli_error},
        )
    try:
        ctx.hooks.cli_rollback(prev_cli)
        final = OrchestratorState.DONE_CLI_ROLLBACK_OK
    except Exception as rb_exc:
        logger.error("CLI rollback failed: %s", rb_exc)
        final = OrchestratorState.DONE_CLI_ROLLBACK_FAILED

    return ctx.result(final, error=f"CLI upgrade failed: {cli_error}", rollback_log=[])


# ---------------------------------------------------------------------------
# Rollback helper
# ---------------------------------------------------------------------------


@observe(tier="stage")
def _rollback_daemon(
    *,
    prev_tag: str,
    env_path: Path,
    service_restart_fn: Callable,
    health_check_fn: Callable,
    snapshot: object,
    pulled_new: bool,
) -> list[dict]:
    """Restore daemon to prev image tag and verify health."""
    rollback_log: list[dict] = []

    def _rlog(state: str, detail: dict | None = None) -> None:
        entry = {"ts": _now_isoformat(), "state": state, "detail": detail}
        rollback_log.append(entry)
        if snapshot is not None:
            snapshot.append_rollback_log(state, detail)  # type: ignore[union-attr]

    _rlog(OrchestratorState.ROLLING_BACK_DAEMON.value, {"prev_tag": prev_tag})

    if not pulled_new:
        # Nothing was mutated — rollback is a no-op.
        _rlog(OrchestratorState.ROLLED_BACK_OK.value, {"note": "no mutations — rollback is no-op"})
        return rollback_log

    # Restore env-file to prev tag
    if prev_tag:
        try:
            _write_env_file(env_path, _extract_version_from_tag(prev_tag), full_tag=prev_tag)
        except Exception as exc:
            logger.error("Failed to restore env-file during rollback: %s", exc)
            _rlog("env_file_restore_failed", {"error": str(exc)})

    # Restart service with old image
    try:
        service_restart_fn()
    except Exception as exc:
        logger.error("Rollback service restart failed: %s", exc)
        _rlog(OrchestratorState.ROLLED_BACK_FAILED.value, {"error": str(exc)})
        return rollback_log

    # Health-check old image
    try:
        hc_ok = health_check_fn()
    except Exception as exc:
        logger.error("Rollback health-check raised: %s", exc)
        hc_ok = False

    if hc_ok:
        _rlog(OrchestratorState.ROLLED_BACK_OK.value)
    else:
        _rlog(OrchestratorState.ROLLED_BACK_FAILED.value)

    return rollback_log


@observe(tier="stage")
def _rollback_final_state(rollback_log: list[dict]) -> OrchestratorState:
    if not rollback_log:
        return OrchestratorState.ROLLED_BACK_FAILED
    last = rollback_log[-1]["state"]
    if last == OrchestratorState.ROLLED_BACK_OK.value:
        return OrchestratorState.ROLLED_BACK_OK
    return OrchestratorState.ROLLED_BACK_FAILED


# ---------------------------------------------------------------------------
# Default callable implementations (overridable via DI)
# ---------------------------------------------------------------------------


@observe(tier="stage")
def _default_image_pull(version: str) -> None:
    """Pull BOTH images `yadgar upgrade` needs — core at *version*, plus the backend.

    task:0101, two defects that shipped together:

    1. CORE-ONLY PULL. This pulled the core image and nothing else, so an upgrade
       installed a fresh core against whatever backend image happened to be on
       disk. Core and backend version independently (core 5.170.x / backend
       5.60.x) and `daemon start` needs both, so an upgraded install could run a
       new core against a stale backend with no warning. Same class as task:0099
       on `daemon pull`, but on the path users hit repeatedly.
    2. HARDCODED RUNTIME. A literal "podman" argv head — the mirror image of
       task:0083's hardcoded "docker" — crashing docker-only hosts.

    The backend tag is resolved exactly the way `YadgarDaemon.pull()` and
    `start_backend()` resolve it (YADGAR_BACKEND_IMAGE env override, else
    DOCKERHUB_BACKEND_IMAGE), so pull and start can never disagree about which
    tag they want. Note DOCKERHUB_BACKEND_IMAGE derives from the CURRENTLY
    installed server.json, so an upgrade fetches the backend tag the running
    install expects, not the one the new core will ship with. That is the
    consistent choice: the systemd unit's baked backend tag is likewise the old
    one until `install-service` reruns, so pull and start stay in agreement.

    Both pulls use check=True: a failure at PULLING_IMAGE must abort into the
    rollback path rather than pass silently.
    """
    # Local imports mirror _default_health_check: keep core/update importable
    # without dragging the daemon package (and its import-time server.json read)
    # into every consumer.
    from yadgar.core.daemon.runtime import (  # noqa: PLC0415
        DOCKERHUB_BACKEND_IMAGE,
        _get_runtime,
    )

    rt = _get_runtime()
    backend_image = os.environ.get("YADGAR_BACKEND_IMAGE", DOCKERHUB_BACKEND_IMAGE)

    subprocess.run([rt, "pull", f"docker.io/openfantasy/yadgar:{version}"], check=True)
    subprocess.run([rt, "pull", backend_image], check=True)


@observe(tier="stage")
def _default_graceful_stop(timeout: int) -> None:
    subprocess.run(
        ["yadgar", "daemon", "graceful-stop", f"--timeout={timeout}"],
        check=True,
    )


@observe(tier="stage")
def _default_service_restart() -> None:
    subprocess.run(["systemctl", "--user", "restart", "yadgar.service"], check=True)


@observe(tier="stage")
def _default_health_check() -> bool:
    """Post-restart gate — did the daemon process come back up at all?

    Liveness (ADR-0019), not readiness: only "is the process responding"
    matters for this HEALTH_CHECKING step. Probing readiness (/health) here
    would let a transiently-busy backend fail this gate and trigger a rollback
    of a perfectly good restart/upgrade.
    """
    import urllib.request  # noqa: PLC0415

    from yadgar._shared.config import get_settings  # noqa: PLC0415

    settings = get_settings()
    url = f"http://{settings.HOST}:{settings.PORT}/health/live"
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1)
    return False


@observe(tier="stage")
def _default_cli_upgrade(version: str) -> None:  # noqa: ARG001
    subprocess.run(["pipx", "upgrade", "yadgar"], check=True)


@observe(tier="stage")
def _default_cli_rollback(prev_version: str) -> None:
    subprocess.run(
        ["pipx", "install", "--force", f"yadgar=={prev_version}"],
        check=True,
    )


def _default_re_exec(version: str, snapshot_path: Path) -> None:  # noqa: ARG001
    os.execvp(
        "yadgar",
        ["yadgar", "update", "--finalize", "--snapshot", str(snapshot_path)],
    )


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


def _now_isoformat() -> str:
    from datetime import UTC, datetime  # noqa: PLC0415

    return datetime.now(tz=UTC).isoformat()


@observe(tier="stage")
def _get_current_cli_version() -> str:
    try:
        from importlib.metadata import version  # noqa: PLC0415

        return version("yadgar")
    except Exception:  # noqa: BLE001
        return "unknown"


@observe(tier="stage")
def _read_current_image_tag(env_path: Path) -> str | None:
    if not env_path.exists():
        return None
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line.startswith("YADGAR_IMAGE_TAG="):
            return line[len("YADGAR_IMAGE_TAG=") :]
    return None


@observe(tier="stage")
def _read_unit_file() -> str | None:
    try:
        return _DEFAULT_UNIT_FILE_PATH.read_text()
    except OSError:
        return None


@observe(tier="stage")
def _write_env_file(env_path: Path, version: str, *, full_tag: str | None = None) -> None:
    """Atomically rewrite upgrade.env with the new image tag."""
    import tempfile  # noqa: PLC0415

    tag = full_tag or f"docker.io/openfantasy/yadgar:{version}"
    content = f"YADGAR_IMAGE_TAG={tag}\n"
    env_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(dir=env_path.parent, prefix="tmp")
    try:
        os.fchmod(tmp_fd, 0o600)
        with os.fdopen(tmp_fd, "w") as fh:
            fh.write(content)
        os.replace(tmp_name, env_path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


@observe(tier="stage")
def _extract_version_from_tag(tag: str) -> str:
    if ":" in tag:
        return tag.rsplit(":", 1)[-1]
    return tag
