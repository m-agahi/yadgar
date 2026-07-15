"""Phase 3.5 — memorize soft-gate (Car 2 Part B).

A NON-BLOCKING near-duplicate check for DURABLE writes only. Runs AFTER
phase_embed (so ctx.embedding is available) and BEFORE phase_store. It NEVER
returns a rejection — it stashes ``ctx.near_duplicates`` (up to
MEMORIZE_SIM_TOP_K), which _phase_post_write attaches to the response. The
memory ALWAYS stores; the dups are advisory (the write-side counterpart to the
read-first-write discipline: surface near-dups so the caller can choose to
UPDATE-in-place instead of accumulating redundant memories).

Trigger — caller-settable DURABLE signals ONLY (NOT store_type, which is
"episodic" at gate time and set by the CLS classifier post-gate in phase_store):
  * tags ∩ {feedback, decision, _anchor}, OR
  * is_protected=True, OR
  * any anchor tier set (semantic_immortal / conditional / ephemeral).
Episodic writes (none of the above) BYPASS entirely — keeps the hot path cheap.

Config: YADGAR_MEMORIZE_SIM_GATE_ENABLED (default true),
YADGAR_MEMORIZE_SIM_THRESHOLD (cosine, default 0.85, stricter than the wiki
0.80), YADGAR_MEMORIZE_SIM_TOP_K (default 3).
"""

from __future__ import annotations

import logging

import yadgar._shared.runtime.lifecycle as _lifecycle
from yadgar._shared.observability.observe import observe
from yadgar._shared.observability.tracing import trace_span
from yadgar._shared.write_exec import MemorizeContext

logger = logging.getLogger(__name__)

# Tags that mark a write as DURABLE (worth a near-dup surface).
_DURABLE_TAGS: frozenset[str] = frozenset({"feedback", "decision", "_anchor"})


@observe(tier="stage", metric="write_exec.phase_soft_gate._is_durable")
def _is_durable_write(ctx: MemorizeContext) -> bool:
    """True when caller-settable signals mark this write as durable (gate applies)."""
    if ctx.is_protected:
        return True
    if ctx.tier is not None:
        return True
    return bool(_DURABLE_TAGS.intersection(ctx.tags))


@trace_span()
def phase_soft_gate(ctx: MemorizeContext, settings) -> None:
    """Attach ctx.near_duplicates for durable writes. NEVER blocks (returns None).

    No-op when the gate is disabled, the write is episodic (no durable signals),
    or the embedding is missing. Fully guarded — must never break the write path.
    """
    if not getattr(settings, "MEMORIZE_SIM_GATE_ENABLED", True):
        return
    if not _is_durable_write(ctx):
        return
    if ctx.embedding is None:
        return

    threshold = float(getattr(settings, "MEMORIZE_SIM_THRESHOLD", 0.85))
    top_k = int(getattr(settings, "MEMORIZE_SIM_TOP_K", 3))

    try:
        storage = _lifecycle._get_storage()
        query_bytes = storage._floats_to_bytes(ctx.embedding)
        # Fetch a few extra so the threshold + self-exclusion prune still leaves top_k.
        raw = storage.search_vectors(query_bytes, top_k=top_k + 2, min_heat=0.0)
    except Exception:  # noqa: BLE001 — soft-gate is advisory; never break the write
        logger.debug("memorize soft-gate KNN failed (non-fatal)", exc_info=True)
        return

    dups: list[dict] = []
    for mid, dist in raw:
        # search_vectors returns distance = 1.0 - cosine_sim.
        sim = 1.0 - float(dist)
        if sim < threshold:
            continue
        # Exclude a just-stored self-match (memory_id is None at gate time — store
        # runs after — so a same-content prior row is a legitimate near-dup here).
        if ctx.memory_id is not None and mid == ctx.memory_id:
            continue
        try:
            existing = storage.get_memory(mid)
        except Exception:  # noqa: BLE001
            existing = None
        content = (existing or {}).get("content", "") if existing else ""
        dups.append({"id": mid, "content": content[:300], "score": round(sim, 4)})
        if len(dups) >= top_k:
            break

    ctx.near_duplicates = dups
    if dups:
        logger.info(
            "memorize soft-gate: %d near-duplicate(s) >= %.2f for a durable write "
            "(non-blocking; caller may UPDATE-in-place)",
            len(dups),
            threshold,
        )
