"""MMR (Maximal Marginal Relevance) reranking mixin — diversity-aware reranking."""

from __future__ import annotations

from yadgar.observability.observe import observe


def _cosine_sim(a, b) -> float:
    """Return cosine similarity between two numpy arrays, or 0.0 if either is None."""
    import numpy as np

    if a is None or b is None:
        return 0.0
    dot = np.dot(a, b)
    norm = np.linalg.norm(a) * np.linalg.norm(b)
    return float(dot / norm) if norm > 0 else 0.0


@observe(tier="hot", name="retrieval.mmr.collect_embeddings")
def _collect_candidate_embeddings(
    storage,
    memories: list[dict],
    q_arr,
) -> tuple[list[dict], list]:
    """Fetch embeddings for each memory; drop dim-mismatched, keep missing as None.

    Returns (valid_memories, mem_embeddings) where each valid_memories[i]
    corresponds to mem_embeddings[i].  Dim-mismatched memories are silently
    dropped; memories with no embedding record are kept with emb=None.
    """
    import numpy as np

    mem_embeddings: list = []
    valid_memories: list[dict] = []
    for mem in memories:
        # v5.97.0 Fix 2: prefer the embedding already carried on the fused result
        # dict (fusion._build_initial_results keeps it in-place after the batched
        # fetch). Only fall back to storage.get_memory for candidates without an
        # in-dict embedding — e.g. CE-diversity / comparison injects that never
        # went through the batched fusion hydration. This removes the redundant
        # per-candidate re-fetch (~183 ms marginal warm) while preserving exact
        # parity for injected candidates.
        raw_emb = mem.get("embedding")
        if not raw_emb:
            full_mem = storage.get_memory(mem["id"])
            raw_emb = full_mem.get("embedding") if full_mem else None
        if raw_emb:
            emb = np.frombuffer(raw_emb, dtype=np.float32)
            if len(emb) == len(q_arr):
                mem_embeddings.append(emb)
                valid_memories.append(mem)
            # else: dim mismatch — drop entirely (no append to either list)
        else:
            valid_memories.append(mem)
            mem_embeddings.append(None)
    return valid_memories, mem_embeddings


def _max_sim_to_selected(emb, selected_embs: list) -> float:
    """Return maximum cosine similarity between emb and any selected embedding.

    Returns 0.0 when selected_embs is empty or emb is None (floor matches
    original behaviour — cosine similarity can be negative, but we cap at 0).
    """
    max_sim = 0.0
    if selected_embs and emb is not None:
        for sel_emb in selected_embs:
            sim = _cosine_sim(emb, sel_emb)
            if sim > max_sim:
                max_sim = sim
    return max_sim


def _best_mmr_candidate(
    candidates: list[int],
    mem_embeddings: list,
    valid_memories: list[dict],
    selected_embs: list,
    lambda_param: float,
) -> int | None:
    """Pick the candidate index with the highest MMR score.

    MMR score = lambda * relevance - (1 - lambda) * max_similarity_to_selected.
    Ties broken by first (lowest) index — strict `>` comparison preserved.
    """
    best_idx = None
    best_score = -float("inf")
    for idx in candidates:
        emb = mem_embeddings[idx]
        relevance = valid_memories[idx].get("_retrieval_score", 0.0)
        max_sim = _max_sim_to_selected(emb, selected_embs)
        mmr_score = lambda_param * relevance - (1 - lambda_param) * max_sim
        if mmr_score > best_score:
            best_score = mmr_score
            best_idx = idx
    return best_idx


class _MMRMixin:
    """Provides mmr_rerank for diversity-aware candidate selection."""

    @observe(tier="stage", name="retrieval.mmr.rerank")
    def mmr_rerank(
        self,
        memories: list[dict],
        query_embedding: bytes | None,
        top_k: int = 5,
        lambda_param: float = 0.7,
    ) -> list[dict]:
        """Maximal Marginal Relevance for diversity-aware reranking.

        Balances relevance (lambda) vs diversity (1-lambda) to avoid
        returning top-K results that are all from the same conversation segment.
        """
        if not memories or len(memories) <= 1 or query_embedding is None:
            return memories[:top_k]

        import numpy as np

        q_arr = np.frombuffer(query_embedding, dtype=np.float32)

        valid_memories, mem_embeddings = _collect_candidate_embeddings(
            self._storage, memories, q_arr
        )

        if not valid_memories:
            return memories[:top_k]

        selected: list[dict] = []
        selected_embs: list = []
        candidates = list(range(len(valid_memories)))

        for _ in range(min(top_k, len(valid_memories))):
            best_idx = _best_mmr_candidate(
                candidates, mem_embeddings, valid_memories, selected_embs, lambda_param
            )
            if best_idx is not None:
                selected.append(valid_memories[best_idx])
                selected_embs.append(mem_embeddings[best_idx])
                candidates.remove(best_idx)

        return selected
