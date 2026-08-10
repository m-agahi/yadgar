"""Runtime config MCP tool registrations (ADR-0163, Car G3).

Four tools over the DB-backed, directory-aware runtime-config store:

  config_get(key, directory=None, default=None)          — resolved value (PTC read)
  config_list(directory=None)                             — effective rows (debug/read)
  config_set(key, value, scope="global", directory=None) — validate + forward + invalidate
  config_delete(key, scope="global", directory=None)     — forward + invalidate

Reads (config_get / config_list) go CORE via the G2 resolver / ``_get_storage``
(``_runtime_config`` module) — no read admin op, matching ``blocks``. Writes
(config_set / config_delete) forward to the backend G1 admin ops
(``runtime_config_set`` / ``runtime_config_delete``) via ``_forward_admin`` and
then whole-flush the resolver cache via ``invalidate_config_cache``.

Scope→directory mapping (matches the backend ``{key, value, directory}`` payload
and the G2 resolution model where per-dir overrides global):

  scope="global"  → directory=None (the global row)
  scope="project" → directory=<given dir> (an absolute path; REQUIRED)

NONE are ``always_load`` (ADR-0047 — config reads are not session-critical).

Error model: validation failures return ``{ok: False, error: "..."}`` — never
raise (mirrors ``blocks``).
"""

from __future__ import annotations

import logging
from typing import Any

from yadgar._shared.observability.observe import observe
from yadgar._shared.runtime.lifecycle import _get_storage
from yadgar.core.forward import _forward_admin
from yadgar.core.server._app import _tool
from yadgar.core.server.tools._project_param import accept_project_param

# Import the resolver module's public read + invalidation entry points. Aliased so
# the tool-level ``config_get`` does not shadow the resolver's ``config_get``.
from yadgar.core.server.tools._runtime_config import config_get as _resolver_get
from yadgar.core.server.tools._runtime_config import invalidate_config_cache

logger = logging.getLogger(__name__)

# Valid scope values. A "global" write targets directory=None; a "project" write
# targets the given absolute directory (required).
_VALID_SCOPES = frozenset({"global", "project"})

# JSON-serializable value types the store accepts. bool is a subclass of int, so
# ``isinstance(True, int)`` is True — both are covered. Deliberately excludes
# float (no neighbor stores it) and bytes / arbitrary objects.
_JSON_VALUE_TYPES = (bool, int, str, list, dict)

_INVALID_SCOPE_RESPONSE: dict = {
    "ok": False,
    "error": "invalid_scope",
    "message": "scope must be 'global' or 'project'.",
}

_MISSING_DIRECTORY_RESPONSE: dict = {
    "ok": False,
    "error": "missing_directory",
    "message": "directory is required when scope='project'. Pass the absolute project path.",
}

_INVALID_VALUE_RESPONSE: dict = {
    "ok": False,
    "error": "invalid_value",
    "message": "value must be JSON-serializable: bool, int, str, list, or dict.",
}


@observe(tier="hot", metric="tools.runtime_config._resolve_scope")
def _resolve_scope(scope: str, directory: str | None) -> tuple[str | None, dict | None]:
    """Map (scope, directory) → (target_directory, error).

    Returns ``(target_directory, None)`` on success where ``target_directory`` is
    ``None`` for a global write or the given path for a project write. Returns
    ``(None, error_dict)`` on a validation failure.
    """
    if scope not in _VALID_SCOPES:
        return None, _INVALID_SCOPE_RESPONSE
    if scope == "project":
        if not (directory and directory.strip()):
            return None, _MISSING_DIRECTORY_RESPONSE
        return directory, None
    # global scope → the global row (directory IS NONE), ignoring any passed dir.
    return None, None


@_tool(power=False)
def config_get(
    key: str, directory: str | None = None, default: Any = None, *, project: str | None = None
) -> Any:
    """Resolve a runtime config value (per-dir override → global → ``default``).

    PTC read-through the G2 resolver cache. Never raises — a storage error
    yields ``default``.

    Args:
        key: Config key (e.g. ``code_graph.enabled``).
        directory: Absolute project path for a per-dir lookup; ``None`` = global only.
        default: Returned when neither a per-dir nor a global row exists.

    Returns:
        The resolved value, or ``default``.
    """
    # C3 (0047 PR#40 §5.C3): validated at the MCP boundary; C7 re-keys
    # this tool's scope from ``directory`` onto the resolved project_id.
    accept_project_param(project, directory)
    return _resolver_get(key, directory=directory, default=default)


