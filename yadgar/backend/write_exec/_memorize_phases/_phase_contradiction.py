"""Phase 4 — contradiction: LLM conflict resolver heuristic.

Handles the YADGAR_CONFLICT_RESOLVER=on path (C4 conflict resolution).
On success returns None (continue to store). On NOOP or DELETE returns
a rejection dict. On UPDATE handles the update and returns a success dict.
"""

from __future__ import annotations

import logging

from yadgar._shared.observability.observe import observe
from yadgar._shared.observability.tracing import trace_span
from yadgar._shared.runtime.lifecycle import _get_storage
from yadgar._shared.write_exec import MemorizeContext

logger = logging.getLogger(__name__)


@trace_span()
def phase_contradiction(ctx: MemorizeContext) -> dict | None:
    """Run C4 LLM conflict resolver if enabled.

    Returns:
    - dict on terminal result (NOOP, DELETE, UPDATE success) — caller returns
    - None to continue to store phase (ADD or fallback from failed UPDATE)
    """
    import os

    if os.environ.get("YADGAR_CONFLICT_RESOLVER", "off").lower() != "on":
        return None  # resolver disabled — continue to store

    try:
        from yadgar.backend.conflict_resolver import resolve_conflict

        candidate = {"content": ctx.content, "tags": list(ctx.tags), "context": ctx.context}
        result = resolve_conflict(candidate)
        op = result.get("op", "ADD")
        target_id = result.get("target_id")
        cr_reason = result.get("reason", "")
        logger.info("conflict_resolver: op=%s target_id=%s reason=%r", op, target_id, cr_reason)

        if op == "NOOP":
            return {"stored": False, "reason": "conflict_resolver_noop", "cr_reason": cr_reason}

        if op == "UPDATE" and target_id is not None:
            return _handle_update(ctx, target_id, cr_reason)

        if op == "DELETE" and target_id is not None:
            return _handle_delete(target_id, cr_reason)

    except Exception as exc:
        logger.warning("conflict_resolver outer error (%s) — degrading to ADD", exc)

    return None  # ADD or fallback — continue to store


@observe(tier="stage")
def _handle_update(ctx: MemorizeContext, target_id: int, cr_reason: str) -> dict | None:
    """Handle conflict resolver UPDATE op. Returns result dict or None (fallback to ADD)."""
    storage = _get_storage()
    try:
        storage.update_memory_fields(target_id, content=ctx.content, tags=list(ctx.tags))
        return {
            "stored": True,
            "action": "conflict_resolver_update",
            "memory_id": target_id,
            "cr_reason": cr_reason,
        }
    except Exception as exc:
        logger.warning("conflict_resolver UPDATE failed (%s), falling back to ADD", exc)
        return None  # fall through to ADD


@observe(tier="stage")
def _handle_delete(target_id: int, cr_reason: str) -> dict:
    """Handle conflict resolver DELETE op. Returns result dict."""
    storage = _get_storage()
    try:
        storage.delete_memory(target_id)
    except Exception as exc:
        logger.warning("conflict_resolver DELETE failed (%s), skipping", exc)
    return {"stored": False, "reason": "conflict_resolver_delete", "cr_reason": cr_reason}
