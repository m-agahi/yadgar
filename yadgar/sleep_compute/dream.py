"""Dream replay mixin for SleepComputeEngine."""

from __future__ import annotations

import random


class _DreamMixin:
    """Dream replay operations: discover unexpected cross-domain connections."""

    def dream_replay(self) -> dict:
        """Select random pairs of unconnected memories and discover hidden connections.

        For each pair:
        - similarity > 0.4: create a weak co_occurrence relationship (weight=0.5)
        - similarity > 0.7: also generate a synthetic "dream insight" memory
        """
        stats = {"pairs_examined": 0, "connections_found": 0, "insights_generated": 0}

        memories = self._storage.get_all_memories_with_embeddings()
        if len(memories) < 2:
            return stats

        max_pairs = len(memories) * (len(memories) - 1) // 2
        num_pairs = min(self._settings.DREAM_REPLAY_PAIRS, max_pairs)

        # Generate random unique index pairs
        pairs: set[tuple[int, int]] = set()
        attempts = 0
        max_attempts = num_pairs * 10
        while len(pairs) < num_pairs and attempts < max_attempts:
            i, j = random.sample(range(len(memories)), 2)
            pairs.add((min(i, j), max(i, j)))
            attempts += 1

        # Pre-build a connected-pair index: ONE bulk fetch instead of one
        # get_relationship_between HTTP call per pair (_memories_connected).
        candidate_mem_ids = {memories[i]["id"] for idx_pair in pairs for i in idx_pair}
        # Resolve memory-entity names → entity ids (only for already-existing entities).
        mem_entity_id: dict[int, int] = {}
        for mid in candidate_mem_ids:
            ent = self._storage.get_entity_by_name(f"memory:{mid}")
            if ent is not None:
                mem_entity_id[mid] = ent["id"]

        # Bulk-fetch all relationships among the entity ids we found.
        connected_pairs: set[tuple[int, int]] = set()
        if len(mem_entity_id) >= 2:
            entity_ids = list(mem_entity_id.values())
            for rel in self._storage.get_relationships_among_entities(entity_ids):
                sid = rel.get("source_entity_id")
                tid = rel.get("target_entity_id")
                if sid is not None and tid is not None:
                    connected_pairs.add((min(sid, tid), max(sid, tid)))

        for idx_a, idx_b in pairs:
            mem_a = memories[idx_a]
            mem_b = memories[idx_b]

            if mem_a["embedding"] is None or mem_b["embedding"] is None:
                continue

            # Skip already-connected memories — O(1) dict+set lookup.
            eid_a = mem_entity_id.get(mem_a["id"])
            eid_b = mem_entity_id.get(mem_b["id"])
            if eid_a is not None and eid_b is not None:
                if (min(eid_a, eid_b), max(eid_a, eid_b)) in connected_pairs:
                    continue

            stats["pairs_examined"] += 1
            sim = self._embeddings.similarity(mem_a["embedding"], mem_b["embedding"])

            if sim > 0.7:
                # Strong unexpected connection: link + dream insight
                self._create_dream_connection(mem_a["id"], mem_b["id"])
                self._create_dream_insight(mem_a, mem_b)
                stats["connections_found"] += 1
                stats["insights_generated"] += 1
            elif sim > 0.4:
                # Moderate connection: link only
                self._create_dream_connection(mem_a["id"], mem_b["id"])
                stats["connections_found"] += 1

        return stats

    def _create_dream_connection(self, mem_a_id: int, mem_b_id: int) -> None:
        """Create a weak co_occurrence link between two memories."""
        src_eid = self._ensure_memory_entity(mem_a_id)
        tgt_eid = self._ensure_memory_entity(mem_b_id)

        self._storage.insert_relationship(
            {
                "source_entity_id": src_eid,
                "target_entity_id": tgt_eid,
                "relationship_type": "co_occurrence",
                "weight": 0.5,
            }
        )

    def _ensure_memory_entity(self, memory_id: int) -> int:
        """Get or create an entity node for a memory."""
        name = f"memory:{memory_id}"
        existing = self._storage.get_entity_by_name(name)
        if existing:
            return existing["id"]
        return self._storage.insert_entity({"name": name, "type": "file"})

    def _create_dream_insight(self, mem_a: dict, mem_b: dict) -> None:
        """Generate a synthetic dream insight memory."""
        summary_a = mem_a["content"][:100].strip()
        summary_b = mem_b["content"][:100].strip()
        content = f"Dream connection: {summary_a} may relate to {summary_b}"

        embedding = self._embeddings.encode(content)
        memory_id = self._storage.insert_memory(
            {
                "content": content,
                "embedding": embedding,
                "tags": ["dream", "auto-generated"],
                "directory_context": "system",
                "heat": 0.5,
                "is_stale": False,
                "embedding_model": self._embeddings.get_model_name(),
            }
        )
        self._storage.update_memory_scores(
            memory_id,
            surprise_score=0.8,
            importance=0.4,
        )
