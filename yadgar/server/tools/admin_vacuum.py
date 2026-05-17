"""vacuum_now MCP tool."""

from __future__ import annotations

import logging

from yadgar.server._app import _tool

logger = logging.getLogger(__name__)


@_tool(power=True)
def vacuum_now(force: bool = False) -> dict:
    """Trigger a SurrealKV vacuum. Daemon downtime ~2-5 min on a 500 MB DB.

    power=True: vacuum stops the daemon, any active MCP session loses its connection.

    Returns:
        {
            "started": bool,
            "service_unit": "yadgar-vacuum.service",
            "before_bytes": int,
            "skipped_reason": str | None,
        }
    """
    import subprocess as _subprocess
    import sys as _sys

    from yadgar.ops import _fire_vacuum_service, detect_service_mode

    # Look up _get_storage via yadgar.server so patch.object(srv, "_get_storage", ...)
    # in tests takes effect (v4.x patching contract restored after server split).
    _srv = _sys.modules.get("yadgar.server")
    if _srv is not None and hasattr(_srv, "_get_storage"):
        _get_storage_fn = _srv._get_storage
    else:
        from yadgar.server.lifecycle import _get_storage as _get_storage_fn  # noqa: PLC0415
    storage = _get_storage_fn()
    db_size_info = storage.get_db_size()
    before_bytes = db_size_info.get("db_size_bytes", 0)

    _MIB_200 = 200 * 1024 * 1024

    # Refuse if DB is too small (no point vacuuming) unless forced
    if before_bytes < _MIB_200 and not force:
        return {
            "started": False,
            "service_unit": "yadgar-vacuum.service",
            "before_bytes": before_bytes,
            "skipped_reason": "db_below_threshold",
        }

    # Refuse if no supported service manager
    mode = detect_service_mode()
    if mode in ("manual", "docker"):
        _shell_cmd = (
            "docker compose run --rm yadgar vacuum"
            if mode == "docker"
            else "yadgar vacuum --service-mode=manual"
        )
        return {
            "started": False,
            "service_unit": "yadgar-vacuum.service",
            "before_bytes": before_bytes,
            "skipped_reason": "no_supported_service_manager",
            "shell_command": _shell_cmd,
        }

    # Refuse if vacuum service is already running (active or activating)
    try:
        out = _subprocess.check_output(
            ["systemctl", "--user", "is-active", "yadgar-vacuum.service"],
            stderr=_subprocess.DEVNULL,
        )
        state = out.decode(errors="replace").strip()
        if state in ("active", "activating"):
            return {
                "started": False,
                "service_unit": "yadgar-vacuum.service",
                "before_bytes": before_bytes,
                "skipped_reason": "vacuum_already_running",
            }
    except FileNotFoundError:
        # systemctl binary not available — treat as no supported service manager
        return {
            "started": False,
            "service_unit": "yadgar-vacuum.service",
            "before_bytes": before_bytes,
            "skipped_reason": "no_supported_service_manager",
            "shell_command": "yadgar vacuum --service-mode=manual",
        }
    except _subprocess.CalledProcessError:
        # is-active returns non-zero for inactive/failed — that's fine, proceed
        pass

    # Fire
    _fire_vacuum_service()

    return {
        "started": True,
        "service_unit": "yadgar-vacuum.service",
        "before_bytes": before_bytes,
        "skipped_reason": None,
    }
