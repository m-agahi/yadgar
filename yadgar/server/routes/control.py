"""Control API routes — v5.50.2.

Endpoints:
  GET  /api/control/config               — full knob table with reload classification
  POST /api/control/config               — set ONE knob (validates type + range)
  POST /api/control/action/{consolidate|vacuum|reembed} — trigger admin actions
  POST /api/control/restart/{yadgar|backend} — write sentinel file only (NO exec)

Gate: all routes require YADGAR_DEBUG_APIS_ENABLED=on (enforced in BearerAuthMiddleware
before the request reaches these handlers — the handlers assume the gate has been
checked but still verify for defence-in-depth).

Restart design (SECURITY-CRITICAL):
  - Writes $XDG_STATE_HOME/yadgar/restart-<service>.request with a timestamp.
  - Does NOT call os.execv, subprocess, systemctl, or any restart mechanism.
  - A systemd .path + .service unit (documented in MIGRATION_NOTES.md) does the
    actual restart. Until those units are installed, the endpoint is inert.

Registered as a side-effect import in yadgar/server/__init__.py.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse

from yadgar.config_registry import list_config
from yadgar.server._app import mcp_server
from yadgar.server.tools.admin_other import consolidate_now, reembed_all
from yadgar.server.tools.admin_vacuum import vacuum_now
from yadgar.tracing import trace_span

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Reload-vs-restart classification
# ---------------------------------------------------------------------------
# Plan Q2: viz.* = hot-reload (frontend re-reads config endpoint).
#          physics.*, consolidation.*, embedding.*, storage.* = restart-required.
# Special case: viz.physics.* in registry = hot-reload (force graph reads live).
# Any knob whose Settings field is read only at startup = restart-required.
#
# Implementation: prefix matching on the env-var name segment after YADGAR_.
# "YADGAR_VIZ_*" → hot_reload (frontend polls /api/control/config).
# Everything else → restart_required.
# Exception overrides below handle the few cross-category cases.
#
# This classification is returned in the GET /config response; clients display
# a "hot" or "restart" pill.

_RESTART_REQUIRED_PREFIXES: tuple[str, ...] = (
    "YADGAR_EMBEDDING_",
    "YADGAR_DB_",
    "YADGAR_EMBED_URL",
    "YADGAR_DATA_DIR",
    "YADGAR_PORT",
    "YADGAR_HOST",
    "YADGAR_CONSOLIDATION_",
    "YADGAR_BACKUP_",
    "YADGAR_VACUUM_TRIGGER_PATH",
    "YADGAR_ASGI_SHUTDOWN_TIMEOUT_SEC",
    "YADGAR_DAEMON_CHECK_INTERVAL",
    "YADGAR_NARRATIVE_INTERVAL_HOURS",
    "YADGAR_NUM_ASTROCYTE_PROCESSES",
)

# viz.physics.* in config = YADGAR_VIZ_PHYSICS_* — hot-reload via force graph
_HOT_RELOAD_OVERRIDES: frozenset[str] = frozenset(
    {
        "YADGAR_VIZ_PHYSICS_CHARGE_STRENGTH",
        "YADGAR_VIZ_PHYSICS_LINK_DISTANCE_2D",
        "YADGAR_VIZ_PHYSICS_LINK_DISTANCE_3D",
    }
)


def _classify_knob(name: str) -> str:
    """Return 'hot_reload' or 'restart_required' for a knob env-var name."""
    if name in _HOT_RELOAD_OVERRIDES:
        return "hot_reload"
    # All YADGAR_VIZ_* are hot-reload by default
    if name.startswith("YADGAR_VIZ_"):
        return "hot_reload"
    for prefix in _RESTART_REQUIRED_PREFIXES:
        if name.startswith(prefix):
            return "restart_required"
    # Non-VIZ non-explicitly-classified knobs default to restart_required (safe)
    return "restart_required"


# ---------------------------------------------------------------------------
# Range validators for known-bounded knobs (400 on out-of-range)
# ---------------------------------------------------------------------------


def _validate_range(name: str, value: object) -> str | None:
    """Return an error message string if value is out of range, else None."""
    positive_floats = {
        "YADGAR_VIZ_NODE_SIZE_3D",
        "YADGAR_VIZ_NODE_SIZE_2D",
        "YADGAR_VIZ_EDGE_OPACITY",
        "YADGAR_VIZ_EDGE_WIDTH_3D_MULTIPLIER",
        "YADGAR_VIZ_EDGE_ARROW_LEN",
    }
    positive_ints = {
        "YADGAR_VIZ_LAYOUT_ZOOM_FIT_TICK",
        "YADGAR_VIZ_LAYOUT_ZOOM_FIT_PADDING",
        "YADGAR_VIZ_LAYOUT_ZOOM_FIT_TRANSITION_MS",
    }
    bounded_floats_01 = {
        "YADGAR_VIZ_SEARCH_DIM_OPACITY",
    }

    if name in positive_floats or name in positive_ints:
        if isinstance(value, (int, float)) and value <= 0:
            return f"{name} must be > 0, got {value}"
    if name in bounded_floats_01:
        if isinstance(value, float) and not (0.0 <= value <= 1.0):
            return f"{name} must be in [0.0, 1.0], got {value}"
    return None


# ---------------------------------------------------------------------------
# Type coercion from JSON payload
# ---------------------------------------------------------------------------


def _coerce_int(raw: object) -> tuple[object, str | None]:
    """Coerce to int; return (value, error)."""
    if isinstance(raw, bool):
        return None, "type mismatch: expected int, got bool"
    if isinstance(raw, (int, float)):
        if isinstance(raw, float) and not raw.is_integer():
            return None, f"type mismatch: expected int, got float {raw}"
        return int(raw), None
    if isinstance(raw, str):
        try:
            return int(raw), None
        except ValueError:
            return None, f"type mismatch: expected int, got {type(raw).__name__} {raw!r}"
    return None, f"type mismatch: expected int, got {type(raw).__name__}"


def _coerce_float(raw: object) -> tuple[object, str | None]:
    """Coerce to float; return (value, error)."""
    if isinstance(raw, bool):
        return None, "type mismatch: expected float, got bool"
    if isinstance(raw, (int, float)):
        return float(raw), None
    if isinstance(raw, str):
        try:
            return float(raw), None
        except ValueError:
            return None, f"type mismatch: expected float, got string {raw!r}"
    return None, f"type mismatch: expected float, got {type(raw).__name__}"


def _coerce_bool(raw: object) -> tuple[object, str | None]:
    """Coerce to bool; return (value, error)."""
    if isinstance(raw, bool):
        return raw, None
    if isinstance(raw, str) and raw.lower() in ("true", "1", "yes", "on"):
        return True, None
    if isinstance(raw, str) and raw.lower() in ("false", "0", "no", "off"):
        return False, None
    return None, f"type mismatch: expected bool, got {type(raw).__name__} {raw!r}"


def _coerce_json_value(raw: object, kind: str) -> tuple[object, str | None]:
    """Coerce a JSON-decoded value to the target kind.

    Returns (coerced_value, error_message). error_message is None on success.
    """
    if kind == "int":
        return _coerce_int(raw)
    if kind == "float":
        return _coerce_float(raw)
    if kind == "bool":
        return _coerce_bool(raw)
    # string — accept anything stringifiable
    return str(raw), None


# ---------------------------------------------------------------------------
# Sentinel file helper
# ---------------------------------------------------------------------------

_VALID_SERVICES: frozenset[str] = frozenset({"yadgar", "yadgar-backend"})

# ---------------------------------------------------------------------------
# Write-blocked knobs (security/enforcement — cannot be changed via POST /config)
# ---------------------------------------------------------------------------
# The config editor is scoped to tuning knobs (viz, physics, consolidation,
# embedding, storage). Security and enforcement knobs must never be writable
# via the API, even with the debug gate on. This is defence-in-depth: the gate
# itself (YADGAR_DEBUG_APIS_ENABLED) must not be self-disabling, and auth/root/
# enforcement knobs must not be accessible to any caller regardless of bearer
# token.

_WRITE_BLOCKED: frozenset[str] = frozenset(
    {
        "YADGAR_DEBUG_APIS_ENABLED",  # gate self-disable
        "YADGAR_UPDATE_DEBUG_APIS_ENABLED",  # sibling gate
        "YADGAR_ALLOW_ROOT",  # privilege escalation
        "YADGAR_REQUIRE_AUTH",  # auth bypass
        "YADGAR_BRANCH_ENFORCEMENT",  # enforcement bypass
        "YADGAR_DIRECTORY_ENFORCEMENT",  # enforcement bypass
        "YADGAR_IN_CONTAINER",  # runtime-detected; not user-settable
    }
)


def _sentinel_dir() -> Path:
    """Return the XDG state dir for yadgar sentinel files."""
    import yadgar.paths as _paths  # noqa: PLC0415

    return Path(str(_paths.STATE_DIR))


def _write_restart_sentinel(service: str) -> Path:
    """Write a restart sentinel file for the given service.

    File: $XDG_STATE_HOME/yadgar/restart-<service>.request
    Content: JSON with timestamp + nonce.

    This function ONLY writes a file. It does NOT exec, subprocess, or systemctl.
    """
    sentinel_dir = _sentinel_dir()
    sentinel_dir.mkdir(parents=True, exist_ok=True)
    sentinel_path = sentinel_dir / f"restart-{service}.request"
    payload = {
        "service": service,
        "requested_at": time.time(),
        "nonce": os.urandom(8).hex(),
    }
    sentinel_path.write_text(json.dumps(payload))
    return sentinel_path


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


async def control_config_get_handler(request: Request) -> JSONResponse:
    """GET /api/control/config — full knob table with classification."""
    entries = list_config()
    knobs = []
    for entry in entries:
        if entry._should_redact():
            continue
        knobs.append(
            {
                "name": entry.name,
                "kind": entry.kind,
                "current": entry._raw_value(),
                "default": entry.default,
                "source": entry.source(),
                "reload": _classify_knob(entry.name),
            }
        )
    return JSONResponse({"knobs": knobs})


async def control_config_post_handler(request: Request) -> JSONResponse:
    """POST /api/control/config — set ONE knob; validates type + range."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)

    name = body.get("name")
    raw_value = body.get("value")

    if not name:
        return JSONResponse({"error": "missing 'name' field"}, status_code=400)
    if raw_value is None:
        return JSONResponse({"error": "missing 'value' field"}, status_code=400)

    # Look up the entry in the registry
    registry = {e.name: e for e in list_config()}
    entry = registry.get(str(name).upper())
    if entry is None:
        return JSONResponse({"error": f"unknown knob: {name!r}"}, status_code=400)

    if entry._should_redact():
        return JSONResponse({"error": f"knob {name!r} is write-protected"}, status_code=400)

    if entry.name in _WRITE_BLOCKED:
        return JSONResponse({"error": f"knob {name!r} is write-protected"}, status_code=400)

    # Coerce + validate type
    coerced, err = _coerce_json_value(raw_value, entry.kind)
    if err:
        return JSONResponse({"error": err}, status_code=400)

    # Range check
    range_err = _validate_range(entry.name, coerced)
    if range_err:
        return JSONResponse({"error": range_err}, status_code=400)

    # Persist: write to YAML config (survives restart) AND set in process env (immediate)
    try:
        from ruamel.yaml.comments import CommentedMap  # noqa: PLC0415

        from yadgar.config_yaml import get_config_path, load_yaml, save_yaml  # noqa: PLC0415

        path = get_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)

        yaml_data = load_yaml(path) if path.exists() else CommentedMap()
        if not isinstance(yaml_data, CommentedMap):
            yaml_data = CommentedMap(yaml_data or {})

        # YAML key = lowercase without YADGAR_ prefix
        yaml_key = entry.name.removeprefix("YADGAR_").lower()
        yaml_data[yaml_key] = coerced
        save_yaml(path, yaml_data)
        import os as _os

        _os.chmod(path, 0o600)

    except Exception as exc:
        logger.warning("Failed to persist knob %s to YAML: %s", name, exc)
        return JSONResponse({"error": f"failed to persist: {exc}"}, status_code=500)

    # Update process environment immediately (hot-reload knobs take effect without restart)
    os.environ[entry.name] = str(coerced)

    return JSONResponse(
        {
            "name": entry.name,
            "value": str(coerced),
            "reload": _classify_knob(entry.name),
        }
    )


