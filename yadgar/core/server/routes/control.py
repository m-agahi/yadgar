"""Control API routes — v5.85.0.

Endpoints:
  GET  /api/control/config               — full knob table with reload classification
                                           + enriched fields (description/section/category/locked)
  POST /api/control/config               — set ONE knob (validates type + range);
                                           returns 409 when knob is env-locked
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

Config write path (SINGLE SURFACE):
  - POST /api/control/config is the ONE sanctioned write path.
  - admin_config.py (GET /admin/config) is read-only and stays that way.
  - Env-locked knobs (source=='env') return 409 — a yaml write would be silently
    shadowed by the env var, so we refuse rather than create false confidence.

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

from yadgar._shared.config.config_registry import clear_config_caches, list_config
from yadgar._shared.observability.observe import observe
from yadgar._shared.observability.tracing import trace_span
from yadgar.core.server._app import mcp_server
from yadgar.core.server.routes.control_audit import (
    audit_config_event,
    is_destructive,
    restart_rate_limited,
    stamp_restart,
)
from yadgar.core.server.tools.admin_other import consolidate_now, reembed_all
from yadgar.core.server.tools.admin_vacuum import vacuum_now

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Section → capability category mapping (v5.85.0)
# ---------------------------------------------------------------------------
# Maps every FIELD_META.section to one of the 15 CAPABILITY_REGISTRY categories:
#   retrieval, storage, write-path, consolidation, enrichment, gate, wiki,
#   curation, mcp-tool, observability, security, ops, brain-dynamics, viz, config
#
# A section not listed here is a drift violation (test_section_category_map_covers_all
# catches it). Explicit fallback in _enrich_knob: 'config'.
#
# Maintenance rule: when a new FIELD_META section is added, update this map.

SECTION_TO_CATEGORY: dict[str, str] = {
    # core / daemon / ops
    "core": "ops",
    "daemon": "ops",
    "logging": "ops",
    "ops": "ops",
    "misc": "config",
    "update": "ops",
    "backend_cache": "ops",
    "backend_hot_path_cache": "ops",
    "backend_model_preload": "ops",
    "backend_timeouts": "ops",
    "stats_cache": "ops",
    "active_work_watchdog": "ops",
    "hooks": "ops",
    "cpu_burst_detection": "ops",
    "security": "ops",
    "vacuum": "ops",
    # memory lifecycle / write-path
    "memory_lifecycle": "write-path",
    "memory_archive_retention": "write-path",
    "cold_memory_retention": "write-path",
    "session_end_capture": "write-path",
    "table_retention": "write-path",
    "write_queue": "write-path",
    "memorize_similarity_gate": "write-path",
    # thermodynamics / brain-dynamics
    "thermodynamics": "brain-dynamics",
    "neuromorphic": "brain-dynamics",
    # retrieval
    "retrieval_fusion": "retrieval",
    "reranking": "retrieval",
    "query_routing": "retrieval",
    "temporal_retrieval": "retrieval",
    "embedding_enhancement": "retrieval",
    "graph_knowledge": "retrieval",
    "unified_recall": "retrieval",
    "recall_quality": "retrieval",
    "circuit_breaker": "retrieval",
    # enrichment
    "enrichment": "enrichment",
    "profiles_beliefs": "enrichment",
    "adversarial": "gate",
    # wiki
    "wiki_similarity_gate": "wiki",
    "wiki_staleness": "wiki",
    "wiki_write_wait": "wiki",
    "memory_blocks": "wiki",
    # observability
    "observability": "observability",
    # viz
    "viz_config": "viz",
    # project_brief / anchor
    "project_brief": "ops",
    "anchor_hygiene": "ops",
    "agent_prompt_library": "ops",
}


@observe(tier="stage")
def _get_category(section: str) -> str:
    """Return capability category for a FIELD_META section; fallback 'config'."""
    return SECTION_TO_CATEGORY.get(section, "config")


@observe(tier="stage")
def _serialize_knob_value(value: object) -> str:
    """Serialise a coerced knob value to its canonical string form.

    Booleans render lowercase (``"true"``/``"false"``) — the YAML/JSON
    convention — instead of Python's capitalized ``str(True)``. This keeps the
    POST response (and the env it writes) consistent with the GET read path,
    which surfaces lowercase env/default strings (ADR-0013 bool-display fix).
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


