"""Dream replay mixin for SleepComputeEngine."""

from __future__ import annotations

import random

from yadgar.observability.observe import observe
from yadgar.tracing import trace_span


class _DreamMixin:
    """Dream replay operations: discover unexpected cross-domain connections."""

    @trace_span("sleep.dream_replay")
    def dream_replay(self) -> dict:
        """Select random pairs of unconnected memories and discover hidden connections.

        For each pair:
        - similarity > 0.4: create a weak co_occurrence relationship (weight=0.5)
        - similarity > 0.7: also generate a synthetic "dream insight" memory
        """
        stats = {"pairs_examined": 0, "connections_found": 0, "insights_generated": 0}

        # C3 two-phase fetch: sample IDs first (cheap, no content/embedding pull),
        # then fetch only the ~40 sampled rows with projected fields.
        candidate_ids = self._storage.get_candidate_memory_ids()
        n = len(candidate_ids)
        if n < 2:
            return stats

        max_pairs = n * (n - 1) // 2
        num_pairs = min(self._settings.DREAM_REPLAY_PAIRS, max_pairs)

        # Generate pairs as index pairs into candidate_ids (same RNG semantics as
        # the old path: uniform random.sample over range(n), identical population size).
        pairs = _generate_random_pairs_from_count(n, num_pairs)

        # Resolve index pairs → memory ids for the entity/connection index.
        # Collect unique ids to fetch only the rows we actually need.
        sampled_ids = {candidate_ids[i] for pair in pairs for i in pair}
        rows = self._storage.get_memories_by_ids_projected(list(sampled_ids))
        rows_by_id = {row["id"]: row for row in rows}

        mem_entity_id, connected_pairs = self._build_connected_pair_index_by_ids(
            sampled_ids, pairs, candidate_ids
        )

        for idx_a, idx_b in pairs:
            id_a = candidate_ids[idx_a]
            id_b = candidate_ids[idx_b]
            mem_a = rows_by_id.get(id_a)
            mem_b = rows_by_id.get(id_b)
            if mem_a is None or mem_b is None:
                continue

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

    @observe(tier="stage")
    def _build_connected_pair_index_by_ids(
        self,
        sampled_mem_ids: set[int],
        pairs: set[tuple[int, int]],
        candidate_ids: list[int],
    ) -> tuple[dict[int, int], set[tuple[int, int]]]:
        """Build entity-id lookup and connected-pair set for the sampled memory ids.

        C3 variant: takes raw memory ids directly (not index→memories[i] indirection).
        Same logic as _build_connected_pair_index but works from the id list produced
        by get_candidate_memory_ids() rather than full memory row dicts.

        Pre-builds a connected-pair index via ONE bulk fetch instead of one
        get_relationship_between HTTP call per pair (_memories_connected).
        """
        # Resolve memory-entity names → entity ids (only for already-existing entities).
        mem_entity_id: dict[int, int] = {}
        for mid in sampled_mem_ids:
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

        return mem_entity_id, connected_pairs

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

    @observe(tier="stage")
    def _ensure_memory_entity(self, memory_id: int) -> int:
        """Get or create an entity node for a memory."""
        name = f"memory:{memory_id}"
        existing = self._storage.get_entity_by_name(name)
        if existing:
            return existing["id"]
        return self._storage.insert_entity({"name": name, "type": "file"})

    @trace_span("sleep.dream_insight")
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
                "directory_context": "global",
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


@observe(tier="stage")
def _generate_random_pairs_from_count(
    n: int,
    num_pairs: int,
) -> set[tuple[int, int]]:
    """Generate a set of random unique index pairs for a population of size n.

    C3 replacement for _generate_random_pairs: takes population size (int) instead
    of a list of memory dicts, so it works with the id-only candidate list.
    RNG semantics are identical to _generate_random_pairs: uniform random.sample
    over range(n), same attempt budget.
    """
    pairs: set[tuple[int, int]] = set()
    attempts = 0
    max_attempts = num_pairs * 10
    while len(pairs) < num_pairs and attempts < max_attempts:
        i, j = random.sample(range(n), 2)
        pairs.add((min(i, j), max(i, j)))
        attempts += 1
    return pairs


def _generate_random_pairs(
    memories: list[dict],
    num_pairs: int,
) -> set[tuple[int, int]]:
    """Generate a set of random unique index pairs from memories.

    Kept for backward compatibility. New callers should use
    _generate_random_pairs_from_count(len(memories), num_pairs) instead.
    """
    return _generate_random_pairs_from_count(len(memories), num_pairs)
