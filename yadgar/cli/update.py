"""v5.49.0 Phase 10 — `yadgar update [--check|--install|--finalize|--rollback]`.

v5.48 shipped CHECK-ONLY. Phase 10 adds:
  --install    Routine-upgrade orchestrator. Gated by update.install_enabled.
  --finalize   Re-exec target invoked by orchestrator after pipx upgrade.
               Verifies daemon version, writes DONE to forward_log.json, releases lock.
               Internal-use — invoked via os.execvp by the orchestrator, not by operators.
  --rollback   Operator recovery. Restores prev_image_tag, restarts daemon, health-checks.
               Does NOT revert CLI version — operators pin manually after this completes.
               Rationale: rolling back the CLI risks the same self-replacement edge cases
               the orchestrator avoids via re-exec; operators should pin manually.

Usage:
  yadgar update              # same as --check
  yadgar update --check      # probe PyPI, print upgrade command, exit 0
  yadgar update --install    # run orchestrator (gated by update.install_enabled: true)
  yadgar update --finalize --snapshot <path>  # post-exec verification (orchestrator-internal)
  yadgar update --rollback [--snapshot <path>]  # operator recovery
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

# Default paths (may be monkeypatched in tests)
_DEFAULT_LOCK_PATH: Path = Path.home() / ".yadgar" / "upgrade.lock"
_DEFAULT_UPGRADE_ENV_PATH: Path = Path.home() / ".yadgar" / "upgrade.env"
_DEFAULT_SNAPSHOTS_BASE_DIR: Path = Path.home() / ".yadgar" / "upgrade-snapshots"


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the `update` subcommand."""
    parser = subparsers.add_parser(
        "update",
        help="Check for or apply a newer yadgar version.",
        description=(
            "Probes PyPI for the latest yadgar version and prints the upgrade command,\n"
            "or runs the routine-upgrade orchestrator (--install, gated by config).\n\n"
            "Operator recovery: --rollback [--snapshot <path>].\n"
            "Orchestrator re-exec target: --finalize --snapshot <path> (internal-use)."
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        default=False,
        help="Probe PyPI and print upgrade command (default when no flag given).",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        default=False,
        help=(
            "Run the routine-upgrade orchestrator. "
            "Requires update.install_enabled=true in ~/.yadgar/config.yaml."
        ),
    )
    parser.add_argument(
        "--finalize",
        action="store_true",
        default=False,
        help=(
            "Verify daemon is on target version, write DONE to snapshot, release lock. "
            "Invoked by the orchestrator via os.execvp — not intended for direct operator use."
        ),
    )
    parser.add_argument(
        "--rollback",
        action="store_true",
        default=False,
        help=(
            "Operator recovery: restore prev_image_tag, restart daemon, health-check. "
            "Does NOT revert CLI (operators pin manually: pipx install --force yadgar==<prev>). "
            "Rolling back the CLI risks the same self-replacement edge cases the orchestrator "
            "avoids via re-exec; operators should pin the CLI manually after this command."
        ),
    )
    parser.add_argument(
        "--snapshot",
        metavar="PATH",
        default=None,
        help="Snapshot directory path (required for --finalize; optional for --rollback).",
    )
    parser.add_argument(
        "--target-version",
        metavar="VERSION",
        default=None,
        dest="target_version",
        help="Pin target version for --install (default: latest from PyPI).",
    )
    parser.set_defaults(func=cmd_update)


def cmd_update(args: argparse.Namespace) -> None:
    """Dispatch to the appropriate update subcommand."""
    if args.rollback:
        _cmd_rollback(args)
        return
    if args.finalize:
        _cmd_finalize(args)
        return
    if args.install:
        _cmd_install(args)
        return
    _cmd_check(args)


# ---------------------------------------------------------------------------
# --check (original v5.48 behaviour, unchanged)
# ---------------------------------------------------------------------------


def _cmd_check(args: argparse.Namespace) -> None:  # noqa: ARG001
    """Probe PyPI and print upgrade command."""
    from yadgar import __version__  # noqa: PLC0415
    from yadgar.update import install_methods  # noqa: PLC0415
    from yadgar.update.check import probe_latest_version  # noqa: PLC0415

    method = install_methods.detect_install_method()
    cmd = install_methods.upgrade_command(method)
    can_self = install_methods.can_self_install(method)

    print(f"yadgar {__version__}")
    print(f"Install method: {method}")

    try:
        result = probe_latest_version()
    except Exception as exc:  # noqa: BLE001
        print(f"Warning: could not check for updates — {exc}", file=sys.stderr)
        print(f"Upgrade command: {cmd}")
        sys.exit(0)

    available = result.available_version
    update_available = available != __version__

    if update_available:
        print(f"Update available: {available}")
        print(f"Release notes: https://pypi.org/project/yadgar/{available}/")
    else:
        print(f"Up to date ({__version__})")

    if not can_self:
        print("Manual upgrade required:")
    else:
        print("Upgrade command:")

    print(f"  {cmd}")
    sys.exit(0)


# ---------------------------------------------------------------------------
# --install
# ---------------------------------------------------------------------------


def _install_exit(result: object) -> None:  # type: ignore[override]
    """Map OrchestratorResult to print + sys.exit. Extracts msg+code from result."""
    from yadgar.update.orchestrator import OrchestratorResult  # noqa: PLC0415

    assert isinstance(result, OrchestratorResult)
    msg, code = _install_msg_code(result)
    if msg:
        print(msg, file=sys.stderr)
    sys.exit(code)


def _install_msg_code(result: object) -> tuple[str, int]:  # noqa: PLR0911
    """Return (stderr_message, exit_code) for an OrchestratorResult."""
    from yadgar.update.orchestrator import OrchestratorResult, OrchestratorState  # noqa: PLC0415

    assert isinstance(result, OrchestratorResult)
    fs = result.final_state
    S = OrchestratorState  # noqa: N806 — local alias for brevity

    if fs in (S.RE_EXECING, S.DONE):
        return ("", 0)
    if fs == S.ROLLED_BACK_OK:
        return ("Upgrade rolled back to previous version.", 1)
    if fs == S.ROLLED_BACK_FAILED:
        return ("ROLLBACK FAILED. Manual recovery required: yadgar update --rollback", 2)
    if fs == S.DONE_CLI_ROLLBACK_OK:
        return ("Daemon upgraded; CLI revert succeeded.", 1)
    if fs == S.DONE_CLI_ROLLBACK_FAILED:
        return (
            f"Daemon upgraded; CLI revert FAILED. "
            f"Run pipx install --force yadgar=={result.from_version} manually.",
            2,
        )
    if fs == S.IDLE and result.error and "disabled" in result.error.lower():
        return (
            "yadgar update --install is disabled.\n"
            "To opt in: set update.install_enabled: true in ~/.yadgar/config.yaml\n"
            "See docs/PLAN_V5_49_0.md § Rollout for prerequisites and risks.",
            3,
        )
    return (f"Update failed: {result.error}" if result.error else "", 1)


def _cmd_install(args: argparse.Namespace) -> None:
    """Run the routine-upgrade orchestrator."""
    from yadgar.update.orchestrator import run_install  # noqa: PLC0415

    result = run_install(target_version=getattr(args, "target_version", None) or None)
    _install_exit(result)


# ---------------------------------------------------------------------------
# --finalize
# ---------------------------------------------------------------------------


def _cmd_finalize(args: argparse.Namespace) -> None:
    """Verify daemon is on target version; write DONE entry; release lock."""
    if not args.snapshot:
        print("--finalize requires --snapshot <path>", file=sys.stderr)
        sys.exit(4)

    snap_path = Path(args.snapshot)
    if not snap_path.is_dir():
        print(f"Snapshot directory not found: {snap_path}", file=sys.stderr)
        sys.exit(4)

    # Read target version from snapshot
    target_version_file = snap_path / "target_version"
    if not target_version_file.exists():
        print(
            f"Snapshot at {snap_path} is missing target_version file.",
            file=sys.stderr,
        )
        sys.exit(4)
    target_version = target_version_file.read_text(encoding="utf-8").strip()

    # Check CLI version matches (best-effort; orchestrator re-execs the new CLI)
    cli_version = _get_cli_version()

    # Probe daemon /health for reported version
    daemon_version = _probe_daemon_version()

    log_path = snap_path / "forward_log.json"

    if daemon_version == target_version:
        # Success path
        _append_log(log_path, "done", {"version": daemon_version})
        # Release lock
        try:
            _DEFAULT_LOCK_PATH.unlink(missing_ok=True)
        except OSError as exc:
            import logging  # noqa: PLC0415

            logging.getLogger(__name__).warning("Failed to release lock: %s", exc)
        print(f"Upgrade verified. Daemon on {daemon_version}.")
        sys.exit(0)
    else:
        # Failure path — do NOT release lock
        _append_log(
            log_path,
            "done_but_finalize_failed",
            {
                "observed_version": daemon_version,
                "expected_version": target_version,
                "cli_version": cli_version,
            },
        )
        print(
            f"Finalize failed. Daemon at {daemon_version!r}, expected {target_version!r}. "
            f"Run `yadgar update --rollback` to revert.",
            file=sys.stderr,
        )
        sys.exit(4)


# ---------------------------------------------------------------------------
# --rollback
# ---------------------------------------------------------------------------


def _cmd_rollback(args: argparse.Namespace) -> None:
    """Restore daemon to previous image tag, restart, health-check."""
    from yadgar.update.snapshot import latest_snapshot  # noqa: PLC0415

    # Determine snapshot
    if args.snapshot:
        snap_path = Path(args.snapshot)
        if not snap_path.is_dir():
            print(f"Snapshot directory not found: {snap_path}", file=sys.stderr)
            sys.exit(5)
        from datetime import UTC, datetime  # noqa: PLC0415

        from yadgar.update.snapshot import Snapshot  # noqa: PLC0415

        snap = Snapshot(path=snap_path, created_at=datetime.now(tz=UTC))
    else:
        snap = latest_snapshot(base_dir=_DEFAULT_SNAPSHOTS_BASE_DIR)

    if snap is None:
        print("No upgrade snapshot found. Nothing to roll back.")
        sys.exit(0)

    prev_image_tag = snap.read_prev_image_tag()
    if not prev_image_tag:
        print("Snapshot missing prev_image_tag. Cannot roll back.", file=sys.stderr)
        sys.exit(5)

    # Atomic-rewrite upgrade.env
    _write_upgrade_env(_DEFAULT_UPGRADE_ENV_PATH, prev_image_tag)

    # Restart daemon
    _restart_daemon()

    # Health-check: verify version
    daemon_version = _probe_daemon_version()
    prev_cli_version = snap.read_prev_cli_version() or ""

    # Append rollback log entry
    rollback_log_path = snap.path / "rollback_log.json"
    _append_log(
        rollback_log_path,
        "rolled_back_ok" if daemon_version else "rolled_back_failed",
        {"prev_image_tag": prev_image_tag, "daemon_version": daemon_version},
    )

    if not daemon_version:
        print(
            "Rollback restart did not return healthy daemon. Manual recovery needed.",
            file=sys.stderr,
        )
        sys.exit(5)

    print(f"Daemon rolled back to {prev_image_tag}.")
    if prev_cli_version:
        print(f"CLI rollback is manual. If needed: pipx install --force yadgar=={prev_cli_version}")
    sys.exit(0)


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


def _get_cli_version() -> str:
    """Return the current CLI version string."""
    try:
        from importlib.metadata import version  # noqa: PLC0415

        return version("yadgar")
    except Exception:  # noqa: BLE001
        return "unknown"


def _probe_daemon_version() -> str:
    """Probe /health and return daemon version string, or '' on failure."""
    try:
        from yadgar.config import get_settings  # noqa: PLC0415

        settings = get_settings()
        url = f"http://{settings.HOST}:{settings.PORT}/health"
    except Exception:  # noqa: BLE001
        url = "http://localhost:8765/health"

    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("version", "")
    except Exception:  # noqa: BLE001
        return ""


def _append_log(log_path: Path, state: str, detail: dict | None = None) -> None:
    """Read-append-write a JSON log file."""
    import os  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    from datetime import UTC, datetime  # noqa: PLC0415

    entries: list[dict] = []
    if log_path.exists():
        try:
            entries = json.loads(log_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            entries = []
    entries.append(
        {
            "ts": datetime.now(tz=UTC).isoformat(),
            "state": state,
            "detail": detail,
        }
    )
    content = json.dumps(entries, indent=2)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(dir=log_path.parent, prefix="tmp")
    try:
        os.fchmod(tmp_fd, 0o600)
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_name, log_path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _write_upgrade_env(env_path: Path, full_tag: str) -> None:
    """Atomically rewrite upgrade.env with the given full image tag."""
    import os  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    content = f"YADGAR_IMAGE_TAG={full_tag}\n"
    env_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(dir=env_path.parent, prefix="tmp")
    try:
        os.fchmod(tmp_fd, 0o600)
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_name, env_path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _restart_daemon() -> None:
    """Restart the yadgar daemon. Tries systemd first, falls back to CLI."""
    import shutil  # noqa: PLC0415

    if shutil.which("systemctl"):
        try:
            subprocess.run(
                ["systemctl", "--user", "restart", "yadgar.service"],
                check=True,
            )
            return
        except subprocess.CalledProcessError:
            pass
    # Fallback: yadgar daemon restart
    subprocess.run(["yadgar", "daemon", "restart"], check=True)