@observe(tier="stage")
def _enrich_knob(knob: dict, field_meta_key: str) -> dict:
    """Add description/section/category/locked fields to a knob dict in-place.

    field_meta_key: lowercase-no-prefix name (e.g. 'viz_node_size_3d').
    Mutates and returns the dict.
    """
    from yadgar._shared.config.config_yaml import (
        FIELD_META,  # noqa: PLC0415 — keep import at call site
    )

    meta = FIELD_META.get(field_meta_key, {})
    section = meta.get("section", "misc")
    knob["description"] = meta.get("desc", "")
    knob["section"] = section
    knob["category"] = _get_category(section)
    knob["locked"] = knob.get("source") == "env"
    # P4.1: enum_choices — allowed values for fixed-set string knobs (e.g.
    # log_format → json/text/human). Empty list for free-form/numeric knobs so
    # the config panel can branch on a <select> vs a free-text input.
    choices = meta.get("choices")
    knob["enum_choices"] = list(choices) if isinstance(choices, (list, tuple)) else []
    # Car D: destructive knobs (retention/purge/DLQ pruning) surface a flag so the
    # panel can render a warning + require a typed-confirm arm before writing.
    knob["destructive"] = bool(meta.get("destructive", False))
    return knob


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


@observe(tier="stage")
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


@observe(tier="stage")
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
# Type coercion: delegated to the shared writer (yadgar.config_yaml.coerce_value /
# set_config_value) — annotation-driven, identical to the CLI. The POST handler
# calls those directly; no local coercion helpers (avoids the divergent second
# write path the plan audit warned about).
# ---------------------------------------------------------------------------


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
        "YADGAR_IN_CONTAINER",  # runtime-detected; not user-settable
    }
)


def _sentinel_dir() -> Path:
    """Return the XDG state dir for yadgar sentinel files."""
    import yadgar._shared.paths as _paths  # noqa: PLC0415

    return Path(str(_paths.STATE_DIR))


@observe(tier="stage")
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


@observe(tier="boundary")
async def control_config_get_handler(request: Request) -> JSONResponse:
    """GET /api/control/config — full knob table with classification + enriched metadata."""
    entries = list_config()
    knobs = []
    for entry in entries:
        if entry._should_redact():
            continue
        source = entry.source()
        knob = {
            "name": entry.name,
            "kind": entry.kind,
            "current": entry._raw_value(),
            "default": entry.default,
            "source": source,
            "reload": _classify_knob(entry.name),
        }
        field_meta_key = entry.name.removeprefix("YADGAR_").lower()
        _enrich_knob(knob, field_meta_key)
        knobs.append(knob)
    return JSONResponse({"knobs": knobs})


