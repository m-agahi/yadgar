"""Memory block MCP tool registrations (v5.33.0 — Adopt-4 Letta-style core memory).

Five tools:
  block_create  — create a new named block (project or global scope)
  block_get     — fetch block content by name + scope
  block_update  — full-replace block content (char_limit enforced)
  block_delete  — remove a block (idempotent)
  block_list    — list blocks for a scope + directory

All tools use the unified @_tool(power=True) decorator and delegate to
StorageEngine._BlocksMixin via yadgar.server.lifecycle._get_storage().

Error model: all errors return {ok: False, error: "..."} — never raise.
Secret gate: block_create and block_update scan content via gate_or_reject (I26).
"""

from __future__ import annotations

import logging

from yadgar.secrets import gate_or_reject
from yadgar.server._app import _tool
from yadgar.server.lifecycle import _get_storage

logger = logging.getLogger(__name__)


@_tool(power=True)
def block_create(
    name: str,
    content: str,
    scope: str = "project",
    char_limit: int = 2000,
    directory: str | None = None,
) -> dict:
    """Create a new memory block. Blocks are always-injected, named text containers.

    Args:
        name: Block name — lowercase, underscore-separated, e.g. 'current_task'.
        content: Initial block content.
        scope: 'project' (per-directory) or 'global' (cross-project). Default 'project'.
        char_limit: Per-block character cap. Default 2000, max 8000.
        directory: Absolute project path. Required for scope='project'.

    Returns:
        {id, name, scope, content, char_limit, created_at, updated_at} on success.
        {ok: False, error: "..."} on validation failure or duplicate.
    """
    storage = _get_storage()
    if storage is None:
        return {"ok": False, "error": "storage_not_initialized"}

    # Secret gate — scan before any state mutation (I26)
    gate = gate_or_reject(content)
    if gate is not None:
        return gate

    try:
        result = storage.create_block(
            name=name,
            content=content,
            scope=scope,
            directory=directory,
            char_limit=char_limit,
        )
    except Exception as exc:
        logger.warning("block_create error name=%s: %s", name, exc)
        return {"ok": False, "error": str(exc)}

    return result


@_tool(power=True)
def block_get(
    name: str,
    scope: str = "project",
    directory: str | None = None,
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
    storage = _get_storage()
    if storage is None:
        return {"ok": False, "error": "storage_not_initialized"}

    try:
        result = storage.get_block(name=name, scope=scope, directory=directory)
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
    storage = _get_storage()
    if storage is None:
        return {"ok": False, "error": "storage_not_initialized"}

    # Secret gate (I26)
    gate = gate_or_reject(content)
    if gate is not None:
        return gate

    try:
        result = storage.update_block(
            name=name,
            content=content,
            scope=scope,
            directory=directory,
        )
    except Exception as exc:
        logger.warning("block_update error name=%s: %s", name, exc)
        return {"ok": False, "error": str(exc)}

    return result


@_tool(power=True)
def block_delete(
    name: str,
    scope: str = "project",
    directory: str | None = None,
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
    storage = _get_storage()
    if storage is None:
        return {"ok": False, "error": "storage_not_initialized"}

    try:
        storage.delete_block(name=name, scope=scope, directory=directory)
    except Exception as exc:
        logger.warning("block_delete error name=%s: %s", name, exc)
        return {"ok": False, "error": str(exc)}

    return {"deleted": True, "name": name}


@_tool(power=True)
def block_list(
    scope: str | None = None,
    directory: str | None = None,
) -> list[dict]:
    """List memory blocks for a scope and directory.

    Args:
        scope: 'project', 'global', or None (both). Default None.
        directory: Absolute project path. Required for scope='project' or None.

    Returns:
        List of {name, scope, content, char_limit, updated_at} dicts, ordered by name.
        Empty list on no matches or storage error.
    """
    storage = _get_storage()
    if storage is None:
        return []

    try:
        rows = storage.list_blocks(scope=scope, directory=directory)
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
