"""Cluster and memory similarity link storage mixin."""

import logging

_log = logging.getLogger(__name__)


class _ClusterMixin:
    """Memory cluster + similarity link CRUD — mixed into StorageEngine."""

    # ------------------------------------------------------------------ Memory Clusters

    def insert_cluster(self, cluster: dict) -> int:
        now = self._now_iso()
        cid = self._next_id("memory_cluster")
        centroid = cluster.get("centroid_embedding")
        centroid_floats = self._bytes_to_floats(centroid) if centroid else None
        self._q(
            "CREATE type::record('memory_cluster', $id) SET "
            "name = $name, level = $level, parent_cluster_id = $parent, "
            "summary = $summary, centroid_embedding = $centroid, "
            "member_count = $member_count, created_at = $created_at, "
            "last_updated = $last_updated, heat = $heat",
            {
                "id": cid,
                "name": cluster["name"],
                "level": cluster.get("level", 0),
                "parent": cluster.get("parent_cluster_id"),
                "summary": cluster.get("summary", ""),
                "centroid": centroid_floats,
                "member_count": cluster.get("member_count", 0),
                "created_at": cluster.get("created_at", now),
                "last_updated": cluster.get("last_updated", now),
                "heat": cluster.get("heat", 1.0),
            },
        )
        return cid

    def get_cluster(self, cluster_id: int) -> dict | None:
        cid = int(cluster_id)
        rows = self._q(f"SELECT * FROM memory_cluster:{cid}")
        return self._row_to_dict(rows[0]) if rows else None

    def get_clusters_by_level(self, level: int) -> list[dict]:
        rows = self._q(
            "SELECT * FROM memory_cluster WHERE level = $level ORDER BY heat DESC",
            {"level": level},
        )
        return self._rows_to_dicts(rows)

    def update_cluster(self, cluster_id: int, updates: dict):
        allowed = {
            "name",
            "level",
            "parent_cluster_id",
            "summary",
            "centroid_embedding",
            "member_count",
            "heat",
            "last_updated",
        }
        fields = {k: v for k, v in updates.items() if k in allowed}
        if not fields:
            return
        if "last_updated" not in fields:
            fields["last_updated"] = self._now_iso()
        # Convert centroid bytes if present
        if "centroid_embedding" in fields and isinstance(fields["centroid_embedding"], bytes):
            fields["centroid_embedding"] = self._bytes_to_floats(fields["centroid_embedding"])
        params = {"id": cluster_id}
        set_parts = []
        for k, v in fields.items():
            params[k] = v
            set_parts.append(f"{k} = ${k}")
        self._q(
            f"UPDATE type::record('memory_cluster', $id) SET {', '.join(set_parts)}",
            params,
        )

    # ------------------------------------------------------------------ Memory similarity links

    def get_memory_similarity_link(self, mid_a: int, mid_b: int) -> dict | None:
        src, tgt = (mid_a, mid_b) if mid_a < mid_b else (mid_b, mid_a)
        rows = self._q(
            "SELECT * FROM memory_similarity_link WHERE "
            "source_memory_id = $src AND target_memory_id = $tgt LIMIT 1",
            {"src": src, "tgt": tgt},
        )
        return self._row_to_dict(rows[0]) if rows else None

    def insert_memory_similarity_link(
        self,
        mid_a: int,
        mid_b: int,
        weight: float,
        origin_memory_id: int | None = None,
    ) -> int:
        src, tgt = (mid_a, mid_b) if mid_a < mid_b else (mid_b, mid_a)
        now = self._now_iso()
        lid = self._next_id("memory_similarity_link")
        # C3: citation_source_memory_id tracks which memory triggered this link.
        # Use caller-supplied origin or fall back to the lower-id endpoint (canonical).
        citation_src = origin_memory_id if origin_memory_id is not None else src
        self._q(
            "CREATE type::record('memory_similarity_link', $id) SET "
            "source_memory_id = $src, target_memory_id = $tgt, "
            "weight = $weight, created_at = $created_at, updated_at = $updated_at, "
            "citation_source_memory_id = $csm",
            {
                "id": lid,
                "src": src,
                "tgt": tgt,
                "weight": weight,
                "created_at": now,
                "updated_at": now,
                "csm": citation_src,
            },
        )
        return lid

    def reinforce_memory_similarity_link(self, link_id: int, weight_delta: float) -> None:
        self._q(
            "UPDATE type::record('memory_similarity_link', $id) SET "
            "weight = weight + $delta, updated_at = $now",
            {"id": link_id, "delta": weight_delta, "now": self._now_iso()},
        )

    def get_all_memory_similarity_links(self) -> list[dict]:
        """Return all memory_similarity_link rows. Used for pre-loading before batch writes."""
        rows = self._q("SELECT * FROM memory_similarity_link")
        return self._rows_to_dicts(rows)
