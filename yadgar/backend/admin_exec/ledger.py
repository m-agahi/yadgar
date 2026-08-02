"""Backend execution bodies for the spine ledger admin ops (Car B).

These are the storage-write halves of the core ledger MCP tools. Mirrors the
pattern in ``admin_exec/blocks.py``: core ``@_tool`` shells keep validation
+ secret gate (I26) + MCP schema and forward the write here over HTTP
(POST /admin) via ``_forward_admin``. Read tools (task_get, adr_get,
agent_prompt_get) stay core.

Each op is an undecorated ``(payload: dict) -> dict`` function; the ``@observe``
decorators satisfy the I33 tri-signal ratchet. Error model: ``{ok: False,
error: "..."}`` — never raise.

D20: every row access goes through _LedgerMixin on the storage side. This
file is the forwarder, not the executor — storage methods do the actual work.
"""

from __future__ import annotations

import logging

from yadgar._shared.observability.observe import observe
from yadgar._shared.runtime.lifecycle import _get_storage

logger = logging.getLogger(__name__)


@observe(tier="boundary", metric="backend.admin.task_create")
def ledger_task_create(payload: dict) -> dict:
    """Create a task row. Storage-write half.

    payload: {project_id, origin, title, state?, active_form?, plan_path?, ...}
    Directory guard + secret gate already ran core-side.
    """
    storage = _get_storage()
    try:
        return storage.create_task(payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ledger_task_create error title=%s: %s", payload.get("title"), exc)
        return {"ok": False, "error": str(exc)}


@observe(tier="boundary", metric="backend.admin.adr_add")
def ledger_adr_add(payload: dict) -> dict:
    """Create an ADR row. Storage-write half.

    payload: {project_id, title, context, decision, rationale, ...}
    Directory guard + secret gate already ran core-side.
    """
    storage = _get_storage()
    try:
        return storage.add_adr(payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ledger_adr_add error title=%s: %s", payload.get("title"), exc)
        return {"ok": False, "error": str(exc)}


@observe(tier="boundary", metric="backend.admin.agent_prompt_save")
def ledger_agent_prompt_save(payload: dict) -> dict:
    """Create/update an agent_prompt row. Storage-write half.

    payload: {pattern, content, directory, ...}
    Directory guard + secret gate already ran core-side.
    """
    storage = _get_storage()
    try:
        return storage.save_agent_prompt(payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "ledger_agent_prompt_save error pattern=%s: %s",
            payload.get("pattern"),
            exc,
        )
        return {"ok": False, "error": str(exc)}


@observe(tier="boundary", metric="backend.admin.runtime_config_set")
def ledger_runtime_config_set(payload: dict) -> dict:
    """Upsert a runtime_config row in MariaDB (replaces SurrealDB path).

    payload: {key, value, directory}
    """
    storage = _get_storage()
    try:
        return storage.set_config_row(
            payload["key"],
            payload["value"],
            directory=payload.get("directory"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "ledger_runtime_config_set error key=%s: %s",
            payload.get("key"),
            exc,
        )
        return {"ok": False, "error": str(exc)}


@observe(tier="boundary", metric="backend.admin.runtime_config_delete")
def ledger_runtime_config_delete(payload: dict) -> dict:
    """Delete a runtime_config row (idempotent).

    payload: {key, directory}
    """
    storage = _get_storage()
    key = payload["key"]
    try:
        storage.delete_config_row(key, directory=payload.get("directory"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("ledger_runtime_config_delete error key=%s: %s", key, exc)
        return {"ok": False, "error": str(exc)}
    return {"deleted": True, "key": key}
