"""Memory block MCP tool registrations (v5.33.0 — Adopt-4 Letta-style core memory).

Seven tools:
  block_create  — create a new named block (project or global scope)
  block_get     — fetch block content by name + scope
  block_update  — full-replace block content (char_limit enforced)
  block_delete  — remove a block (idempotent)
  block_list    — list blocks for a scope + directory
  block_replace — patch: string-replace old_text with new_text (v5.35.1)
  block_append  — patch: append text with newline (v5.35.1)

All tools use the unified @_tool(power=True) decorator and delegate to
StorageEngine._BlocksMixin via yadgar.server.lifecycle._get_storage().

Error model: all errors return {ok: False, error: "..."} — never raise.
Secret gate: block_create, block_update, block_replace, block_append scan
content via gate_or_reject (I26).
"""

from __future__ import annotations

import logging

from yadgar._shared.observability.observe import observe
from yadgar._shared.runtime.lifecycle import _get_storage
from yadgar._shared.security.secrets import gate_or_reject
from yadgar.core.forward import _forward_admin
from yadgar.core.server._app import _tool
from yadgar.core.server.tools._project_param import accept_project_param

logger = logging.getLogger(__name__)

# v5.42.5: F3 — guard for scope='project' requiring directory
_MISSING_DIRECTORY_RESPONSE: dict = {
    "ok": False,
    "error": "missing_directory",
    "message": "directory is required when scope='project'. Pass the absolute project path.",
}


@observe(tier="hot", metric="tools.blocks._require_directory_for_project_scope")
def _require_directory_for_project_scope(scope: str, directory: str | None) -> dict | None:
    """Return an error dict if scope='project' and directory is absent/empty, else None."""
    if scope == "project" and not (directory and directory.strip()):
        return _MISSING_DIRECTORY_RESPONSE
    return None


@_tool(power=True)
def block_create(
    name: str,
    content: str,
    scope: str = "project",
    char_limit: int | None = None,
    directory: str | None = None,
    *,
    project: str | None = None,
) -> dict:
    """Create a new memory block. Blocks are always-injected, named text containers.

    Args:
        name: Block name — lowercase, underscore-separated, e.g. 'current_task'.
        content: Initial block content.
        scope: 'project' (per-directory) or 'global' (cross-project). Default 'project'.
        char_limit: Per-block character cap. Default from config (MEMORY_BLOCK_DEFAULT_CHAR_LIMIT=2000), hard max from config (MEMORY_BLOCK_HARD_CHAR_LIMIT=8000).
        directory: Absolute project path. Required for scope='project'.

    Returns:
        {id, name, scope, content, char_limit, created_at, updated_at} on success.
        {ok: False, error: "..."} on validation failure or duplicate.
    """
    # C3 (0047 PR#40 §5.C3): validated at the MCP boundary; C7 re-keys
    # this tool's scope from ``directory`` onto the resolved project_id.
    # C11: the validated value is KEPT and put on the payload. It was computed
    # and discarded — the same defect C13 found on ``checkpoint`` — because
    # ``memory_block`` had no column for it. Migration 033 added one.
    _project_id = accept_project_param(project, directory)
    # v5.42.5 F3: directory required for scope='project'
    _dir_guard = _require_directory_for_project_scope(scope, directory)
    if _dir_guard is not None:
        return _dir_guard

    # Secret gate — scan before any state mutation (I26)
    gate = gate_or_reject(content)
    if gate is not None:
        return gate

    # R3 Car 3a: storage write forwards to backend /admin.
    return _forward_admin(
        "block_create",
        {
            "name": name,
            "content": content,
            "scope": scope,
            "directory": directory,
            "project_id": _project_id,
            "char_limit": char_limit,
        },
    )


