"""Community detection and cluster summarization mixin for SleepComputeEngine."""

from __future__ import annotations

import re
from collections import Counter

import numpy as np

from yadgar.observability.observe import observe
from yadgar.tracing import trace_span

# Entity-like patterns for identifying key sentences during compression
_ENTITY_PATTERN_RE = re.compile(
    r"(?:[\w@.-]+/[\w@.-]+\.\w+"  # file paths
    r"|\bdef\s+\w+"  # python defs
    r"|\bclass\s+\w+"  # python classes
    r"|\b\w*(?:Error|Exception)\b"  # error types
    r"|\bimport\s+\w+"  # imports
    r"|\bfrom\s+\w+)",  # from imports
)

_STOP_WORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "and",
        "or",
        "to",
        "in",
        "of",
        "for",
        "with",
        "on",
        "at",
        "by",
        "from",
        "this",
        "that",
        "it",
        "not",
        "be",
        "as",
        "has",
        "have",
    }
)


def _top_keywords(text: str, n: int = 5) -> list[str]:
    """Return the top *n* keywords from *text*, excluding common stop words."""
    words = text.lower().split()
    meaningful = [w for w in words if w not in _STOP_WORDS and len(w) > 2]
    return [w for w, _ in Counter(meaningful).most_common(n)]


@observe(tier="stage")
def _compute_centroid(rows: list[dict]) -> bytes | None:
    """Compute a normalized centroid embedding from *rows*, or None if no embeddings."""
    embeddings_list = [m["embedding"] for m in rows if m.get("embedding") is not None]
    if not embeddings_list:
        return None
    arrays = [np.frombuffer(e, dtype=np.float32) for e in embeddings_list]
    centroid_arr = np.mean(arrays, axis=0).astype(np.float32)
    norm = np.linalg.norm(centroid_arr)
    if norm > 0:
        centroid_arr = centroid_arr / norm
    return centroid_arr.tobytes()