@_tool(power=False)
def config_list(directory: str | None = None, *, project: str | None = None) -> list[dict]:
    """List effective runtime config rows (debug/read).

    Args:
        directory: ``None`` → ALL rows (global + every directory). An absolute
            path → only that directory's rows.

    Returns:
        List of ``{key, directory, value, ...}`` dicts (empty on no storage).
    """
    # C3 (0047 PR#40 §5.C3): validated at the MCP boundary; C7 re-keys
    # this tool's scope from ``directory`` onto the resolved project_id.
    accept_project_param(project, directory)
    storage = _get_storage()
    if storage is None:
        return []
    try:
        if directory is None:
            # Sentinel default = ALL rows (global + per-dir). Passing None to the
            # storage layer would mean "global rows only" — not what a bare
            # config_list() should show.
            return storage.list_config_rows()
        return storage.list_config_rows(directory=directory)
    except Exception as exc:  # noqa: BLE001 — a debug read must never crash its caller
        logger.warning("config_list error directory=%s: %s", directory, exc)
        return []


@observe(tier="stage", metric="tools.runtime_config._apply_config_set")
def _apply_config_set(key: str, value: Any, scope: str, directory: str | None) -> dict:
    """Validate + forward + invalidate for a config set. Shared by the tool + route.

    Car G5: the ``config_set`` MCP tool AND the host-side WRITE route
    (``POST /api/runtime-config/{key}``) both call this so their validation,
    forward, and cache-bust semantics cannot drift (mirrors how the GET route
    reuses the plain resolver rather than the ``@_tool`` ``config_get``).

    Returns the written row on success, or ``{ok: False, error: "..."}`` on a
    scope / value validation failure (never raises).
    """
    target_dir, err = _resolve_scope(scope, directory)
    if err is not None:
        return err
    if not isinstance(value, _JSON_VALUE_TYPES):
        return _INVALID_VALUE_RESPONSE

    result = _forward_admin(
        "runtime_config_set",
        {"key": key, "value": value, "directory": target_dir},
    )
    invalidate_config_cache()
    return result


@observe(tier="stage", metric="tools.runtime_config._apply_config_delete")
def _apply_config_delete(key: str, scope: str, directory: str | None) -> dict:
    """Validate + forward + invalidate for a config delete. Shared by the tool + route.

    Returns ``{deleted: True, key}`` on success, or ``{ok: False, error: "..."}``
    on a validation failure (never raises).
    """
    target_dir, err = _resolve_scope(scope, directory)
    if err is not None:
        return err

    result = _forward_admin(
        "runtime_config_delete",
        {"key": key, "directory": target_dir},
    )
    invalidate_config_cache()
    return result


@_tool(power=True)
def config_set(
    key: str,
    value: Any,
    scope: str = "global",
    directory: str | None = None,
    *,
    project: str | None = None,
) -> dict:
    """Set a runtime config value; write forwards to the backend, then bust cache.

    Args:
        key: Config key (e.g. ``code_graph.enabled``).
        value: JSON-serializable value (bool / int / str / list / dict).
        scope: ``"global"`` (the global row) or ``"project"`` (per-dir override;
            ``directory`` required).
        directory: Absolute project path — required when ``scope="project"``.

    Returns:
        The written row ``{key, directory, value}`` on success, or
        ``{ok: False, error: "..."}`` on a validation failure.
    """
    # C3 (0047 PR#40 §5.C3): validated at the MCP boundary; C7 re-keys
    # this tool's scope from ``directory`` onto the resolved project_id.
    accept_project_param(project, directory)
    return _apply_config_set(key, value, scope, directory)


@_tool(power=True)
def config_delete(
    key: str,
    scope: str = "global",
    directory: str | None = None,
    *,
    project: str | None = None,
) -> dict:
    """Delete a runtime config row (revert to fallback/default); forward + bust cache.

    Args:
        key: Config key to remove.
        scope: ``"global"`` or ``"project"`` (``directory`` required for project).
        directory: Absolute project path — required when ``scope="project"``.

    Returns:
        ``{deleted: True, key}`` on success (idempotent), or
        ``{ok: False, error: "..."}`` on a validation failure.
    """
    # C3 (0047 PR#40 §5.C3): validated at the MCP boundary; C7 re-keys
    # this tool's scope from ``directory`` onto the resolved project_id.
    accept_project_param(project, directory)
    return _apply_config_delete(key, scope, directory)
