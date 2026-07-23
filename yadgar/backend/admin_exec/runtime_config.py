"""Backend execution bodies for the runtime_config write admin ops (ADR-0163, G1).

These are the storage-write halves of the runtime config store. Mirrors the
memory-block admin ops (``blocks.py``): the core ``@_tool`` shells (Car G3) will
keep validation + MCP schema and forward the write here over HTTP (POST /admin)
via ``_forward_admin``. Reads (config_get / config_list) stay core via
``_get_storage()`` — no read admin op, matching blocks.

Each op is an undecorated ``(payload: dict) -> dict`` function; the ``@observe``
decorators satisfy the I33 tri-signal ratchet. Error model mirrors the block ops:
``{ok: False, error: "..."}`` — never raise.

Only the WRITE ops (set / delete) exist for G1 so the forward path is registered.
Cache invalidation (whole-flush) is Car G2's concern, colocated core-side.
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