class _CommunityMixin:
    """Community detection and cluster summarization operations."""

    @trace_span("sleep.detect_communities")
    def detect_communities(self) -> list[dict]:
        """Build a networkx graph from entity relationships and detect communities."""
        import networkx as nx

        entities = self._storage.get_all_entities(min_heat=0.0, include_archived=False)
        if not entities:
            return []

        entity_map = {e["id"]: e for e in entities}

        G = nx.Graph()
        for e in entities:
            G.add_node(e["id"], name=e["name"], type=e["type"])

        # Build edges — ONE bulk fetch instead of O(N²) per-pair HTTP calls.
        for rel in self._storage.get_all_relationships():
            src_id = rel.get("source_entity_id")
            tgt_id = rel.get("target_entity_id")
            if src_id in entity_map and tgt_id in entity_map:
                G.add_edge(src_id, tgt_id, weight=rel.get("weight") or 1.0)

        if G.number_of_edges() == 0:
            return []

        # Run Louvain community detection with label propagation fallback
        try:
            from networkx.algorithms.community import louvain_communities

            communities = louvain_communities(G, seed=42)
        except Exception:
            from networkx.algorithms.community import label_propagation_communities

            communities = list(label_propagation_communities(G))

        results = []
        for comm_idx, community in enumerate(communities):
            if len(community) < 2:
                continue

            # Collect entity names for this community
            member_names = []
            for eid in community:
                if eid in entity_map:
                    member_names.append(entity_map[eid]["name"])

            # Summary from top entity names
            top_names = sorted(member_names)[:5]
            summary = ", ".join(top_names)
            cluster_name = f"community_{comm_idx}"

            # Find memories associated with these entities
            memory_ids = self._find_memories_for_entities(member_names)

            # Create cluster record
            cluster_id = self._storage.insert_cluster(
                {
                    "name": cluster_name,
                    "level": 1,
                    "summary": summary,
                    "member_count": len(memory_ids),
                }
            )

            # Assign memories to this cluster
            for mid in memory_ids:
                self._storage.update_memory_fields(mid, cluster_id=cluster_id)

            results.append(
                {
                    "cluster_id": cluster_id,
                    "name": cluster_name,
                    "entity_count": len(community),
                    "member_count": len(memory_ids),
                }
            )

        return results

    @observe(tier="stage")
    def _find_memories_for_entities(self, entity_names: list[str]) -> list[int]:
        """Find memory IDs whose content mentions any of the given entity names."""
        if not entity_names:
            return []

        memory_ids: set[int] = set()
        all_memories = self._storage.get_all_memories_for_decay()

        for mem in all_memories:
            if mem.get("heat", 0) <= 0:
                continue
            content = mem.get("content", "")
            for name in entity_names:
                if name in content:
                    memory_ids.add(mem["id"])
                    break

        return list(memory_ids)

    @trace_span("sleep.generate_cluster_summaries")
    def generate_cluster_summaries(self) -> None:
        """Generate summaries and centroid embeddings for clusters with > 3 members."""
        clusters = self._storage.get_clusters_by_level(1)
        all_memories = self._storage.get_all_memories_with_embeddings()

        for cluster in clusters:
            if cluster["member_count"] <= 3:
                continue

            cluster_id = cluster["id"]
            rows = [
                m
                for m in all_memories
                if m.get("cluster_id") == cluster_id and m.get("heat", 0) > 0
            ]

            if len(rows) <= 3:
                continue

            summary = self._build_cluster_summary(rows, cluster["summary"])
            centroid = _compute_centroid(rows)

            self._storage.update_cluster(
                cluster_id,
                {
                    "summary": summary,
                    "centroid_embedding": centroid,
                },
            )

        # Create level 2 (root) clusters by grouping level 1 by directory_context
        self._create_root_clusters()

    @observe(tier="stage")
    def _build_cluster_summary(self, rows: list[dict], fallback: str) -> str:
        """Extract entities and keywords from member memories, return summary string."""
        all_content = " ".join(m["content"] for m in rows)

        entities = _ENTITY_PATTERN_RE.findall(all_content)
        top_entities = [e for e, _ in Counter(entities).most_common(10)]

        top_keywords = _top_keywords(all_content, n=5)

        summary_parts = []
        if top_entities:
            summary_parts.append("Entities: " + ", ".join(top_entities[:5]))
        if top_keywords:
            summary_parts.append("Keywords: " + ", ".join(top_keywords))
        return "; ".join(summary_parts) if summary_parts else fallback

    @observe(tier="stage")
    def _create_root_clusters(self) -> None:
        """Group level 1 clusters by dominant directory_context into level 2 clusters."""
        clusters = self._storage.get_clusters_by_level(1)
        if not clusters:
            return

        all_memories = self._storage.get_all_memories_for_decay()
        dir_groups: dict[str, list[dict]] = {}
        for cluster in clusters:
            cluster_id = cluster["id"]
            cluster_mems = [m for m in all_memories if m.get("cluster_id") == cluster_id]
            if cluster_mems:
                dir_counts: dict[str, int] = {}
                for m in cluster_mems:
                    d = m.get("directory_context", "unknown")
                    dir_counts[d] = dir_counts.get(d, 0) + 1
                # T-0015: default-arg capture prevents closure capturing stale reference
                dominant_dir = max(dir_counts, key=lambda k, dc=dir_counts: dc[k])
            else:
                dominant_dir = "unknown"
            dir_groups.setdefault(dominant_dir, []).append(cluster)

        for dir_ctx, group_clusters in dir_groups.items():
            if len(group_clusters) < 2:
                continue

            root_name = f"root_{dir_ctx.replace('/', '_').strip('_')}"
            total_members = sum(c["member_count"] for c in group_clusters)

            root_id = self._storage.insert_cluster(
                {
                    "name": root_name,
                    "level": 2,
                    "summary": f"Root cluster for {dir_ctx}",
                    "member_count": total_members,
                }
            )

            for child in group_clusters:
                self._storage.update_cluster(
                    child["id"],
                    {
                        "parent_cluster_id": root_id,
                    },
                )
