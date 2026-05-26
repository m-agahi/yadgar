"""Service control abstraction for yadgar vacuum.

Wraps systemd, docker-compose, and manual (no-op / print-only) modes so
that cmd_vacuum can stop/start daemons without knowing which init system is
running.

Auto-detection priority:
1. INVOCATION_ID env var → systemd
2. Path("/.dockerenv").exists() → docker
3. Fallback → manual

Override: pass --service-mode={systemd,docker,manual} on the CLI.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def detect_service_mode() -> str:
    """Auto-detect the init system and return 'systemd', 'docker', or 'manual'."""
    if os.environ.get("INVOCATION_ID"):
        return "systemd"
    if Path("/.dockerenv").exists():
        return "docker"
    return "manual"


class ServiceController:
    """Stop and start the yadgar + yadgar-backend services.

    Args:
        mode: 'systemd' | 'docker' | 'manual'
    """

    SERVICES = ("yadgar", "yadgar-backend")

    def __init__(self, mode: str) -> None:
        if mode not in ("systemd", "docker", "manual"):
            raise ValueError(f"Unknown service mode: {mode!r}")
        self.mode = mode

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def stop(self) -> None:
        """Stop both yadgar and yadgar-backend services."""
        if self.mode == "systemd":
            self._systemctl("stop", *self.SERVICES)
        elif self.mode == "docker":
            self._docker_compose("stop", *self.SERVICES)
        else:
            self._manual_instructions("stop")

    def stop_backend(self) -> None:
        """Stop yadgar-backend only (used by restore path in vacuum phase 3)."""
        if self.mode == "systemd":
            self._systemctl("stop", "yadgar-backend")
        elif self.mode == "docker":
            self._docker_compose("stop", "yadgar-backend")
        else:
            self._manual_instructions("stop-backend")

    def start_backend(self) -> None:
        """Start yadgar-backend only."""
        if self.mode == "systemd":
            self._systemctl("start", "yadgar-backend")
        elif self.mode == "docker":
            self._docker_compose("start", "yadgar-backend")
        else:
            self._manual_instructions("start-backend")

    def start_yadgar(self) -> None:
        """Start yadgar (MCP layer) only."""
        if self.mode == "systemd":
            self._systemctl("start", "yadgar")
        elif self.mode == "docker":
            self._docker_compose("start", "yadgar")
        else:
            self._manual_instructions("start-yadgar")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _systemctl(self, action: str, *services: str) -> None:
        cmd = ["systemctl", "--user", action, *services]
        result = subprocess.run(cmd, capture_output=True)
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace").strip()
            raise RuntimeError(f"systemctl --user {action} {' '.join(services)} failed: {stderr}")

    def _docker_compose(self, action: str, *services: str) -> None:
        cmd = ["docker", "compose", action, *services]
        result = subprocess.run(cmd, capture_output=True)
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace").strip()
            raise RuntimeError(f"docker compose {action} {' '.join(services)} failed: {stderr}")

    def _manual_instructions(self, step: str) -> None:
        """Print manual instructions and exit non-zero.

        Manual mode is NOT a silent no-op — the daemons must actually be
        stopped/started for vacuum to work. The caller must run the printed
        commands before re-running vacuum with --service-mode=manual.
        """
        print(
            "\n[vacuum] Running in manual service mode. "
            "Execute the following commands before continuing:\n",
            file=sys.stderr,
        )
        if step == "stop":
            print(
                "  systemctl --user stop yadgar yadgar-backend\n"
                "  # OR: docker compose stop yadgar yadgar-backend\n",
                file=sys.stderr,
            )
        elif step == "stop-backend":
            print(
                "  systemctl --user stop yadgar-backend\n"
                "  # OR: docker compose stop yadgar-backend\n",
                file=sys.stderr,
            )
        elif step == "start-backend":
            print(
                "  systemctl --user start yadgar-backend\n"
                "  # OR: docker compose start yadgar-backend\n",
                file=sys.stderr,
            )
        elif step == "start-yadgar":
            print(
                "  systemctl --user start yadgar\n  # OR: docker compose start yadgar\n",
                file=sys.stderr,
            )
        # In manual mode the caller is responsible for the step; we do NOT exit
        # here because the test harness (and manual --yes runs) patch this class.
        # Raise so the caller can decide to abort or skip.
        raise ManualModeError(
            f"manual service mode: step={step!r} — run the printed commands and retry"
        )


class ManualModeError(RuntimeError):
    """Raised when ServiceController is in manual mode and cannot auto-act."""


# ---------------------------------------------------------------------------
# Vacuum service fire helper (used by vacuum_now() MCP tool and
# ConsolidationScheduler auto-trigger)
# ---------------------------------------------------------------------------


_DEFAULT_VACUUM_TRIGGER_PATH = "/data/triggers/vacuum_requested"


def _fire_vacuum_service() -> Path:
    """Write a trigger file requesting yadgar-vacuum.service to run.

    The trigger file path is read from YADGAR_VACUUM_TRIGGER_PATH (default:
    /data/triggers/vacuum_requested).  A systemd path-watch unit on the host
    watches the file and starts yadgar-vacuum.service when it appears; it then
    removes the file.  This replaces the old 'systemctl --user start --no-block'
    call which cannot cross the container ↔ host boundary.

    Write is atomic: content is written to <path>.tmp then os.replace()d to <path>.

    Returns the Path of the written trigger file.
    Raises RuntimeError on I/O failure.
    """
    import datetime
    import json as _json

    trigger_path = Path(os.environ.get("YADGAR_VACUUM_TRIGGER_PATH", _DEFAULT_VACUUM_TRIGGER_PATH))
    tmp_path = Path(str(trigger_path) + ".tmp")

    payload = _json.dumps(
        {
            "requested_at": datetime.datetime.now(datetime.UTC).isoformat(),
            "source": "vacuum_now",
        }
    )

    try:
        trigger_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(payload)
        os.replace(tmp_path, trigger_path)
    except OSError as exc:
        raise RuntimeError(f"Failed to write vacuum trigger file {trigger_path}: {exc}") from exc

    return trigger_path
