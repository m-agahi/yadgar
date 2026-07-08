"""Backend memorize replay entry (R3 Car 1 write-half).

The queue drainer replays a queued ``memorize`` job by running the full write
pipeline synchronously against storage — WITHOUT re-enqueueing. The enqueue
fast-path lives in the core memorize shell; this module owns the sync execution
that the drainer invokes during drain-replay.

Imports ``_shared`` + backend only — no ``yadgar.core.*`` edge.
"""

from __future__ import annotations

import logging

from yadgar._shared.config import get_settings
from yadgar._shared.observability.observe import observe

from ._memorize_phases import (
    MemorizeContext,
    phase_contradiction,
    phase_embed,
    phase_post_write,
    phase_resolve_branch,
    phase_store,
    phase_validate,
)

logger = logging.getLogger(__name__)


@observe(tier="boundary", metric="write_exec.memorize_replay")
def run_memorize_replay(  # noqa: PLR0913 — mirrors the memorize MCP signature
    content: str,
    context: str,
    tags: list[str],
    is_protected: bool = False,
    provenance_agent: str | None = None,
    tier: str | None = None,
    valid_until: str | None = None,
    branch: str | None = None,
    reason: str = "",
) -> dict:
    """Run the full memorize pipeline synchronously (drain-replay).

    Called by the queue drainer's apply path. Runs validate → resolve_branch →
    embed → contradiction → store → post_write. Never enqueues.

    ``branch`` is the enqueue-time branch captured in the queue payload; it is
    threaded through ctx.branch_hint so phase_resolve_branch resolves it.

    ``reason`` carries the semantic_immortal anchor reason so phase_validate on
    the drainer side can re-check the require-reason invariant without re-calling
    the MCP shell. Defaults to "" for backwards compatibility with payloads that
    don't carry it (non-semantic_immortal tiers never set it).
    """
    settings = get_settings()

    ctx = MemorizeContext(
        content=content,
        context=context,
        tags=list(tags),
        is_protected=is_protected,
        provenance_agent=provenance_agent,
        tier=tier,
        valid_until=valid_until,
        ttl_days=None,  # valid_until already computed before enqueue
        reason=reason,
        branch_hint=branch,
    )

    result = phase_validate(ctx, settings)
    if result is not None:
        return result

    result = phase_resolve_branch(ctx)
    if result is not None:
        return result

    result = phase_embed(ctx, settings)
    if result is not None:
        return result

    result = phase_contradiction(ctx)
    if result is not None:
        return result

    phase_store(ctx)

    return phase_post_write(ctx, settings)