@observe(tier="boundary")
async def control_config_post_handler(request: Request) -> JSONResponse:
    """POST /api/control/config — set ONE knob; validates type + range.

    Returns:
      200  — knob updated (yaml + env)
      400  — bad input / unknown knob / range violation / write-blocked
      409  — knob is env-locked (source=env); yaml write would be silently shadowed
      422  — value not coercible to the knob's type (type mismatch)
      500  — yaml persistence failed

    Coercion + persistence go through the shared yadgar.config_yaml writer
    (coerce_value + set_config_value) — the same path as `yadgar config set`.
    """
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

    # Car D: capture the pre-write value ONCE, live for every refusal audit below
    # (409 / 422 / 428 all return before the write). armed is echoed on each row.
    old = entry._raw_value()
    armed = body.get("armed")

    if entry._should_redact() or entry.name in _WRITE_BLOCKED:
        return JSONResponse({"error": f"knob {name!r} is write-protected"}, status_code=400)

    # Env-locked: yaml write would be silently shadowed by the env var — refuse with 409.
    if entry.source() == "env":
        audit_config_event("config_write", entry.name, old, raw_value, 409, request, armed=armed)
        return JSONResponse(
            {
                "error": (
                    f"knob {name!r} is env-locked (source=env): a yaml write would be "
                    "silently shadowed by the environment variable. "
                    "Unset the env var to allow yaml configuration."
                ),
                "source": "env",
                "locked": True,
            },
            status_code=409,
        )

    # Car D 428 armed gate — AFTER the security guards (write-blocked 400, env-lock
    # 409 must never be bypassed). A destructive knob (retention/purge/DLQ) needs an
    # explicit ``"armed": true`` in the body. Own FIELD_META lookup via is_destructive
    # (the POST path does not call _enrich_knob, so the GET-side flag is unavailable).
    if is_destructive(entry.name) and armed is not True:
        audit_config_event("config_write", entry.name, old, raw_value, 428, request, armed=armed)
        return JSONResponse(
            {
                "error": f"knob {name!r} is destructive and requires arming",
                "destructive": True,
                "hint": 'resend with "armed": true',
            },
            status_code=428,
        )

    # Coerce via the SHARED writer's annotation-driven coercion (identical path to
    # the CLI). Coercion failure → 422 (well-formed request, value not coercible).
    from yadgar._shared.config.config_yaml import coerce_value  # noqa: PLC0415 — call-site import

    yaml_key = entry.name.removeprefix("YADGAR_").lower()
    try:
        coerced = coerce_value(yaml_key, str(raw_value))
    except (ValueError, TypeError) as exc:
        audit_config_event("config_write", entry.name, old, raw_value, 422, request, armed=armed)
        return JSONResponse({"error": f"type mismatch: {exc}"}, status_code=422)

    # Range check (semantic bound, distinct from type coercion) → 400.
    range_err = _validate_range(entry.name, coerced)
    if range_err:
        return JSONResponse({"error": range_err}, status_code=400)

    # Persist via the SINGLE sanctioned writer (set_config_value) — same path as
    # `yadgar config set`. Never hand-write yaml here.
    try:
        from yadgar._shared.config.config_yaml import set_config_value  # noqa: PLC0415

        coerced = set_config_value(yaml_key, raw_value)
    except Exception as exc:  # noqa: BLE001 — surface yaml/io failures as 500
        logger.warning("Failed to persist knob %s to YAML: %s", name, exc)
        return JSONResponse({"error": f"failed to persist: {exc}"}, status_code=500)

    # Hot-reload via the settings cache, NOT os.environ (Bug A, v5.89). The old
    # ``os.environ[entry.name] = value_str`` smuggled the value into the env layer
    # to fake hot-reload — but env is the highest-precedence, machine/nix-owned
    # layer, so that mutation self-locked the knob (source() flipped to "env",
    # the next POST hit the 409 env-lock guard) and vanished on restart. Clearing
    # the config caches makes the next get_settings() / ConfigEntry read re-load
    # the just-written yaml — correct hot-reload, correct source attribution.
    clear_config_caches()

    # Car G2 (ADR-0163): defensively flush the runtime_config read-through cache on
    # a Settings hot-reload too (harmless — writes are rare). Wired at THIS core call
    # site, NOT inside _shared's clear_config_caches(), to avoid a _shared → core
    # import edge (lint-imports). The PRIMARY write-path bust is G3's config_set /
    # config_delete calling invalidate_config_cache() directly.
    from yadgar.core.server.tools._runtime_config import invalidate_config_cache

    invalidate_config_cache()

    # Serialise the coerced value. Python ``str(True)`` is capitalized ``"True"``,
    # which diverged from the GET path (lowercase env strings) and the YAML/JSON
    # convention — the config editor then showed booleans inconsistently. Render
    # bool knobs as lowercase ``"true"``/``"false"`` (ADR-0013 bool-display fix).
    value_str = _serialize_knob_value(coerced)

    audit_config_event("config_write", entry.name, old, value_str, 200, request, armed=armed)
    return JSONResponse(
        {
            "name": entry.name,
            "value": value_str,
            "reload": _classify_knob(entry.name),
        }
    )


