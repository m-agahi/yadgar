"""vacuum_now MCP tool."""

from __future__ import annotations

import logging

from yadgar.core.server._app import _tool

logger = logging.getLogger(__name__)


@_tool(power=True)
def vacuum_now(force: bool = False) -> dict:
    """Trigger a SurrealKV vacuum. Daemon downtime ~2-5 min on a 500 MB DB.

    power=True: vacuum stops the daemon, any active MCP session loses its connection.

    Writes a trigger file to YADGAR_VACUUM_TRIGGER_PATH.  A host-side watcher
    unit (systemd .path on nix, launchd WatchPaths on macOS) picks this up and
    starts yadgar-vacuum.service.  This approach works whether yadgar runs on
    the host or inside a container.

    The env var has NO default: install surfaces that ship no watcher leave it
    unset, and this tool then returns started=False with
    skipped_reason="no_trigger_path_configured" rather than writing a trigger
    file nothing reads (task:0044).

    Returns:
        {
            "started": bool,
            "trigger_path": str | None,
            "before_bytes": int,
            "skipped_reason": str | None,
        }
    """
    import sys as _sys

    from yadgar.core.ops import VacuumTriggerNotConfiguredError, _fire_vacuum_service

    # Look up _get_storage via yadgar.server so patch.object(srv, "_get_storage", ...)
    # in tests takes effect (v4.x patching contract restored after server split).
    _srv = _sys.modules.get("yadgar.core.server")
    if _srv is not None and hasattr(_srv, "_get_storage"):
        _get_storage_fn = _srv._get_storage
    else:
        from yadgar._shared.runtime.lifecycle import (
            _get_storage as _get_storage_fn,  # noqa: PLC0415
        )
    storage = _get_storage_fn()
    db_size_info = storage.get_db_size()
    before_bytes = db_size_info.get("db_size_bytes", 0)

    _MIB_200 = 200 * 1024 * 1024

    # Refuse if DB is too small (no point vacuuming) unless forced
    if before_bytes < _MIB_200 and not force:
        return {
            "started": False,
            "trigger_path": None,
            "before_bytes": before_bytes,
            "skipped_reason": "db_below_threshold",
        }

    # Fire — write trigger file atomically.  No watcher on this surface ⇒ say so
    # instead of reporting started=True into a void (task:0044 D1).
    try:
        trigger_path = _fire_vacuum_service()
    except VacuumTriggerNotConfiguredError:
        logger.warning(
            "vacuum_now: YADGAR_VACUUM_TRIGGER_PATH unset — this install surface "
            "ships no vacuum trigger watcher; refusing to write a trigger nothing reads"
        )
        return {
            "started": False,
            "trigger_path": None,
            "before_bytes": before_bytes,
            "skipped_reason": "no_trigger_path_configured",
        }

    return {
        "started": True,
        "trigger_path": str(trigger_path),
        "before_bytes": before_bytes,
        "skipped_reason": None,
    }
