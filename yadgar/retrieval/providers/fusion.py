"""Cross-type fusion layer for the unified fan-out recall path (v6 T6 Step 4).

Implements:
  1. Per-type quotas — neither memory nor wiki starves the pool.
  2. Pool across types (memory + wiki Candidates combined).
  3. Cross-encoder rerank over query↔Candidate.content for ALL types via the
     GTE reranker the Retriever owns.
  4. Additive prior boost: native_score (provider's own signal) folded in after
     CE reranking via RECALL_MEMORY_PRIOR_WEIGHT / RECALL_WIKI_PRIOR_WEIGHT.
  5. Cross-type dedup: if a memory Candidate's id appears in a wiki Candidate's
     source_memory_ids, keep only the higher-CE one.

CE relevance is the primary sort key; priors are tie-shapers (additive, not
replacement).  This mirrors the _apply_prior_boost pattern from retrieval/fusion.py.

Design note (pluggable strategy):
  A future consensus / landscape mode (issue #67) plugs in as another strategy
  by adding a new ``FusionStrategy`` value and a corresponding ``_fuse_*``
  dispatch branch in ``fuse()``.  This module deliberately does NOT implement
  consensus / MMR (that's #67).

Settings consumed (I25 three-way registered):
  RECALL_MEMORY_QUOTA   (int)   Default 5 — max memory candidates in the pool.
  RECALL_WIKI_QUOTA     (int)   Default 5 — max wiki candidates in the pool.
  RECALL_MEMORY_PRIOR_WEIGHT (float)  Default 0.1 — additive prior for memory native_score.
  RECALL_WIKI_PRIOR_WEIGHT   (float)  Default 0.1 — additive prior for wiki native_score.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from yadgar.retrieval.providers.base import Candidate

if TYPE_CHECKING:
    from yadgar.retrieval.core import Retriever

logger = logging.getLogger(__name__)


def _score_candidates_ce(
    candidates: list[Candidate],
    query: str,
    retriever: Retriever,
) -> list[tuple[Candidate, float]]:
    """Score all candidates using the Retriever's cross-encoder.

    Returns list of (Candidate, ce_score) sorted by ce_score descending.
    Falls back to native_score ordering if CE is unavailable.
    """
    if not candidates:
        return []

    texts = [c.content for c in candidates]
    ce_scores: list[float] | None = None

    try:
        ce_scores = retriever._reranker._ml.score_cross_encoder(query, texts)
    except Exception as exc:
        logger.debug("Cross-type CE scoring failed: %s — using native_score order", exc)

    if not ce_scores or not isinstance(ce_scores, (list, tuple)) or len(ce_scores) == 0:
        # Fallback: use native_score (sorted descending already).
        # Also guards against CE returning a non-list (e.g. mock objects in tests).
        return [(c, c.native_score) for c in candidates]

    # Pair each candidate with its CE score
    paired = list(zip(candidates, ce_scores, strict=False))
    paired.sort(key=lambda x: x[1], reverse=True)
    return paired


def _apply_type_prior(
    paired: list[tuple[Candidate, float]],
    memory_prior_weight: float,
    wiki_prior_weight: float,
) -> list[tuple[Candidate, float]]:
    """Fold native_score as an additive prior into CE scores.

    For each (candidate, ce_score), add:
      weight * candidate.native_score

    where weight is RECALL_MEMORY_PRIOR_WEIGHT for memory and
    RECALL_WIKI_PRIOR_WEIGHT for wiki.  Re-sorts by boosted score descending.
    """
    boosted: list[tuple[Candidate, float]] = []
    for cand, ce in paired:
        weight = memory_prior_weight if cand.type == "memory" else wiki_prior_weight
        score = ce + weight * cand.native_score
        boosted.append((cand, score))
    boosted.sort(key=lambda x: x[1], reverse=True)
    return boosted


def _cross_type_dedup(
    paired: list[tuple[Candidate, float]],
) -> list[tuple[Candidate, float]]:
    """Remove cross-type duplicates — keep higher-CE when a memory is a source of a wiki.

    If candidate_A is a memory with id=X, and candidate_B is a wiki whose
    source_memory_ids contains X, keep only the one with the higher score.
    The check is provenance-based: same content that was lifted from a memory into
    a wiki page should not appear twice.
    """
    # Build index: memory_id → (idx, score) for quick lookup
    memory_id_to_idx: dict[int, int] = {}
    for i, (cand, _score) in enumerate(paired):
        if cand.type == "memory" and isinstance(cand.id, int):
            memory_id_to_idx[cand.id] = i

    dropped: set[int] = set()  # indices to drop

    for wiki_idx, (wiki_cand, wiki_score) in enumerate(paired):
        if wiki_cand.type != "wiki":
            continue
        source_ids = wiki_cand.raw.get("source_memory_ids") or []
        for src_id in source_ids:
            mem_idx = memory_id_to_idx.get(src_id)
            if mem_idx is None or mem_idx in dropped or wiki_idx in dropped:
                continue
            _mem_cand, mem_score = paired[mem_idx]
            # Drop the lower-scored one (both are from the same provenance)
            if wiki_score >= mem_score:
                dropped.add(mem_idx)
            else:
                dropped.add(wiki_idx)

    return [(c, s) for i, (c, s) in enumerate(paired) if i not in dropped]


def fuse_candidates(
    memory_candidates: list[Candidate],
    wiki_candidates: list[Candidate],
    query: str,
    retriever: Retriever | None,
    max_results: int,
    settings,
) -> list[Candidate]:
    """Fuse memory and wiki candidates into a single ranked list.

    Steps:
      1. Apply per-type quotas (RECALL_MEMORY_QUOTA, RECALL_WIKI_QUOTA).
      2. Pool the quota-bounded candidates.
      3. Cross-encoder rerank (uses Retriever's GTE reranker or CE fallback).
         Falls back to native_score order when retriever is None or CE unavailable.
      4. Apply additive prior boost (RECALL_MEMORY_PRIOR_WEIGHT / RECALL_WIKI_PRIOR_WEIGHT).
      5. Cross-type dedup (provenance: memory id ∈ wiki.source_memory_ids).
      6. Trim to max_results.

    Args:
        memory_candidates: Candidates from MemoryProvider (sorted by native_score desc).
        wiki_candidates: Candidates from WikiProvider (sorted by native_score desc).
        query: Search query (for CE scoring).
        retriever: Retriever instance (owns the GTE reranker). May be None.
        max_results: Maximum candidates in the final output.
        settings: Settings instance carrying quota/prior weights.

    Returns:
        List of Candidates sorted by (CE-score + prior) descending, trimmed to max_results.
    """
    mem_quota = int(getattr(settings, "RECALL_MEMORY_QUOTA", 5))
    wiki_quota = int(getattr(settings, "RECALL_WIKI_QUOTA", 5))
    mem_prior_weight = float(getattr(settings, "RECALL_MEMORY_PRIOR_WEIGHT", 0.1))
    wiki_prior_weight = float(getattr(settings, "RECALL_WIKI_PRIOR_WEIGHT", 0.1))

    # Step 1: apply per-type quotas
    mem_pool = memory_candidates[:mem_quota]
    wiki_pool = wiki_candidates[:wiki_quota]

    # Step 2: pool
    pool = mem_pool + wiki_pool
    if not pool:
        return []

    # Step 3: cross-encoder rerank
    if retriever is not None:
        paired = _score_candidates_ce(pool, query, retriever)
    else:
        # No retriever — fall back to native_score
        paired = [(c, c.native_score) for c in pool]
        paired.sort(key=lambda x: x[1], reverse=True)

    # Step 4: additive prior boost
    paired = _apply_type_prior(paired, mem_prior_weight, wiki_prior_weight)

    # Step 5: cross-type dedup (provenance)
    paired = _cross_type_dedup(paired)

    # Step 6: trim to max_results
    top = paired[:max_results]
    return [c for c, _s in top]