@observe(tier="stage")
async def _vacuum_confirmed(request: Request) -> bool:
    """Return True iff the POST body carries ``confirm == "vacuum"``.

    Vacuum is ungated (ADR-0013) but causes 2-5 min of daemon downtime, so it
    requires a typed confirm — mirrors the restart handler's confirm gate.
    consolidate (mode=light, ~30s) and reembed (idempotent) need no confirm.
    """
    try:
        body = await request.json()
    except Exception:
        return False
    return isinstance(body, dict) and body.get("confirm") == "vacuum"


@observe(tier="boundary")
async def control_action_handler(request: Request) -> JSONResponse:
    """POST /api/control/action/{consolidate|vacuum|reembed} — trigger admin actions.

    ADR-0013 (v5.88.2): ungated (auth-protected, not debug-gated). ``vacuum``
    additionally requires a ``{"confirm": "vacuum"}`` body — it carries real
    daemon downtime. Each successful trigger emits one audit log line.
    """
    action = request.path_params.get("action", "")
    if action not in ("consolidate", "vacuum", "reembed"):
        return JSONResponse(
            {"error": f"unknown action: {action!r}. Supported: consolidate, vacuum, reembed"},
            status_code=400,
        )

    if action == "vacuum" and not await _vacuum_confirmed(request):
        return JSONResponse(
            {
                "error": 'vacuum requires confirmation: send {"confirm": "vacuum"}',
                "hint": "vacuum causes 2-5 min of daemon downtime.",
            },
            status_code=400,
        )

    logger.info("Control action %s triggered via control API", action)

    try:
        if action == "consolidate":
            result = consolidate_now(mode="light")
        elif action == "vacuum":
            # force=True (Car 10): a click here has already been confirmed
            # twice over — the browser confirm() dialog, then the
            # {"confirm": "vacuum"} body check above. vacuum_now's db-size
            # refusal (yadgar/core/server/tools/admin_vacuum.py) exists for
            # unattended/scheduled callers deciding whether a vacuum is worth
            # the downtime on their own; an explicit interactive request has
            # already made that call. Without force=True, any DB under 200
            # MiB (e.g. right after a previous vacuum shrank it) silently
            # skips writing the trigger file while this route still returns
            # HTTP 200 — the click looks like it worked and nothing happens.
            result = vacuum_now(force=True)
        elif action == "reembed":
            result = reembed_all()
        else:
            result = {}
    except Exception as exc:
        logger.exception("Control action %s failed: %s", action, exc)
        return JSONResponse({"error": str(exc)}, status_code=500)

    return JSONResponse({"action": action, "result": result})


@observe(tier="boundary")
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
        # Confirm-mismatch checked FIRST (Car D) — a rejected attempt must NOT
        # consume the rate-limit window (stamp only fires on a successful write).
        return JSONResponse(
            {
                "error": f"confirmation mismatch: expected {service!r}, got {confirm!r}",
                "hint": f'Send {{"confirm": "{service}"}} to confirm the restart.',
            },
            status_code=400,
        )

    # Car D rate-limit — THEN (after confirm passes, before the sentinel write).
    if restart_rate_limited(service):
        audit_config_event("restart", service, None, "rate_limited", 429, request)
        return JSONResponse(
            {"error": f"restart of {service!r} rate-limited; try again shortly."},
            status_code=429,
        )

    # Write sentinel file ONLY — no exec, no subprocess, no systemctl
    logger.info("Control restart %s triggered via control API", service)
    sentinel_path = _write_restart_sentinel(service)
    stamp_restart(service)  # window starts only on a successful sentinel write
    audit_config_event("restart", service, None, "sentinel_written", 202, request)
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
# Maintenance mode handlers (v5.50.3)
#
# These endpoints are intentionally NOT gated by YADGAR_DEBUG_APIS_ENABLED —
# the nightly script must reach them regardless of debug-API settings.
# They are still protected by BearerAuthMiddleware (path starts with /api/).
#
# POST /api/control/maintenance/enter — flip _maintenance_mode ON.
#   All DB-backed MCP tools fast-fail with a structured error until /exit.
# POST /api/control/maintenance/exit  — flip _maintenance_mode OFF.
#   Normal tool dispatch resumes. MUST be called in finally by the nightly cycle.
# ---------------------------------------------------------------------------


