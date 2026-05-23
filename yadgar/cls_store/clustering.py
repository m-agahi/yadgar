"""Clustering and embedding-similarity search for the CLS store."""

import logging

import numpy as np

from yadgar.tracing import trace_span

logger = logging.getLogger(__name__)


class _ClusteringMixin:
    """Mixin: find_recurring_patterns, _summarize_cluster, _search_store."""

    # ── Pattern Detection (Go-CLS) ───────────────────────────────────────

    @trace_span("consolidation.cls.find_patterns")
    def find_recurring_patterns(
        self, directory: str = None, min_occurrences: int = 3
    ) -> list[dict]:
        """Find clusters of similar episodic memories that recur across sessions.

        Algorithm:
        1. Get episodic memories (capped at CLS_PATTERN_MAX_CANDIDATES most-recent)
        2. Group by embedding similarity (threshold: CLUSTER_SIMILARITY_THRESHOLD)
           using vectorised numpy matmul — O(N·D) instead of O(N²) Python calls
        3. For each cluster with >= min_occurrences members:
           - Check session diversity (>= 2 different sessions)
           - Check generalizability (>= 2 directories OR same directory)
        4. Return qualifying clusters
        """
        max_candidates = self._settings.CLS_PATTERN_MAX_CANDIDATES
        # 1. Get episodic memories — cap to avoid O(N²) at scale
        if directory:
            memories = self._storage.get_memories_by_store_type(
                "episodic", directory=directory, limit=max_candidates
            )
        else:
            memories = self._storage.get_memories_by_store_type("episodic", limit=max_candidates)
        if len(memories) < min_occurrences:
            return []

        # 2. Vectorised greedy clustering via numpy matmul
        threshold = self._settings.CLUSTER_SIMILARITY_THRESHOLD

        # Build unit-normalised embedding matrix for memories with valid embeddings
        valid_mems: list[dict] = []
        unit_vecs: list[np.ndarray] = []
        for mem in memories:
            # Don't promote action-stream noise or already-abstracted semantics
            # to semantic — they are garbage at the episodic level.
            mem_tags = mem.get("tags") or []
            if isinstance(mem_tags, str):
                import json as _json

                mem_tags = _json.loads(mem_tags)
            if "_action_stream" in mem_tags or "auto-abstracted" in mem_tags:
                continue

            emb = mem.get("embedding")
            if not emb:
                continue
            try:
                arr = np.frombuffer(emb, dtype=np.float32)
                norm = np.linalg.norm(arr)
                if len(arr) == 0 or norm == 0:
                    continue
                unit_vecs.append(arr / norm)
                valid_mems.append(mem)
            except Exception:
                continue

        if len(valid_mems) < min_occurrences:
            return []

        # Pairwise cosine similarity via matrix multiplication (O(N·D))
        mat = np.stack(unit_vecs)  # N x D
        sim_matrix = mat @ mat.T  # N x N

        clusters: list[list[dict]] = []
        assigned: set[int] = set()

        for i, mem_a in enumerate(valid_mems):
            if mem_a["id"] in assigned:
                continue
            cluster = [mem_a]
            assigned.add(mem_a["id"])

            for j in range(i + 1, len(valid_mems)):
                mem_b = valid_mems[j]
                if mem_b["id"] in assigned:
                    continue
                if sim_matrix[i, j] >= threshold:
                    cluster.append(mem_b)
                    assigned.add(mem_b["id"])

            clusters.append(cluster)

        # 3. Filter by occurrence count and session/directory diversity
        qualifying = []
        for cluster in clusters:
            if len(cluster) < min_occurrences:
                continue

            # Session diversity: check source_episode_id → session_id
            session_ids = set()
            directories = set()
            for mem in cluster:
                directories.add(mem.get("directory_context", ""))
                ep_id = mem.get("source_episode_id")
                if ep_id is not None:
                    session_id = self._storage.get_episode_session_id(ep_id)
                    if session_id is not None:
                        session_ids.add(session_id)
                else:
                    # No episode linkage — treat created_at date as session proxy
                    created = mem.get("created_at", "")
                    if isinstance(created, str) and len(created) >= 10:
                        session_ids.add(created[:10])  # date part as proxy

            # Go-CLS: require >= 2 different sessions for generalizability
            if len(session_ids) < 2:
                continue

            qualifying.append(
                {
                    "memories": cluster,
                    "pattern_summary": self._summarize_cluster(cluster),
                    "occurrence_count": len(cluster),
                    "session_count": len(session_ids),
                    "directories": list(directories),
                }
            )

        return qualifying

    # ── Internal Helpers ──────────────────────────────────────────────────

    def _search_store(
        self,
        query: str,
        query_embedding: bytes,
        store_type: str,
        directory: str,
    ) -> list[tuple[dict, float]]:
        """Search a specific store (episodic or semantic) by embedding similarity.

        Scan is capped at CLS_PATTERN_MAX_CANDIDATES most-recently-accessed
        memories to prevent O(N) full-table scans from blocking consolidation.
        A full vector-index path (HNSW/MTREE) is the eventual target but not
        implemented here to keep the change surgical.
        """
        cap = self._settings.CLS_PATTERN_MAX_CANDIDATES
        if directory:
            memories = self._storage.get_memories_by_store_type(
                store_type, directory=directory, limit=cap
            )
        else:
            memories = self._storage.get_memories_by_store_type(store_type, limit=cap)

        results = []
        for mem in memories:
            if mem.get("embedding"):
                sim = self._embeddings.similarity(query_embedding, mem["embedding"])
                results.append((mem, sim))

        results.sort(key=lambda x: x[1], reverse=True)
        return results[:10]

    def _summarize_cluster(self, cluster: list[dict]) -> str:
        """Generate a brief summary of a cluster of memories."""
        contents = [m.get("content", "")[:100] for m in cluster[:3]]
        return " | ".join(contents)