@_tool(power=True)
def block_get(
    name: str,
    scope: str = "project",
    directory: str | None = None,
    *,
    project: str | None = None,
) -> dict:
    """Fetch a memory block by name and scope.

    Args:
        name: Block name.
        scope: 'project' or 'global'. Default 'project'.
        directory: Absolute project path. Required for scope='project'.

    Returns:
        {id, name, scope, content, char_limit, created_at, updated_at} on success.
        {ok: False, error: "..."} if not found.
    """
    # C3 (0047 PR#40 §5.C3): validated at the MCP boundary; C7 re-keys
    # this tool's scope from ``directory`` onto the resolved project_id.
    _project_id = accept_project_param(project, directory)
    storage = _get_storage()
    if storage is None:
        return {"ok": False, "error": "storage_not_initialized"}

    # v5.42.5 F3: directory required for scope='project'
    _dir_guard = _require_directory_for_project_scope(scope, directory)
    if _dir_guard is not None:
        return _dir_guard

    try:
        result = storage.get_block(
            name=name, scope=scope, directory=directory, project_id=_project_id
        )
    except Exception as exc:
        logger.warning("block_get error name=%s: %s", name, exc)
        return {"ok": False, "error": str(exc)}

    if result is None:
        return {
            "ok": False,
            "error": f"block {name!r} not found in scope={scope!r} directory={directory!r}",
        }

    return result


@_tool(power=True)
def block_update(
    name: str,
    content: str,
    scope: str = "project",
    directory: str | None = None,
    *,
    project: str | None = None,
) -> dict:
    """Replace a memory block's content (full overwrite, char_limit enforced).

    Args:
        name: Block name to update.
        content: New block content.
        scope: 'project' or 'global'. Default 'project'.
        directory: Absolute project path. Required for scope='project'.

    Returns:
        Updated {id, name, scope, content, char_limit, updated_at} on success.
        {ok: False, error: "..."} on failure.
    """
    # C3 (0047 PR#40 §5.C3): validated at the MCP boundary; C7 re-keys
    # this tool's scope from ``directory`` onto the resolved project_id.
    _project_id = accept_project_param(project, directory)
    # v5.42.5 F3: directory required for scope='project'
    _dir_guard = _require_directory_for_project_scope(scope, directory)
    if _dir_guard is not None:
        return _dir_guard

    # Secret gate (I26)
    gate = gate_or_reject(content)
    if gate is not None:
        return gate

    # R3 Car 3a: storage write forwards to backend /admin.
    return _forward_admin(
        "block_update",
        {
            "name": name,
            "content": content,
            "scope": scope,
            "directory": directory,
            "project_id": _project_id,
        },
    )


@_tool(power=True)
def block_delete(
    name: str,
    scope: str = "project",
    directory: str | None = None,
    *,
    project: str | None = None,
) -> dict:
    """Delete a memory block. Idempotent — no error if block doesn't exist.

    Args:
        name: Block name to delete.
        scope: 'project' or 'global'. Default 'project'.
        directory: Absolute project path. Required for scope='project'.

    Returns:
        {deleted: True, name: str} on success.
        {ok: False, error: "..."} on unexpected failure.
    """
    # C3 (0047 PR#40 §5.C3): validated at the MCP boundary; C7 re-keys
    # this tool's scope from ``directory`` onto the resolved project_id.
    _project_id = accept_project_param(project, directory)
    # v5.42.5 F3: directory required for scope='project'
    _dir_guard = _require_directory_for_project_scope(scope, directory)
    if _dir_guard is not None:
        return _dir_guard

    # R3 Car 3a: storage write forwards to backend /admin.
    return _forward_admin(
        "block_delete",
        {"name": name, "scope": scope, "directory": directory, "project_id": _project_id},
    )