@observe(tier="stage")
async def _maintenance_ttl(request: Request) -> float | None:
    """Read a positive ``ttl_seconds`` from the request body, else None.

    Tolerant by design: a missing/empty/unparseable body means "no TTL", which
    is the pre-task:0113 behaviour.  A malformed TTL must never 500 the one
    endpoint an operator uses to un-wedge a read-only engine.
    """
    try:
        body = await request.json()
    except Exception:
        return None
    if not isinstance(body, dict):
        return None
    # Single `except Exception` on purpose: ruff (target-version py314) rewrites a
    # tuple except into PEP-758's parenthesis-free form, which older interpreters
    # — including the one some pre-commit hooks resolve — cannot even AST-parse,
    # and scripts/check_route_literals.py silently drops an unparseable file from
    # its route table.  A malformed TTL is a no-TTL either way.
    try:
        ttl = float(body.get("ttl_seconds") or 0)
    except Exception:
        return None
    return ttl if ttl > 0 else None


@observe(tier="stage")
async def _maintenance_label(request: Request) -> dict:
    """Read the optional ``operation`` / ``phase`` labels off the enter body.

    Same tolerant-by-design stance as ``_maintenance_ttl`` above and for the same
    reason: a malformed label must never 500 the endpoint that un-wedges the
    engine.  ``None`` means "not supplied" — the caller leaves the stored label
    untouched, which is what makes a nested enter safe.

    Single ``except Exception`` on purpose — see ``_maintenance_ttl``.
    """
    try:
        body = await request.json()
    except Exception:
        return {"operation": None, "phase": None}
    if not isinstance(body, dict):
        return {"operation": None, "phase": None}
    out = {}
    for key in ("operation", "phase"):
        value = body.get(key)
        out[key] = value.strip() if isinstance(value, str) and value.strip() else None
    return out


@observe(tier="boundary")
async def maintenance_enter_handler(request: Request) -> JSONResponse:
    """POST /api/control/maintenance/enter — enter maintenance mode.

    Sets _maintenance_mode=True so every MCP tool returns a fast structured error
    instead of touching the DB. Core stays UP — no MCP disconnect for clients.

    Body (task:0113, all optional):
      ``ttl_seconds`` — self-heal deadline; omitted/blank/<=0 keeps the historic
      no-expiry behaviour so a caller that has not been updated cannot regress.
      ``operation`` — what the window is FOR ("vacuum"/"nightly"/"backup"); the
      gate envelope used to hardcode "(vacuum)" for all three (Car 1, 2026-08-20
      train). Honoured on the OUTER enter only; unlabelled renders "maintenance".
      ``phase`` — where inside the operation we are. Re-entering IS the phase
      channel, so advancing it needs no extra route.

    Returns ``previous`` — the state BEFORE this call.  Windows nest: nightly
    enters at step 1 and exits at step 7, and its step-4 vacuum enters the same
    flag.  Without ``previous`` the inner caller would un-gate the outer one
    mid-cycle.  Reported on the enter response rather than via a separate GET:
    one round trip, and no TOCTOU between a read and a write.

    A nested enter NEVER shortens the outer window — the deadline is widened to
    the later of the two (and stays None if either side asked for no expiry).

    Returns ``deadline_seconds`` (task:0113 follow-up, car E) — seconds until the
    EFFECTIVE deadline after nesting is resolved, or ``None`` when the window has
    no expiry.  Purely informational: a caller (e.g. a backup arm asserting this
    gate per ADR-0204) can use it to verify it actually has a self-heal belt
    before doing something destructive.  This field never changes gate behavior —
    it just reports the ``_maintenance_deadline`` this call already computed.
    """
    import yadgar._shared.runtime.state as _st  # noqa: PLC0415 — late import, write live attr

    ttl = await _maintenance_ttl(request)
    label = await _maintenance_label(request)
    previous = bool(_st._maintenance_mode)
    deadline = (time.monotonic() + ttl) if ttl else None
    if previous and (_st._maintenance_deadline is None or deadline is None):
        deadline = None
    elif previous:
        deadline = max(_st._maintenance_deadline, deadline)
    else:
        _st._maintenance_entered_at = time.monotonic()
    _st._maintenance_mode = True
    _st._maintenance_deadline = deadline
    # ``operation`` names the WINDOW, so like ``_maintenance_entered_at`` above
    # it belongs to the OUTER holder: nightly labels the window at step 1 and its
    # step-4 vacuum must not relabel it, because the window the caller is waiting
    # on is nightly's. ``phase`` is the opposite — anyone may advance it, and a
    # nested enter is exactly how a phase transition is reported.
    # Absent means "leave it alone" in both cases, never "clear it".
    if label["operation"] is not None and not previous:
        _st._maintenance_operation = label["operation"]
    if label["phase"] is not None:
        _st._maintenance_phase = label["phase"]
    deadline_seconds = (deadline - time.monotonic()) if deadline is not None else None
    logger.info(
        "maintenance mode entered",
        extra={
            "component": "control",
            "action": "maintenance_enter",
            "outcome": "ok",
            "previous": previous,
            "ttl_seconds": ttl,
            "operation": _st._maintenance_operation,
            "phase": _st._maintenance_phase,
        },
    )
    return JSONResponse(
        {
            "status": "maintenance",
            "maintenance_mode": True,
            "previous": previous,
            "deadline_seconds": deadline_seconds,
            "operation": _st._maintenance_operation,
            "phase": _st._maintenance_phase,
        }
    )


