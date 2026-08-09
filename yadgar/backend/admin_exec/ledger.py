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

    create_task_row(payload) -> {"id": int, ...row}
        payload: {"project_id": str, "title": str, "status"?: str,
                  "state"?: str, "active_form"?: str, "plan_path"?: str,
                  "body_slug"?: str}
        Returns the inserted PK (LAST_INSERT_ID) plus the inserted params.

    update_task_row(payload) -> {"id": int, ...patched}
        payload: {"id": int, <column>: <value>, ...}
        Only the named columns are UPDATEd; absent fields are left unchanged
        (the storage layer's update_task_row enforces an empty-fields no-op).
        ``state: None`` clears the column to NULL (§16.10 — completed/archived
        transitions). ``blocked_by`` / ``blocks`` lists are reconciled against
        the ``task_blocked_by`` join table (D39) — they are NOT columns on
        ``task``; the admin op handles the join-edge sync side-channel.

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


# ── Car D: task write ops ─────────────────────────────────────────────────────
# The MCP tool shells in yadgar.core.server.tools.task forward here over HTTP.
# These wrappers translate the dict payload into the typed call into
# ``MariaStorageEngine`` (engine #2). The optional ``blocked_by`` / ``blocks``
# keys reconcile the ``task_blocked_by`` join table (D39) AFTER the row is
# created/updated; the reconciliation is idempotent (delete-then-insert).


@observe(tier="boundary", metric="backend.admin.ledger.create_task_row")
async def create_task_row(payload: dict) -> dict:
    """INSERT one ``task`` row. payload keys (see module docstring)."""
    storage = _get_sql_storage()
    if storage is None:
        return {"ok": False, "error": "engine #2 not composed (MariaStorageEngine is None)"}
    try:
        result = await storage.create_task_row(
            project_id=payload["project_id"],
            title=payload["title"],
            status=payload.get("status", "pending"),
            state=payload.get("state", "open"),
            active_form=payload.get("active_form"),
            plan_path=payload.get("plan_path"),
            body_slug=payload.get("body_slug"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("create_task_row error: %s", exc)
        return {"ok": False, "error": str(exc)}
    # D39: optionally reconcile ``task_blocked_by`` join edges on CREATE.
    inserted_id = int(result.get("id", 0))
    blocked_by = payload.get("blocked_by")
    if blocked_by is not None and inserted_id:
        try:
            for blocker_id in blocked_by:
                await storage.add_task_blocked_by(inserted_id, int(blocker_id))
        except Exception as exc:  # noqa: BLE001
            logger.warning("create_task_row blocked_by sync error: %s", exc)
            # Row created; edge-sync failure is non-fatal — surface the id.
    return result


@observe(tier="boundary", metric="backend.admin.ledger.update_task_row")
async def update_task_row(payload: dict) -> dict:
    """UPDATE one ``task`` row. payload: {id, <col>: <val>, ...}."""
    storage = _get_sql_storage()
    if storage is None:
        return {"ok": False, "error": "engine #2 not composed (MariaStorageEngine is None)"}
    try:
        task_id = int(payload["id"])
        # Strip non-column keys before the typed UPDATE; ``blocked_by`` and
        # ``blocks`` are join-edge reconcilers (D39), handled separately.
        column_payload = {
            k: v for k, v in payload.items() if k not in {"id", "blocked_by", "blocks"}
        }
        await storage.update_task_row(task_id, **column_payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("update_task_row error: %s", exc)
        return {"ok": False, "error": str(exc)}
    # D39: optionally reconcile ``task_blocked_by`` join edges on UPDATE.
    blocked_by = payload.get("blocked_by")
    if blocked_by is not None:
        try:
            # Read the existing set; delete the diff (removed); insert the diff (added).
            existing = set(await storage.list_task_blocked_by(task_id))
            desired = {int(x) for x in blocked_by}
            from sqlalchemy import text as _sa_text  # noqa: PLC0415

            async with storage._engine.begin() as conn:  # type: ignore[attr-defined]
                for gone in existing - desired:
                    await conn.execute(
                        _sa_text(
                            "DELETE FROM task_blocked_by "
                            "WHERE task_id = :task_id AND blocked_by_id = :blocked_by_id"
                        ),
                        {"task_id": task_id, "blocked_by_id": gone},
                    )
            for new in desired - existing:
                await storage.add_task_blocked_by(task_id, new)
        except Exception as exc:  # noqa: BLE001
            logger.warning("update_task_row blocked_by sync error: %s", exc)
    return {"id": task_id, **column_payload}


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
