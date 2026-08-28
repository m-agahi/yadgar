"""Cross-type fusion layer for the unified fan-out recall path (v6 T6 Step 4).

Implements:
  1. Per-type quotas — neither memory nor wiki starves the pool.
  2. Pool across types (memory + wiki Candidates combined).
  3. Cross-encoder scoring over query↔Candidate.content for ALL types via the
     GTE reranker the Retriever owns — gives a common relevance scale.
  4. Memory-order-stable merge: memory candidates are emitted in their NATIVE
     relative order (the order MemoryProvider returned them, which is already
     WRRF+GTE-ranked). The CE score for memory is used ONLY for wiki placement,
     never to reorder memories against each other.
  5. Wiki interleaving: each wiki candidate (sorted by CE + RECALL_WIKI_PRIOR_WEIGHT
     boost) is inserted before the first memory whose CE score is lower than the
     wiki's placement score, or appended if no such memory exists. This places
     relevant wiki pages at the top while leaving memory order intact.
  6. Cross-type dedup: if a memory Candidate's id appears in a wiki Candidate's
     source_memory_ids → keep only the higher-CE one (provenance gate).

Design note — why CE-score memories but not sort by it:
  MemoryProvider has already applied WRRF+GTE fusion which correctly combines
  BM25, vector, heat, and cross-encoder signals. Re-sorting by CE alone discards
  WRRF weights and degrades MRR (measured: 0.81→0.63 regression). The fix is to
  use CE as a common yardstick for wiki→memory placement (honest cross-scale
  comparison) while preserving the WRRF-determined memory order.

Design note (pluggable strategy):
  A future consensus / landscape mode (issue #67) plugs in as another strategy
  by adding a new ``FusionStrategy`` value and a corresponding ``_fuse_*``
  dispatch branch in ``fuse()``.  This module deliberately does NOT implement
  consensus / MMR (that's #67).

Settings consumed (I25 three-way registered):
  RECALL_MEMORY_QUOTA   (int)   Default 5 — max memory candidates in the pool.
  RECALL_WIKI_QUOTA     (int)   Default 5 — max wiki candidates in the pool.
  RECALL_MEMORY_PRIOR_WEIGHT (float)  Default 0.1 — prior for memory in provenance-dedup
                                       score comparison (keeps the setting consumed).
  RECALL_WIKI_PRIOR_WEIGHT   (float)  Default 0.1 — additive boost to wiki CE score
                                       for placement into the memory-ordered list.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from yadgar._shared.observability.observe import observe
from yadgar.backend.retrieval.providers.base import Candidate

if TYPE_CHECKING:
    from yadgar.backend.retrieval.core import Retriever

logger = logging.getLogger(__name__)


@observe(tier="hot", metric="retrieval.crossfuse.score_candidates_ce")
def _score_candidates_ce(
    candidates: list[Candidate],
    query: str,
    retriever: Retriever,
) -> dict[int, float]:
    """Score all candidates using the Retriever's cross-encoder.

    Returns a dict mapping candidate index (position in input list) → ce_score.
    Falls back to native_score when CE is unavailable.
    """
    if not candidates:
        return {}

    texts = [c.content for c in candidates]
    ce_scores: list[float] | None = None

    try:
        # Car 1 (#41): route crossfuse CE scoring through the ce cache so the
        # scores computed here are REUSED by cross_encoder / multi_passage later
        # in the same request (within-request dedup; get-or-compute).
        ce_scores = retriever._reranker.score_ce_cached(query, texts)
    except Exception as exc:  # BLE001-KEEP: ML client boundary for cross-type CE scoring; same untypeable local-torch-or-remote-httpx surface, and the fallback to native_score is on the next line
        logger.debug("Cross-type CE scoring failed: %s — using native_score fallback", exc)

    if not ce_scores or not isinstance(ce_scores, (list, tuple)) or len(ce_scores) == 0:
        # Fallback: use native_score as the CE proxy.
        return {i: c.native_score for i, c in enumerate(candidates)}

    return {i: float(score) for i, score in enumerate(ce_scores[: len(candidates)])}


@observe(tier="hot", metric="retrieval.crossfuse.cross_type_dedup")
def _cross_type_dedup(
    result: list[tuple[Candidate, float]],
    memory_prior_weight: float,
) -> list[tuple[Candidate, float]]:
    """Remove cross-type duplicates — keep higher-scored when a memory is a source of a wiki.

    If candidate_A is a memory with id=X, and candidate_B is a wiki whose
    source_memory_ids contains X, keep only the one with the higher score
    (ce_score + memory_prior_weight * native_score for memories; placement_score for wiki).
    The check is provenance-based: same content that was lifted from a memory into
    a wiki page should not appear twice.

    memory_prior_weight: folded into memory score for dedup comparison (keeps the
    RECALL_MEMORY_PRIOR_WEIGHT setting consumed without reordering memories).
    """
    # Build index: memory_id → (idx, score) for quick lookup
    memory_id_to_idx: dict[int, int] = {}
    for i, (cand, _score) in enumerate(result):
        if cand.type == "memory" and isinstance(cand.id, int):
            memory_id_to_idx[cand.id] = i

    dropped: set[int] = set()  # indices to drop

    for wiki_idx, (wiki_cand, wiki_score) in enumerate(result):
        if wiki_cand.type != "wiki":
            continue
        source_ids = wiki_cand.raw.get("source_memory_ids") or []
        for src_id in source_ids:
            mem_idx = memory_id_to_idx.get(src_id)
            if mem_idx is None or mem_idx in dropped or wiki_idx in dropped:
                continue
            _mem_cand, mem_ce = result[mem_idx]
            # Fold memory prior into the comparison score (keeps RECALL_MEMORY_PRIOR_WEIGHT consumed).
            mem_score = mem_ce + memory_prior_weight * _mem_cand.native_score
            # Drop the lower-scored one (both are from the same provenance)
            if wiki_score >= mem_score:
                dropped.add(mem_idx)
            else:
                dropped.add(wiki_idx)

    return [(c, s) for i, (c, s) in enumerate(result) if i not in dropped]


@observe(tier="hot", metric="retrieval.crossfuse.interleave_wiki")
def _interleave_wiki_into_memories(
    mem_pool: list[Candidate],
    wiki_candidates_with_score: list[tuple[Candidate, float]],
    mem_ce_scores: dict[int, float],
) -> list[tuple[Candidate, float]]:
    """Merge wiki candidates into the memory-ordered list by CE placement score.

    Memory candidates are emitted in their NATIVE order (position in mem_pool).
    Each wiki is inserted before the first memory whose CE score is lower than
    the wiki's placement score. If no such memory exists, the wiki is appended.

    This guarantees:
      - Memory relative order is NEVER changed (mem_pool order = output memory order).
      - Wiki placement is determined by CE relevance vs the memory sequence.
      - A highly-relevant wiki can reach rank 1 (inserted before all memories).

    Args:
        mem_pool: Memory candidates in native WRRF+GTE order (index i → mem_ce_scores[i]).
        wiki_candidates_with_score: List of (wiki_cand, placement_score) sorted by
            placement_score descending (most relevant wiki first).
        mem_ce_scores: CE score for each memory at its index in mem_pool.

    Returns:
        Merged list of (Candidate, score) with memories in native order and wiki
        interleaved by relevance.
    """
    # Start with a mutable copy of the memory sequence (preserving native order).
    # Each slot is (candidate, ce_score_for_placement_comparison).
    result: list[tuple[Candidate, float]] = [
        (mem, mem_ce_scores.get(i, mem.native_score)) for i, mem in enumerate(mem_pool)
    ]

    # Insert each wiki in placement_score order (highest first) so that ties in
    # wiki scores don't flip the wiki relative order relative to each other.
    for wiki_cand, wiki_score in wiki_candidates_with_score:
        # Find insertion point: first memory slot with CE < wiki placement score.
        insert_at = len(result)
        for j, (slot_cand, slot_score) in enumerate(result):
            if slot_cand.type == "memory" and slot_score < wiki_score:
                insert_at = j
                break
        result.insert(insert_at, (wiki_cand, wiki_score))

    return result


@observe(tier="stage", metric="retrieval.crossfuse.fuse_candidates")
def fuse_candidates(
    memory_candidates: list[Candidate],
    wiki_candidates: list[Candidate],
    query: str,
    retriever: Retriever | None,
    max_results: int,
    settings,
    profile: str | None = None,
) -> list[Candidate]:
    """Fuse memory and wiki candidates into a single ranked list.

    Memory-order-stable fusion algorithm:

    1. Apply per-type quotas (RECALL_MEMORY_QUOTA, RECALL_WIKI_QUOTA).
    2. CE-score ALL candidates (memory + wiki) against the query.
       This gives a common relevance scale for cross-type comparison.
       Falls back to native_score when retriever is None, CE unavailable,
       OR profile="fast" (fast skips the cross-type CE pass entirely).
    3. Emit memory candidates in NATIVE order (the order MemoryProvider returned
       them — WRRF+GTE already applied). Never reorder memories by CE.
    4. Sort wiki candidates by (CE + RECALL_WIKI_PRIOR_WEIGHT * native_score)
       descending, then interleave each wiki into the memory-ordered list by
       inserting it before the first memory with a lower CE score.
       This places a relevant wiki at rank 1 if it outscores all memories,
       without disturbing the memory sequence.
    5. Cross-type dedup: memory.id ∈ wiki.source_memory_ids → keep higher-scored
       (provenance gate). RECALL_MEMORY_PRIOR_WEIGHT is folded into the memory
       comparison score here (keeps the setting consumed).
    6. Trim to max_results.

    Net effect for a memory-only-relevant query: type=all returns memories in
    the SAME relative order as legacy recall (MRR preserved), with any relevant
    wiki inserted by relevance (R@10 coverage preserved).

    Args:
        memory_candidates: Candidates from MemoryProvider (sorted by native_score desc).
        wiki_candidates: Candidates from WikiProvider (sorted by native_score desc).
        query: Search query (for CE scoring).
        retriever: Retriever instance (owns the GTE reranker). May be None.
        max_results: Maximum candidates in the final output.
        settings: Settings instance carrying quota/prior weights.
        profile: Optional rerank level ("fast", "balanced", "full", None).
            When "fast", the cross-type CE pass is skipped (native_score proxy used
            for wiki placement). Matches Phase 1 §3.1 fusion-CE gate.

    Returns:
        List of Candidates with memories in native order + wiki interleaved by
        relevance, trimmed to max_results.
    """
    mem_quota = int(settings.RECALL_MEMORY_QUOTA)
    wiki_quota = int(settings.RECALL_WIKI_QUOTA)
    mem_prior_weight = float(settings.RECALL_MEMORY_PRIOR_WEIGHT)
    wiki_prior_weight = float(settings.RECALL_WIKI_PRIOR_WEIGHT)

    # Step 1: apply per-type quotas
    mem_pool = memory_candidates[:mem_quota]
    wiki_pool = wiki_candidates[:wiki_quota]

    if not mem_pool and not wiki_pool:
        return []

    # Step 2: CE-score all candidates for a common relevance yardstick.
    # Memory scores are used ONLY for wiki placement comparison, never for
    # resorting memories against each other.
    # Phase 1 §3.1 fusion-CE gate: when profile="fast", skip CE (native_score proxy).
    # This matches the memory-arm CE gate in Retriever.recall(profile="fast") so the
    # full fanout fast path avoids ALL CE inference — latency budget for the hook.
    all_candidates = mem_pool + wiki_pool
    _skip_ce = profile == "fast"
    if retriever is not None and not _skip_ce:
        all_scores = _score_candidates_ce(all_candidates, query, retriever)
    else:
        # No retriever OR profile="fast" — fall back to native_score as the CE proxy.
        all_scores = {i: c.native_score for i, c in enumerate(all_candidates)}

    # Split scores back per type.
    mem_ce_scores: dict[int, float] = {}  # index in mem_pool → ce_score
    wiki_ce_scores: dict[int, float] = {}  # index in wiki_pool → ce_score
    for i, cand in enumerate(all_candidates):
        score = all_scores.get(i, cand.native_score)
        if cand.type == "memory":
            mem_ce_scores[i] = score
        else:
            # wiki index in wiki_pool = i - len(mem_pool)
            wiki_ce_scores[i - len(mem_pool)] = score

    # Step 3: prepare wiki candidates with placement score
    # (CE + RECALL_WIKI_PRIOR_WEIGHT * native_score), sorted best-first.
    # Car C7 (0047, absorbing C8 item 5): the C2 "downweight" multiply that
    # stood here is DELETED, and a verified sign bug with it. It multiplied
    # ``ce + wiki_prior_weight * native_score`` by a factor in (0, 1) — but
    # ``ce`` is a raw cross-encoder logit and is commonly NEGATIVE, so the
    # multiply RAISED the score. The penalty PROMOTED exactly the pages it was
    # written to sink. Its only user (``task_list``) is now
    # ``recall_disposition="exclude"``, so those pages are never fetched: the
    # stage-1 WHERE drops them before they can consume a pool slot, which is
    # strictly better than re-ranking them afterwards. A future genuine soft
    # sink must SUBTRACT or CLAMP, never multiply.
    wiki_with_placement: list[tuple[Candidate, float]] = []
    for j, wiki_cand in enumerate(wiki_pool):
        ce = wiki_ce_scores.get(j, wiki_cand.native_score)
        placement_score = ce + wiki_prior_weight * wiki_cand.native_score
        wiki_with_placement.append((wiki_cand, placement_score))
    # C4.0 (ADR-0108 A): break equal placement scores by candidate id desc so
    # wiki placement is a deterministic total order. wiki candidate ids are
    # homogeneous slug strings here, so `(score, id)` desc is well-ordered; the
    # tie-break also feeds the max_results trim in Step 6, so equal-score wikis
    # survive the cut deterministically.
    wiki_with_placement.sort(key=lambda x: (x[1], x[0].id), reverse=True)

    # Step 4: interleave wiki into the memory-ordered list.
    # mem_pool order is PRESERVED — memories never reordered by CE.
    merged = _interleave_wiki_into_memories(mem_pool, wiki_with_placement, mem_ce_scores)

    # Step 5: cross-type dedup (provenance: memory.id ∈ wiki.source_memory_ids).
    # RECALL_MEMORY_PRIOR_WEIGHT is folded into the dedup comparison score.
    merged = _cross_type_dedup(merged, mem_prior_weight)

    # Step 6: trim to max_results
    top = merged[:max_results]
    return [c for c, _s in top]
