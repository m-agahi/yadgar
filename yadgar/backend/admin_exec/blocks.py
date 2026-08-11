"""Backend execution bodies for the memory-block CRUD admin ops (R3 Car 3a / R5).

These are the storage-write halves of the core ``block_*`` MCP tools. The core
``@_tool`` shells keep the directory guard + secret gate (I26) + MCP schema and
forward the write here over HTTP (POST /admin) via ``_forward_admin``. Read
tools (block_get / block_list) stay core.

Each op is an undecorated ``(payload: dict) -> dict`` function; the ``@observe``
decorators satisfy the I33 tri-signal ratchet. Error model mirrors the tools:
``{ok: False, error: "..."}`` — never raise.

NOTE: block writes touch NO ``core/cache.py`` namespace (the four cached read
tools are project_brief / wiki_read / wiki_query / agent_prompt_prelude). No
epoch bump needed for block CRUD.
"""

from __future__ import annotations

import logging

from yadgar._shared.observability.observe import observe
from yadgar._shared.runtime.lifecycle import _get_storage

logger = logging.getLogger(__name__)


@observe(tier="boundary", metric="backend.admin.block_create")
def block_create(payload: dict) -> dict:
    """Create a memory block. Storage-write half.

    payload: {name, content, scope, directory, project_id?, char_limit?}
    Directory guard + secret gate already ran core-side.

    C11 (0047 PR#40 §5): ``project_id`` is the value the core tool resolved via
    ``accept_project_param`` and — until this car — computed and threw away.
    Migration 033 gives ``memory_block`` a column for it; ``create_block``
    dual-writes it alongside ``directory``.
    """
    storage = _get_storage()
    kwargs: dict = {
        "name": payload["name"],
        "content": payload["content"],
        "scope": payload.get("scope", "project"),
        "directory": payload.get("directory"),
        "project_id": payload.get("project_id"),
    }
    if payload.get("char_limit") is not None:
        kwargs["char_limit"] = payload["char_limit"]
    try:
        return storage.create_block(**kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("block_create error name=%s: %s", payload.get("name"), exc)
        return {"ok": False, "error": str(exc)}


@observe(tier="boundary", metric="backend.admin.block_update")
def block_update(payload: dict) -> dict:
    """Full-replace a block's content. Storage-write half.

    payload: {name, content, scope, directory}
    """
    storage = _get_storage()
    try:
        return storage.update_block(
            name=payload["name"],
            content=payload["content"],
            scope=payload.get("scope", "project"),
            directory=payload.get("directory"),
            project_id=payload.get("project_id"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("block_update error name=%s: %s", payload.get("name"), exc)
        return {"ok": False, "error": str(exc)}


@observe(tier="boundary", metric="backend.admin.block_delete")
def block_delete(payload: dict) -> dict:
    """Delete a block (idempotent). Storage-write half.

    payload: {name, scope, directory}
    """
    storage = _get_storage()
    name = payload["name"]
    try:
        storage.delete_block(
            name=name,
            scope=payload.get("scope", "project"),
            directory=payload.get("directory"),
            project_id=payload.get("project_id"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("block_delete error name=%s: %s", name, exc)
        return {"ok": False, "error": str(exc)}
    return {"deleted": True, "name": name}


@observe(tier="boundary", metric="backend.admin.block_replace")
def block_replace(payload: dict) -> dict:
    """Patch a block: replace one occurrence of old_text with new_text. Storage-write half.

    payload: {name, old_text, new_text, scope, directory}
    Secret gate on new_text already ran core-side.
    """
    storage = _get_storage()
    try:
        return storage.replace_block(
            name=payload["name"],
            old_text=payload["old_text"],
            new_text=payload["new_text"],
            scope=payload.get("scope", "project"),
            directory=payload.get("directory"),
            project_id=payload.get("project_id"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("block_replace error name=%s: %s", payload.get("name"), exc)
        return {"ok": False, "error": str(exc)}


@observe(tier="boundary", metric="backend.admin.block_append")
def block_append(payload: dict) -> dict:
    """Append text to a block with a newline separator. Storage-write half.

    payload: {name, text, scope, directory}
    Secret gate on text already ran core-side.
    """
    storage = _get_storage()
    try:
        return storage.append_block(
            name=payload["name"],
            text=payload["text"],
            scope=payload.get("scope", "project"),
            directory=payload.get("directory"),
            project_id=payload.get("project_id"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("block_append error name=%s: %s", payload.get("name"), exc)
        return {"ok": False, "error": str(exc)}