@observe(tier="boundary")
async def maintenance_exit_handler(request: Request) -> JSONResponse:
    """POST /api/control/maintenance/exit — exit nightly maintenance mode.

    Sets _maintenance_mode=False, restoring normal MCP tool dispatch.
    The nightly cycle calls this in a finally block to guarantee un-wedge.

    Clears the TTL deadline too (task:0113): a surviving deadline would make the
    next no-TTL enter inherit an already-expired window and self-clear at once.
    Car 1 (2026-08-20 train) clears the operation/phase labels for the same
    reason — a stale label would mislabel the NEXT window's envelope.
    """
    from yadgar._shared.runtime.maintenance import (  # noqa: PLC0415
        reset_maintenance_state,
    )

    reset_maintenance_state()
    logger.info(
        "maintenance mode exited",
        extra={"component": "control", "action": "maintenance_exit", "outcome": "ok"},
    )
    return JSONResponse({"status": "active", "maintenance_mode": False})


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


@mcp_server.custom_route("/api/control/config", methods=["GET"])
@trace_span()
async def control_config_get(request: Request) -> JSONResponse:
    """Expose knob table for Control tab config editor."""
    return await control_config_get_handler(request)


@mcp_server.custom_route("/api/control/config", methods=["POST"])
@trace_span()
async def control_config_post(request: Request) -> JSONResponse:
    """Set ONE config knob (validates type + range; persists to YAML)."""
    return await control_config_post_handler(request)


@mcp_server.custom_route("/api/control/action/{action}", methods=["POST"])
@trace_span()
async def control_action(request: Request) -> JSONResponse:
    """Trigger consolidate / vacuum / reembed action."""
    return await control_action_handler(request)


@mcp_server.custom_route("/api/control/restart/{service}", methods=["POST"])
@trace_span()
async def control_restart(request: Request) -> JSONResponse:
    """Write restart sentinel file for yadgar or yadgar-backend (sentinel-only; no exec)."""
    return await control_restart_handler(request)


@mcp_server.custom_route("/api/control/maintenance/enter", methods=["POST"])
@trace_span()
async def control_maintenance_enter(request: Request) -> JSONResponse:
    """Enter nightly maintenance mode — MCP tools fast-fail, core stays UP."""
    return await maintenance_enter_handler(request)


@mcp_server.custom_route("/api/control/maintenance/exit", methods=["POST"])
@trace_span()
async def control_maintenance_exit(request: Request) -> JSONResponse:
    """Exit nightly maintenance mode — restore normal MCP tool dispatch."""
    return await maintenance_exit_handler(request)
