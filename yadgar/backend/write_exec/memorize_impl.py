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
    phase_soft_gate,
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
    reason: str = "",
    project_id: str | None = None,
) -> dict:
    """Run the full memorize pipeline synchronously (drain-replay).

    Called by the queue drainer's apply path. Runs validate → embed →
    contradiction → store → post_write. Never enqueues.

    ``reason`` carries the semantic_immortal anchor reason so phase_validate on
    the drainer side can re-check the require-reason invariant without re-calling
    the MCP shell. Defaults to "" for backwards compatibility with payloads that
    don't carry it (non-semantic_immortal tiers never set it).

    ``project_id`` (C4b, 0047 PR#40 §5) is the enqueue-time stamp resolved by
    the core ``memorize`` tool. It is carried onto ``MemorizeContext`` and
    reaches ``insert_memory`` as its ``caller_value`` through BOTH store
    branches — the curator path and the direct-insert fallback. This process
    cannot derive one (no git binary, no host project mounts; ADR-0227), so a
    ``None`` here means a legacy payload, not a licence to guess.
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
        project_id=project_id,
    )

    result = phase_validate(ctx, settings)
    if result is not None:
        return result

    result = phase_embed(ctx, settings)
    if result is not None:
        return result

    # Car 2 (Part B): non-blocking soft-gate — attaches ctx.near_duplicates for
    # durable writes (embedding now available). NEVER returns a rejection.
    phase_soft_gate(ctx, settings)

    result = phase_contradiction(ctx)
    if result is not None:
        return result

    phase_store(ctx)

    return phase_post_write(ctx, settings)
