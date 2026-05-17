"""MMR (Maximal Marginal Relevance) reranking mixin — diversity-aware reranking."""

from __future__ import annotations


class _MMRMixin:
    """Provides mmr_rerank for diversity-aware candidate selection."""

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

        # Get embeddings for all candidate memories
        mem_embeddings = []
        valid_memories = []
        for mem in memories:
            full_mem = self._storage.get_memory(mem["id"])
            if full_mem and full_mem.get("embedding"):
                emb = np.frombuffer(full_mem["embedding"], dtype=np.float32)
                # Handle dimension mismatch
                if len(emb) == len(q_arr):
                    mem_embeddings.append(emb)
                    valid_memories.append(mem)
            else:
                valid_memories.append(mem)
                mem_embeddings.append(None)

        if not valid_memories:
            return memories[:top_k]

        def cosine_sim(a, b):
            if a is None or b is None:
                return 0.0
            dot = np.dot(a, b)
            norm = np.linalg.norm(a) * np.linalg.norm(b)
            return float(dot / norm) if norm > 0 else 0.0

        selected = []
        selected_embs = []
        candidates = list(range(len(valid_memories)))

        for _ in range(min(top_k, len(valid_memories))):
            best_idx = None
            best_score = -float("inf")

            for idx in candidates:
                emb = mem_embeddings[idx]
                relevance = valid_memories[idx].get("_retrieval_score", 0.0)

                # Max similarity to already-selected documents
                max_sim = 0.0
                if selected_embs and emb is not None:
                    for sel_emb in selected_embs:
                        sim = cosine_sim(emb, sel_emb)
                        if sim > max_sim:
                            max_sim = sim

                mmr_score = lambda_param * relevance - (1 - lambda_param) * max_sim

                if mmr_score > best_score:
                    best_score = mmr_score
                    best_idx = idx

            if best_idx is not None:
                selected.append(valid_memories[best_idx])
                selected_embs.append(mem_embeddings[best_idx])
                candidates.remove(best_idx)

        return selected