@_tool(power=True)
def block_list(
    scope: str | None = None,
    directory: str | None = None,
    *,
    project: str | None = None,
) -> list[dict]:
    """List memory blocks for a scope and directory.

    Args:
        scope: 'project', 'global', or None (both). Default None.
        directory: Absolute project path. Required for scope='project' or None.

    Returns:
        List of {name, scope, content, char_limit, updated_at} dicts, ordered by name.
        Empty list on no matches or storage error.
    """
    # C3 (0047 PR#40 §5.C3): validated at the MCP boundary; C7 re-keys
    # this tool's scope from ``directory`` onto the resolved project_id.
    # C11: the read takes BOTH — project_id for rows written after migration
    # 033, the legacy path for the historical corpus no backfill reaches.
    _project_id = accept_project_param(project, directory)
    storage = _get_storage()
    if storage is None:
        return []

    try:
        rows = storage.list_blocks(scope=scope, directory=directory, project_id=_project_id)
    except Exception as exc:
        logger.warning("block_list error scope=%s directory=%s: %s", scope, directory, exc)
        return []

    return [
        {
            "name": r.get("name", ""),
            "scope": r.get("scope", ""),
            "directory": r.get("directory"),
            "content": r.get("content", ""),
            "char_limit": r.get("char_limit", 2000),
            "updated_at": str(r.get("updated_at") or ""),
        }
        for r in rows
    ]


@_tool(power=True)
def block_replace(
    name: str,
    old_text: str,
    new_text: str,
    scope: str = "project",
    directory: str | None = None,
    *,
    project: str | None = None,
) -> dict:
    """Patch a memory block by replacing one occurrence of old_text with new_text.

    Cheaper than block_update for incremental edits — no need to re-emit full content.
    Errors if old_text is not found (0 matches) OR found more than once (ambiguous).

    Args:
        name: Block name to patch.
        old_text: Exact string to find (must appear exactly once).
        new_text: Replacement string.
        scope: 'project' or 'global'. Default 'project'.
        directory: Absolute project path. Required for scope='project'.

    Returns:
        Updated {id, name, scope, content, char_limit, updated_at} on success.
        {ok: False, error: "..."} on failure (not found, ambiguous, limit exceeded).
    """
    # C3 (0047 PR#40 §5.C3): validated at the MCP boundary; C7 re-keys
    # this tool's scope from ``directory`` onto the resolved project_id.
    _project_id = accept_project_param(project, directory)
    # v5.42.5 F3: directory required for scope='project'
    _dir_guard = _require_directory_for_project_scope(scope, directory)
    if _dir_guard is not None:
        return _dir_guard

    # Secret gate on new_text (I26)
    gate = gate_or_reject(new_text)
    if gate is not None:
        return gate

    # R3 Car 3a: storage write forwards to backend /admin.
    return _forward_admin(
        "block_replace",
        {
            "name": name,
            "old_text": old_text,
            "new_text": new_text,
            "scope": scope,
            "directory": directory,
            "project_id": _project_id,
        },
    )


@_tool(power=True)
def block_append(
    name: str,
    text: str,
    scope: str = "project",
    directory: str | None = None,
    *,
    project: str | None = None,
) -> dict:
    """Append text to a memory block with a newline separator.

    Cheaper than block_update for incremental additions — no need to re-emit full content.
    Respects the block's char_limit (hard cap enforced).

    Args:
        name: Block name to append to.
        text: Text to append (a newline is inserted between existing content and text).
        scope: 'project' or 'global'. Default 'project'.
        directory: Absolute project path. Required for scope='project'.

    Returns:
        Updated {id, name, scope, content, char_limit, updated_at} on success.
        {ok: False, error: "..."} on failure (block not found, limit exceeded).
    """
    # C3 (0047 PR#40 §5.C3): validated at the MCP boundary; C7 re-keys
    # this tool's scope from ``directory`` onto the resolved project_id.
    _project_id = accept_project_param(project, directory)
    # v5.42.5 F3: directory required for scope='project'
    _dir_guard = _require_directory_for_project_scope(scope, directory)
    if _dir_guard is not None:
        return _dir_guard

    # Secret gate (I26)
    gate = gate_or_reject(text)
    if gate is not None:
        return gate

    # R3 Car 3a: storage write forwards to backend /admin.
    return _forward_admin(
        "block_append",
        {
            "name": name,
            "text": text,
            "scope": scope,
            "directory": directory,
            "project_id": _project_id,
        },
    )
