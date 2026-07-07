"""Phase 2 — resolve_branch: detect git branch + enqueue fast path.

Returns:
- dict with queued=True on successful enqueue (caller returns immediately)
- dict with error=missing_branch when branch absent and not draining
- None when draining (sync path continues to embed phase)
"""

from __future__ import annotations

import logging
import os

import yadgar.core.file_queue as _file_queue
from yadgar._shared.observability.observe import observe
from yadgar._shared.runtime.lifecycle import _get_file_queue
from yadgar._shared.tracing import trace_span

from .context import MemorizeContext

logger = logging.getLogger(__name__)


@trace_span("memorize.resolve_branch")
def phase_resolve_branch(ctx: MemorizeContext) -> dict | None:
    """Resolve branch context and enqueue on fast path.

    Mutations on ctx:
    - resolved_branch set from detection / branch_hint / env

    Returns:
    - dict(stored=True, queued=True, ...) — fast path success → caller returns
    - dict(error=missing_branch, ...) — hard reject
    - None — draining, continue to sync path
    """
    # v5.46.7: resolution order: _detect_branch(context) → branch_hint → YADGAR_CI_BRANCH
    branch = None
    try:
        import yadgar.core.server as _srv

        branch = _srv._detect_branch(ctx.context)
    except Exception:
        pass  # non-fatal — fall through to branch_hint

    # v5.42.3: branch_hint fallback
    if not branch and ctx.branch_hint:
        branch = ctx.branch_hint

    # v5.46.7: YADGAR_CI_BRANCH env fallback
    if not branch:
        branch = os.environ.get("YADGAR_CI_BRANCH") or None

    ctx.resolved_branch = branch

    # v5.42.3: hard-reject when branch absent and not draining
    if not branch and not _file_queue.is_draining():
        return {
            "error": "missing_branch",
            "stored": False,
            "message": (
                "Branch context required. Supply branch_hint=<current-branch-name> or ensure "
                "the working directory is a git repo accessible to the yadgar daemon."
            ),
            "field": "branch_hint",
            "op_type": "memorize",
        }

    # Fast path: enqueue and return (skip during drain replay)
    if not _file_queue.is_draining():
        return _enqueue(ctx)

    # Draining — continue to sync path
    return None


@observe(tier="stage")
def _enqueue(ctx: MemorizeContext) -> dict:
    """Enqueue memorize job. Returns queued result or falls through on error (returns None-like)."""
    try:
        payload: dict = {
            "content": ctx.content,
            "context": ctx.context,
            "tags": list(ctx.tags),
            "is_protected": ctx.is_protected,
            "branch": ctx.resolved_branch,
            "provenance_agent": ctx.provenance_agent_resolved,
        }
        if ctx.tier is not None:
            payload["tier"] = ctx.tier
        if ctx.computed_valid_until is not None:
            payload["valid_until"] = ctx.computed_valid_until
        job_id = _get_file_queue().enqueue("memorize", payload)
        return {"stored": True, "queued": True, "queue_id": job_id}
    except Exception as exc:
        logger.warning(
            "enqueue_failed",
            extra={
                "component": "memorize",
                "action": "enqueue",
                "outcome": "error",
                "error": type(exc).__name__,
                "fallback": "sync",
            },
        )
        # Fall through to sync path — return None signals "continue"
        return None  # type: ignore[return-value]
