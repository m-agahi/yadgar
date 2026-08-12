"""Promotion logic: episodic → semantic memory transition."""

import logging

from yadgar._shared.observability.observe import observe
from yadgar._shared.storage._project_id_writer import (
    observe_project_id_skip,
    resolve_project_id_from_rows,
)
from yadgar._shared.storage.directory import dominant_directory
from yadgar.backend.cls_store.patterns import (
    _is_degenerate_auto_abstracted,
    _is_thin_auto_abstracted,
)

logger = logging.getLogger(__name__)


def _schema_rejected_pre_promotion(schema: str) -> bool:
    """Return True when *schema* must NOT be promoted to a semantic memory.

    Two write-time corpus guards, OR'd:
      - degenerate: no meaningful subject (exact "frequently modified together"
        class / no meaningful token) — PR #60.
      - thin (C4.3 / S1, ADR-0142): a meta-token-dense bag of yadgar-internal
        plumbing tokens (entity:/graph/derived_from/co_occurrence/…) with too
        few distinct real domain tokens. These win meta-queries about the memory
        system by construction and are never demoted at recall — cheapest fix is
        to not promote them.

    Kept as a module helper so _promote_pattern stays within the I13 cyclomatic
    cap (adding the thin branch inline pushed it to 16).
    """
    if _is_degenerate_auto_abstracted(schema):
        logger.debug("Skipping degenerate CLS pattern (no meaningful subject): %r", schema[:80])
        return True
    if _is_thin_auto_abstracted(schema):
        logger.debug("Skipping thin auto-abstracted CLS pattern (meta-dense): %r", schema[:80])
        return True
    return False


class _PromotionMixin:
    """Mixin: _promote_pattern — promotes one qualifying cluster to a semantic memory."""

    @observe(tier="stage", metric="consolidation.cls.promote")
    def _near_duplicate_semantic_exists(self, schema_embedding) -> bool:
        """True when a semantic memory already covers this schema.

        Extracted from ``_promote_pattern`` in C4 (0047 PR#40 §5): adding the
        project-resolution guard pushed that method's cyclomatic complexity to
        16, one over the I30 hard cap. This block is the natural seam — it is
        step (c) of the promotion sequence and has no other coupling to the
        surrounding steps. Behaviour is unchanged.
        """
        if schema_embedding is None:
            return False
        existing = self._storage.search_vectors(schema_embedding, top_k=3, min_heat=0.0)
        for mid, _distance in existing:
            mem = self._storage.get_memory(mid)
            if mem and mem.get("store_type") == "semantic":
                sim = self._embeddings.similarity(schema_embedding, mem["embedding"])
                if sim > self._settings.CURATION_SIMILARITY_THRESHOLD:
                    return True
        return False

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

        # b. Guard: skip degenerate (no meaningful subject) OR thin
        # (meta-token-dense) schemas before promotion — see helper.
        if _schema_rejected_pre_promotion(schema):
            return False

        # c. Check if we already have a similar semantic memory
        schema_embedding = self._embeddings.encode(schema)
        if self._near_duplicate_semantic_exists(schema_embedding):
            return False

        # d. Create semantic memory.
        #
        # C4 (0047 PR#40 §5): the cluster's OWN rows name the project. Exactly
        # one distinct identifying ``project_id`` → the promotion belongs to
        # that project; zero or two or more → it belongs to none, and the
        # promotion is SKIPPED and counted. It is never collapsed onto a
        # sentinel: a semantic memory stamped ``"global"`` because its inputs
        # disagreed is exactly the phantom-namespace row §1.4 forbids, and
        # this writer plus ``strengthen.py`` are the two that actually mint it.
        # Nothing is derived — this runs in the backend container (ADR-0227).
        primary_project = resolve_project_id_from_rows(cluster_mems)
        if primary_project is None:
            observe_project_id_skip("cls_promotion")
            logger.info(
                "Skipping promotion: cluster of %d memories names no single "
                "project_id (0 or >=2 distinct). Not collapsing to a sentinel.",
                len(cluster_mems),
            )
            return False

        # directory_context keeps its own resolution — it is the legacy read key
        # until C7 re-keys the readers onto project_id.
        cluster_dirs = [m.get("directory_context") for m in cluster_mems]
        primary_dir = dominant_directory(cluster_dirs)

        semantic_id = self._storage.insert_memory(
            {
                "content": schema,
                "embedding": schema_embedding,
                "tags": ["semantic", "auto-abstracted"],
                "directory_context": primary_dir,
                "project_id": primary_project,
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
