"""Backend execution bodies for the runtime_config admin ops (ADR-0163, G1 + Car B).

Storage half of the runtime config store. Mirrors the memory-block admin ops
(``blocks.py``): the core ``@_tool`` shells keep validation + MCP schema and
forward the write over HTTP (POST /admin) via ``_forward_admin``.

WRITE ops (``runtime_config_set`` / ``runtime_config_delete``) were G1. READ ops
(``get_config_row`` / ``list_config_rows``) are Car B — they close the
in-process ``_get_storage()`` read violation on ``core/server/tools/
_runtime_config.py`` (ADR-0078 / ADR-0200: core touches zero DB directly).
The read bodies stay SYNC because the storage half (``_RuntimeConfigMixin`` on
``StorageEngine``) is the SurrealDB sync path.

Each op is an undecorated ``(payload: dict) -> dict`` function; ``@observe``
satisfies the I33 tri-signal ratchet. Error model mirrors the block ops:
``{ok: False, error: "..."}`` — never raise.
"""

from __future__ import annotations

import logging

from yadgar._shared.observability.observe import observe
from yadgar._shared.runtime.lifecycle import _get_storage

logger = logging.getLogger(__name__)


@observe(tier="boundary", metric="backend.admin.runtime_config_set")
def runtime_config_set(payload: dict) -> dict:
    """Upsert a runtime_config (key, directory) → value. Storage-write half.

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
        logger.warning("runtime_config_set error key=%s: %s", payload.get("key"), exc)
        return {"ok": False, "error": str(exc)}


@observe(tier="boundary", metric="backend.admin.runtime_config_delete")
def runtime_config_delete(payload: dict) -> dict:
    """Delete a runtime_config (key, directory) row (idempotent). Storage-write half.

    payload: {key, directory}
    """
    storage = _get_storage()
    key = payload["key"]
    try:
        storage.delete_config_row(key, directory=payload.get("directory"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("runtime_config_delete error key=%s: %s", key, exc)
        return {"ok": False, "error": str(exc)}
    return {"deleted": True, "key": key}


# Car B: READ ops. The core ``_runtime_config.py`` config reads now forward
# over HTTP via ``_forward_admin("get_config_row", ...)`` / ``list_config_rows``
# — closing the in-process ``_get_storage()`` read violation.


@observe(tier="boundary", metric="backend.admin.get_config_row")
def get_config_row(payload: dict) -> dict:
    """Exact (key, directory) row lookup. payload: {key, directory}."""
    storage = _get_storage()
    try:
        row = storage.get_config_row(
            payload["key"],
            directory=payload.get("directory"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_config_row error key=%s: %s", payload.get("key"), exc)
        return {"ok": False, "error": str(exc)}
    return {"row": row}


@observe(tier="boundary", metric="backend.admin.list_config_rows")
def list_config_rows(payload: dict) -> dict:
    """List rows. payload: {directory?: str | None | sentinel}.

    When ``directory`` key is absent, the ``_RuntimeConfigMixin`` sentinel
    branch returns ALL rows (global + every directory). When ``directory`` is
    ``None``, only global rows. When ``directory`` is a string, that dir only.
    The core resolver never calls ``list_config_rows`` directly today (G3's
    ``config_list`` tool is the wired caller; Car B exposes the admin op so a
    future warmer / tool can use it without touching the DB directly).
    """
    storage = _get_storage()
    try:
        # Pass only the keys the caller actually supplied — the mixin's sentinel
        # branch treats "no directory key" as the explicit "ALL rows" intent.
        if "directory" in payload:
            rows = storage.list_config_rows(directory=payload["directory"])
        else:
            rows = storage.list_config_rows()
    except Exception as exc:  # noqa: BLE001
        logger.warning("list_config_rows error: %s", exc)
        return {"ok": False, "error": str(exc)}
    return {"rows": rows}


#: The GLOBAL runtime config key that turns the yadgar task ledger off for
#: every project (yaadgaar #699). Mirrors core ``task_ledger_gate.LEDGER_DISABLED_KEY``
#: (backend never imports core; a test pins the two strings equal).
LEDGER_DISABLED_KEY = "tasks.ledger_disabled"


@observe(tier="stage", metric="backend.admin.runtime_config.task_ledger_disabled")
def task_ledger_disabled() -> bool:
    """True only when the global ``tasks.ledger_disabled`` row holds JSON ``true``.

    The backend half of the kill switch: the core gate refuses ``task_write``
    first, and this read lets the backend ledger ops and the nightly sweep
    refuse too, so a raw ``POST /admin`` caller cannot write a frozen ledger.

    Reads through ``get_config_row`` above (global scope: no directory in the
    payload), so it shares that op's storage call and fault model. Same
    semantics as the core gate: absent, ``false``, malformed or an unreadable
    row is off. A failed read (an exception, or the op's ``{"ok": False}``
    fault body) logs a warning and is off for this call only; nothing is
    cached here.
    """
    try:
        result = get_config_row({"key": LEDGER_DISABLED_KEY})
    except Exception as exc:  # noqa: BLE001 — a config read must never break a ledger op or the sweep; a failed read is "off" for this call only, logged below
        result = {"ok": False, "error": repr(exc)}
    if "row" not in result:
        logger.warning(
            "%s read failed (%s); the yadgar task ledger stays on for this call",
            LEDGER_DISABLED_KEY,
            result.get("error"),
        )
        return False
    row = result["row"]
    return isinstance(row, dict) and row.get("value") is True
