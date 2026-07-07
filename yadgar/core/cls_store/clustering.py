"""Clustering and embedding-similarity search for the CLS store."""

import json as _json
import logging

import numpy as np

from yadgar._shared.observability.observe import observe

logger = logging.getLogger(__name__)


class _ClusteringMixin:
    """Mixin: find_recurring_patterns, _summarize_cluster, _search_store."""

    # ── Pattern Detection (Go-CLS) ───────────────────────────────────────

    @observe(tier="stage", metric="consolidation.cls.find_patterns")
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
        memories = self._fetch_episodic_candidates(directory)
        if len(memories) < min_occurrences:
            return []

        valid_mems, mat = self._build_unit_matrix(memories)
        if len(valid_mems) < min_occurrences:
            return []

        sim_matrix = mat @ mat.T
        threshold = self._settings.CLUSTER_SIMILARITY_THRESHOLD
        clusters = self._greedy_cluster(valid_mems, sim_matrix, threshold)

        return [
            result
            for cluster in clusters
            if (result := self._qualify_cluster(cluster, min_occurrences)) is not None
        ]

    # ── Pattern Detection Helpers ─────────────────────────────────────────

    @observe(tier="stage", metric="consolidation.cls.fetch_episodic_candidates")
    def _fetch_episodic_candidates(self, directory: str | None) -> list[dict]:
        """Fetch episodic memories capped at CLS_PATTERN_MAX_CANDIDATES."""
        cap = self._settings.CLS_PATTERN_MAX_CANDIDATES
        if directory:
            return self._storage.get_memories_by_store_type(
                "episodic", directory=directory, limit=cap
            )
        return self._storage.get_memories_by_store_type("episodic", limit=cap)

    @staticmethod
    def _mem_tags(mem: dict) -> list:
        """Return tags as a list (handles JSON-string encoding)."""
        tags = mem.get("tags") or []
        if isinstance(tags, str):
            tags = _json.loads(tags)
        return tags

    @observe(tier="stage", metric="consolidation.cls.build_unit_matrix")
    def _build_unit_matrix(self, memories: list[dict]) -> tuple[list[dict], np.ndarray]:
        """Filter noise tags and normalise embeddings into a unit matrix.

        Returns (valid_mems, N×D unit matrix).  Memories with invalid or
        missing embeddings, or tagged _action_stream / auto-abstracted, are
        excluded.
        """
        valid_mems: list[dict] = []
        unit_vecs: list[np.ndarray] = []
        for mem in memories:
            tags = self._mem_tags(mem)
            if "_action_stream" in tags or "auto-abstracted" in tags:
                continue
            vec = self._try_unit_vec(mem)
            if vec is None:
                continue
            unit_vecs.append(vec)
            valid_mems.append(mem)
        mat = np.stack(unit_vecs) if unit_vecs else np.empty((0, 0), dtype=np.float32)
        return valid_mems, mat

    @staticmethod
    def _try_unit_vec(mem: dict) -> np.ndarray | None:
        """Convert embedding bytes to a unit vector; return None on failure."""
        emb = mem.get("embedding")
        if not emb:
            return None
        try:
            arr = np.frombuffer(emb, dtype=np.float32)
            norm = np.linalg.norm(arr)
            if len(arr) == 0 or norm == 0:
                return None
            return arr / norm
        except Exception:
            return None

    @staticmethod
    @observe(tier="stage", metric="consolidation.cls.greedy_cluster")
    def _greedy_cluster(
        valid_mems: list[dict], sim_matrix: np.ndarray, threshold: float
    ) -> list[list[dict]]:
        """Greedy O(N²) cluster assignment using precomputed similarity matrix."""
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
        return clusters

    def _session_proxy(self, mem: dict) -> str | None:
        """Return a session identifier for a memory, or None if unresolvable."""
        ep_id = mem.get("source_episode_id")
        if ep_id is not None:
            return self._storage.get_episode_session_id(ep_id)
        created = mem.get("created_at", "")
        if isinstance(created, str) and len(created) >= 10:
            return created[:10]
        return None

    def _qualify_cluster(self, cluster: list[dict], min_occurrences: int) -> dict | None:
        """Return a result dict if cluster meets size + session-diversity thresholds.

        Returns None when the cluster does not qualify.
        """
        if len(cluster) < min_occurrences:
            return None
        session_ids: set[str] = set()
        directories: set[str] = set()
        for mem in cluster:
            directories.add(mem.get("directory_context", ""))
            proxy = self._session_proxy(mem)
            if proxy is not None:
                session_ids.add(proxy)
        if len(session_ids) < 2:
            return None
        return {
            "memories": cluster,
            "pattern_summary": self._summarize_cluster(cluster),
            "occurrence_count": len(cluster),
            "session_count": len(session_ids),
            "directories": list(directories),
        }

    # ── Internal Helpers ──────────────────────────────────────────────────

    @observe(tier="stage", metric="consolidation.cls.search_store")
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
