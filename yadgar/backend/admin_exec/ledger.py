"""Backend execution bodies for the ledger READ admin ops (Car B).

Engine #2's ``MariaStorageEngine`` exposes the task / adr / agent_prompt read
methods directly on the class (no ``_LedgerMixin`` — Car A's deliberate design
choice to avoid the PR-#32 MRO collision with SurrealDB's
``_RuntimeConfigMixin``). These op bodies are the backend dispatch wrappers
that forward them to the ``/admin`` route's caller.

Each body is an ``async def`` because ``asyncmy`` is async-only; the admin
dispatcher (``run_admin_op_async``) keeps SYNC bodies on
``asyncio.to_thread`` and awaits ASYNC bodies on the event loop directly.

PAYLOAD SHAPES (contract for Cars D / F / I):

    list_task_rows(payload) -> {"rows": list[dict]}
        payload: {"project_id": str, "status"?: list[str]}

    get_task_row(payload) -> {"row": dict | None}
        payload: {"id": int}

    list_task_rows_all_projects(payload) -> {"rows": list[dict]}
        payload: {"status"?: list[str]}

    list_adr_rows(payload) -> {"rows": list[dict]}
        payload: {"project_id": str, "status"?: str}

    get_adr_row(payload) -> {"row": dict | None}
        payload: {"id": int}

    list_agent_prompt_rows(payload) -> {"rows": list[dict]}
        payload: {}   # no parameters today

ERROR MODEL: never raise. A storage exception becomes ``{"ok": False,
"error": "..."}`` — matches the existing admin-op contract (see
``runtime_config.py``).
"""

from __future__ import annotations

import logging
from typing import Any

from yadgar._shared.observability.observe import observe

logger = logging.getLogger(__name__)


def _get_sql_storage() -> Any:
    """The composed ``MariaStorageEngine``, or None when engine #2 is absent.

    A FUNCTION, not a module-scope import: ``sqlalchemy`` lives in the
    ``sql`` extra and is not always available. Matches the seam at
    ``admin_exec/engine_status.py:58`` and ``invariants_cross_engine.py:136``
    so tests patch one symbol across the admin_exec ledger surface.
    """
    from yadgar._shared.runtime.lifecycle import _get_sql_storage  # noqa: PLC0415

    return _get_sql_storage()


@observe(tier="boundary", metric="backend.admin.ledger.list_task_rows")
async def list_task_rows(payload: dict) -> dict:
    """Project-scoped ``task`` read. payload: {project_id, status?}."""
    storage = _get_sql_storage()
    if storage is None:
        return {"ok": False, "error": "engine #2 not composed (MariaStorageEngine is None)"}
    try:
        rows = await storage.list_task_rows(
            project_id=payload["project_id"],
            status=payload.get("status"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("list_task_rows error: %s", exc)
        return {"ok": False, "error": str(exc)}
    return {"rows": rows}


@observe(tier="boundary", metric="backend.admin.ledger.get_task_row")
async def get_task_row(payload: dict) -> dict:
    """Single ``task`` lookup by id. payload: {id}."""
    storage = _get_sql_storage()
    if storage is None:
        return {"ok": False, "error": "engine #2 not composed (MariaStorageEngine is None)"}
    try:
        row = await storage.get_task_row(int(payload["id"]))
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_task_row error id=%s: %s", payload.get("id"), exc)
        return {"ok": False, "error": str(exc)}
    return {"row": row}


@observe(tier="boundary", metric="backend.admin.ledger.list_task_rows_all_projects")
async def list_task_rows_all_projects(payload: dict) -> dict:
    """Cross-project ``task`` read. payload: {status?}."""
    storage = _get_sql_storage()
    if storage is None:
        return {"ok": False, "error": "engine #2 not composed (MariaStorageEngine is None)"}
    try:
        rows = await storage.list_task_rows_all_projects(
            status=payload.get("status"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("list_task_rows_all_projects error: %s", exc)
        return {"ok": False, "error": str(exc)}
    return {"rows": rows}


@observe(tier="boundary", metric="backend.admin.ledger.list_adr_rows")
async def list_adr_rows(payload: dict) -> dict:
    """Project-scoped ``adr`` read. payload: {project_id, status?}."""
    storage = _get_sql_storage()
    if storage is None:
        return {"ok": False, "error": "engine #2 not composed (MariaStorageEngine is None)"}
    try:
        rows = await storage.list_adr_rows(
            project_id=payload["project_id"],
            status=payload.get("status"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("list_adr_rows error: %s", exc)
        return {"ok": False, "error": str(exc)}
    return {"rows": rows}


@observe(tier="boundary", metric="backend.admin.ledger.get_adr_row")
async def get_adr_row(payload: dict) -> dict:
    """Single ``adr`` lookup by id. payload: {id}."""
    storage = _get_sql_storage()
    if storage is None:
        return {"ok": False, "error": "engine #2 not composed (MariaStorageEngine is None)"}
    try:
        row = await storage.get_adr_row(int(payload["id"]))
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_adr_row error id=%s: %s", payload.get("id"), exc)
        return {"ok": False, "error": str(exc)}
    return {"row": row}


@observe(tier="boundary", metric="backend.admin.ledger.list_agent_prompt_rows")
async def list_agent_prompt_rows(payload: dict) -> dict:
    """List every ``agent_pattern`` row. payload: {} (no params)."""
    storage = _get_sql_storage()
    if storage is None:
        return {"ok": False, "error": "engine #2 not composed (MariaStorageEngine is None)"}
    try:
        rows = await storage.list_agent_prompt_rows()
    except Exception as exc:  # noqa: BLE001
        logger.warning("list_agent_prompt_rows error: %s", exc)
        return {"ok": False, "error": str(exc)}
    return {"rows": rows}
