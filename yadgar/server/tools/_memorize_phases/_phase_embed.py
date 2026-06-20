"""Phase 3 — embed: write gate, contextual prefix, embedding, thermo scores."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import yadgar.server._state as _st
import yadgar.server.lifecycle as _lifecycle

from .context import MemorizeContext

logger = logging.getLogger(__name__)


def phase_embed(ctx: MemorizeContext, settings) -> dict | None:
    """Run write gate, generate embedding, compute thermo scores.

    Mutations on ctx:
    - gate_result set from write gate
    - gate_surprisal set from write gate (shadow mode — the GATE's surprisal, distinct
      from ctx.surprise which is the thermo score used for heat boost)
    - would_reject set: True if gate WOULD reject at WRITE_GATE_SHADOW_THRESHOLD
      (informational only — WRITE_GATE_THRESHOLD=0.0 means nothing is actually dropped)
    - contextual_prefix set from retriever
    - embedding set from embeddings engine
    - surprise, importance, valence, initial_heat set from thermo

    Returns rejection dict if write gate rejects, else None.
    """
    embeddings = _lifecycle._get_embeddings()

    # Predictive coding write gate — FIRST check before any storage
    if _st._write_gate is not None:
        should_store, surprisal, reason = _st._write_gate.should_store(
            ctx.content, ctx.context, ctx.tags
        )
        # Shadow mode: capture gate's surprisal (distinct from thermo ctx.surprise).
        # Pass the already-computed surprisal to would_reject_at() to avoid a second
        # embedding call. The shadow decision is faithful to the gate's adaptive logic.
        ctx.gate_surprisal = surprisal
        ctx.would_reject = _st._write_gate.would_reject_at(
            ctx.content,
            ctx.context,
            ctx.tags,
            settings.WRITE_GATE_SHADOW_THRESHOLD,
            surprisal=surprisal,
        )
        ctx.gate_result = {"surprisal": round(surprisal, 4), "gate_reason": reason}
        if not should_store:
            try:
                from yadgar.metrics import yadgar_writegate_outcome  # noqa: PLC0415

                yadgar_writegate_outcome.labels(outcome="skipped_low_surprise").inc()
            except Exception:
                pass
            return {
                "stored": False,
                "surprisal": round(surprisal, 4),
                "reason": reason,
                "message": "Memory below surprisal threshold, skipped",
            }

    # Generate contextual prefix for richer embedding semantics
    retriever = _st._retriever
    if retriever is not None and settings.CONTEXTUAL_PREFIX_ENABLED:
        ctx.contextual_prefix = retriever.generate_contextual_prefix(
            ctx.content, ctx.context, ctx.tags, datetime.now(UTC)
        )

    # Embed with contextual prefix prepended if available
    embed_text = f"{ctx.contextual_prefix}{ctx.content}" if ctx.contextual_prefix else ctx.content
    ctx.embedding = embeddings.encode(embed_text)

    # Compute thermodynamic scores
    thermo = _st._thermo
    if thermo is not None:
        ctx.surprise = thermo.compute_surprise(ctx.content, ctx.context)
        ctx.importance = thermo.compute_importance(ctx.content, ctx.tags)
        ctx.valence = thermo.compute_valence(ctx.content)
        ctx.initial_heat = thermo.apply_surprise_boost(1.0, ctx.surprise)
    else:
        ctx.surprise = 0.0
        ctx.importance = 0.5
        ctx.valence = 0.0
        ctx.initial_heat = 1.0

    return None
