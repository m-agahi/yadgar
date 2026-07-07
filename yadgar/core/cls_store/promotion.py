"""Promotion logic: episodic → semantic memory transition."""

import logging

from yadgar._shared.observability.observe import observe
from yadgar._shared.storage.directory import dominant_directory
from yadgar.core.cls_store.patterns import _is_degenerate_auto_abstracted

logger = logging.getLogger(__name__)


class _PromotionMixin:
    """Mixin: _promote_pattern — promotes one qualifying cluster to a semantic memory."""

    @observe(tier="stage", metric="consolidation.cls.promote")
    def _promote_pattern(self, pattern: dict) -> bool:
        """Promote a qualifying cluster to a semantic memory.

        Returns True if promoted, False if skipped.
        Assumes check_consistency has already been called by the caller.

        Steps:
        a. abstract_to_schema() — generate semantic summary
        b. Guard against degenerate schemas
        c. Check for near-duplicate semantic memories
        d. Create new semantic memory
        e. Link episodic memories to semantic via derived_from
        """
        cluster_mems = pattern["memories"]

        # a. Abstract to schema
        schema = self.abstract_to_schema(cluster_mems)
        if not schema:
            return False

        # b. Guard: skip degenerate schemas with no meaningful subject.
        if _is_degenerate_auto_abstracted(schema):
            logger.debug("Skipping degenerate CLS pattern (no meaningful subject): %r", schema[:80])
            return False

        # c. Check if we already have a similar semantic memory
        schema_embedding = self._embeddings.encode(schema)
        if schema_embedding is not None:
            existing = self._storage.search_vectors(schema_embedding, top_k=3, min_heat=0.0)
            for mid, _distance in existing:
                mem = self._storage.get_memory(mid)
                if mem and mem.get("store_type") == "semantic":
                    sim = self._embeddings.similarity(schema_embedding, mem["embedding"])
                    if sim > self._settings.CURATION_SIMILARITY_THRESHOLD:
                        return False

        # d. Create semantic memory — derive originating directory from cluster members.
        # Use dominant_directory() over cluster_mems to get the real project dir
        # (not directories[0] which is set-ordered and loses counts; not "system").
        # Cross-cluster or unknown → "global" (safe, cross-cutting).
        cluster_dirs = [m.get("directory_context") for m in cluster_mems]
        primary_dir = dominant_directory(cluster_dirs)

        semantic_id = self._storage.insert_memory(
            {
                "content": schema,
                "embedding": schema_embedding,
                "tags": ["semantic", "auto-abstracted"],
                "directory_context": primary_dir,
                "heat": 0.8,
                "is_stale": False,
                "embedding_model": self._embeddings.get_model_name(),
            }
        )

        # Set store_type to semantic
        self._storage.update_memory_fields(semantic_id, store_type="semantic")

        # e. Link episodic memories to semantic memory via derived_from —
        # ONE bulk fetch instead of O(cluster_size) per-pair HTTP calls.
        tgt_name = f"memory:{semantic_id}"
        tgt_entity = self._storage.get_entity_by_name(tgt_name)
        if tgt_entity is None:
            tgt_eid = self._storage.insert_entity({"name": tgt_name, "type": "file"})
        else:
            tgt_eid = tgt_entity["id"]

        # Resolve (or create) source entity ids for each episodic memory.
        src_eids: list[int] = []
        for mem in cluster_mems:
            src_name = f"memory:{mem['id']}"
            src_entity = self._storage.get_entity_by_name(src_name)
            if src_entity is None:
                src_eid = self._storage.insert_entity({"name": src_name, "type": "file"})
            else:
                src_eid = src_entity["id"]
            src_eids.append(src_eid)

        # One bulk relationship fetch for all (src_eid, tgt_eid) pairs.
        all_eids = src_eids + [tgt_eid]
        existing_rels = self._storage.get_relationships_among_entities(all_eids)
        rel_index: dict[tuple[int, int], dict] = {
            (
                min(r["source_entity_id"], r["target_entity_id"]),
                max(r["source_entity_id"], r["target_entity_id"]),
            ): r
            for r in existing_rels
        }

        for src_eid in src_eids:
            key = (min(src_eid, tgt_eid), max(src_eid, tgt_eid))
            existing = rel_index.get(key)
            if existing:
                self._storage.reinforce_relationship(existing["id"])
            else:
                self._storage.insert_relationship(
                    {
                        "source_entity_id": src_eid,
                        "target_entity_id": tgt_eid,
                        "relationship_type": "derived_from",
                    }
                )

        return True