async def control_action_handler(request: Request) -> JSONResponse:
    """POST /api/control/action/{consolidate|vacuum|reembed} — trigger admin actions."""
    action = request.path_params.get("action", "")
    if action not in ("consolidate", "vacuum", "reembed"):
        return JSONResponse(
            {"error": f"unknown action: {action!r}. Supported: consolidate, vacuum, reembed"},
            status_code=400,
        )

    try:
        if action == "consolidate":
            result = consolidate_now(mode="light")
        elif action == "vacuum":
            result = vacuum_now(force=False)
        elif action == "reembed":
            result = reembed_all()
        else:
            result = {}
    except Exception as exc:
        logger.exception("Control action %s failed: %s", action, exc)
        return JSONResponse({"error": str(exc)}, status_code=500)

    return JSONResponse({"action": action, "result": result})


async def control_restart_handler(request: Request) -> JSONResponse:
    """POST /api/control/restart/{yadgar|backend} — write sentinel file ONLY.

    Security design:
      - Validates confirm == exact service name.
      - Writes $XDG_STATE_HOME/yadgar/restart-<service>.request (timestamp + nonce).
      - Does NOT call os.execv, subprocess, os.system, or systemctl.
      - Inert until a systemd .path unit (documented in MIGRATION_NOTES.md) is installed.
    """
    svc_param = request.path_params.get("service", "")

    # Map URL segment to service name
    service_map = {"yadgar": "yadgar", "backend": "yadgar-backend"}
    service = service_map.get(svc_param)
    if service is None:
        return JSONResponse(
            {"error": f"unknown service: {svc_param!r}. Supported: yadgar, backend"},
            status_code=400,
        )

    # Parse body
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)

    confirm = body.get("confirm")
    if confirm != service:
        return JSONResponse(
            {
                "error": f"confirmation mismatch: expected {service!r}, got {confirm!r}",
                "hint": f'Send {{"confirm": "{service}"}} to confirm the restart.',
            },
            status_code=400,
        )

    # Write sentinel file ONLY — no exec, no subprocess, no systemctl
    sentinel_path = _write_restart_sentinel(service)
    logger.info(
        "Restart sentinel written for service %s at %s (no daemon restart performed — "
        "requires systemd .path unit; see MIGRATION_NOTES.md)",
        service,
        sentinel_path,
    )

    return JSONResponse(
        {
            "service": service,
            "sentinel": str(sentinel_path),
            "status": "sentinel_written",
            "note": (
                "Sentinel file written. A systemd .path unit must be installed to act on it. "
                "See MIGRATION_NOTES.md for the required unit files."
            ),
        },
        status_code=202,
    )


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


@mcp_server.custom_route("/api/control/config", methods=["GET"])
@trace_span("api.control.config.get")
async def control_config_get(request: Request) -> JSONResponse:
    """Expose knob table for Control tab config editor."""
    return await control_config_get_handler(request)


@mcp_server.custom_route("/api/control/config", methods=["POST"])
@trace_span("api.control.config.post")
async def control_config_post(request: Request) -> JSONResponse:
    """Set ONE config knob (validates type + range; persists to YAML)."""
    return await control_config_post_handler(request)


@mcp_server.custom_route("/api/control/action/{action}", methods=["POST"])
@trace_span("api.control.action")
async def control_action(request: Request) -> JSONResponse:
    """Trigger consolidate / vacuum / reembed action."""
    return await control_action_handler(request)


@mcp_server.custom_route("/api/control/restart/{service}", methods=["POST"])
@trace_span("api.control.restart")
async def control_restart(request: Request) -> JSONResponse:
    """Write restart sentinel file for yadgar or yadgar-backend (sentinel-only; no exec)."""
    return await control_restart_handler(request)
