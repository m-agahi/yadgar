"""Novelty-signal computation mixin for the predictive-coding write gate.

Extracted from ``predictive_coding.py`` (C6 module-standardization train,
ADR-0066). ``WriteGate`` inherits ``_SignalsMixin`` so all signal methods
remain accessible on the instance (test suite calls them as ``gate._compute_*``).

Mixin contract: assumes ``self._embeddings``, ``self._storage``,
``self._retriever``, and ``self._get_cached_entities()`` are available on
the composing class.
"""

from datetime import UTC, datetime
from typing import Any

from yadgar._shared.observability.observe import observe


class _SignalsMixin:
    """Mixin providing the four novelty-signal methods for WriteGate."""

    # Declared here so type-checkers find them; real objects set in WriteGate.__init__.
    _embeddings: Any
    _storage: Any
    _retriever: Any

    def _get_cached_entities(self) -> list[dict[str, Any]]:  # pragma: no cover
        """Implemented by WriteGate (entity-cache host)."""
        raise NotImplementedError  # always shadowed by WriteGate._get_cached_entities

    # ── Signal 1: Embedding novelty ──────────────────────────────────────

    @observe(tier="stage", metric="write.compute_embedding_novelty")
    def _compute_embedding_novelty(self, content: str) -> float:
        """Signal 1: How novel is this content in embedding space?

        novelty = 1.0 - max_similarity (0.0=identical, 1.0=completely novel)
        """
        query_embedding = self._embeddings.encode(content)
        if query_embedding is None:
            return 0.5

        vec_hits = self._storage.search_vectors(query_embedding, top_k=5, min_heat=0.0)
        if not vec_hits:
            return 0.8  # No vectors at all = fairly novel

        max_similarity = 0.0
        for mid, _distance in vec_hits:
            mem = self._storage.get_memory(mid)
            if mem and mem.get("embedding"):
                sim = self._embeddings.similarity(query_embedding, mem["embedding"])
                max_similarity = max(max_similarity, sim)

        return max(0.0, min(1.0, 1.0 - max_similarity))

    # ── Signal 2: Entity novelty ─────────────────────────────────────────

    @observe(tier="stage", metric="write.compute_entity_novelty")
    def _compute_entity_novelty(self, content: str, directory: str) -> float:
        """Signal 2: How many entities in this content are new to the graph?

        entity_novelty = new_entities / total_entities (or 0.5 if no entities)
        """
        kg = self._retriever._graph
        extracted = kg.extract_entities_typed(content, directory)

        if not extracted:
            return 0.5  # No entities = moderate novelty

        total_entities = len(extracted)
        new_count = 0
        for name, _type, _rel_ctx in extracted:
            existing = self._storage.get_entity_by_name(name)
            if existing is None:
                new_count += 1

        return new_count / total_entities

    # ── Signal 3 helpers ─────────────────────────────────────────────────

    @observe(tier="hot", metric="write.collect_temporal_entities")
    def _collect_temporal_entities(self, content: str, directory: str) -> set[str]:
        """Collect entity names to check for temporal novelty.

        Method 1: Extract entities from content using code patterns.
        Method 2: Check which cached entities appear in the content text.
        """
        entity_names: set[str] = set()
        kg = self._retriever._graph
        extracted = kg.extract_entities_typed(content, directory)
        for name, _type, _rel_ctx in extracted:
            entity_names.add(name)
        for entity in self._get_cached_entities():
            if entity["name"] in content and len(entity["name"]) > 1:
                entity_names.add(entity["name"])
        return entity_names

    def _parse_created_at(self, mem: dict) -> datetime | None:
        """Parse a memory's created_at field into a timezone-aware datetime.

        Returns None if the field is missing or unparseable.
        """
        try:
            mem_dt = datetime.fromisoformat(mem["created_at"])
            if mem_dt.tzinfo is None:
                mem_dt = mem_dt.replace(tzinfo=UTC)
            return mem_dt
        except (ValueError, TypeError, KeyError):  # fmt: skip
            return None

    @observe(tier="hot", metric="write.most_recent_mention_dt")
    def _most_recent_mention_dt(
        self, entity_names: set[str], dir_memories: list[dict]
    ) -> datetime | None:
        """Return the most recent datetime any of entity_names appears in dir_memories.

        Returns None if no matching memory is found.
        """
        most_recent: datetime | None = None
        for mem in dir_memories:
            mem_content = mem.get("content", "")
            if not any(name in mem_content for name in entity_names):
                continue
            mem_dt = self._parse_created_at(mem)
            if mem_dt is None:
                continue
            if most_recent is None or mem_dt > most_recent:
                most_recent = mem_dt
        return most_recent

    # ── Signal 3: Temporal novelty ───────────────────────────────────────

    @observe(tier="stage", metric="write.compute_temporal_novelty")
    def _compute_temporal_novelty(self, content: str, directory: str) -> float:
        """Signal 3: How recently was a related topic discussed?

        Within last hour: 0.1 (recent = expected follow-up)
        1-24h ago: 0.3 (moderate gap)
        >24h or none found: 0.7 (old topic resurfacing = surprising)
        """
        entity_names_to_check = self._collect_temporal_entities(content, directory)
        if not entity_names_to_check:
            return 0.7  # No entities to check = surprising

        dir_memories = self._storage.get_memories_for_directory(directory, min_heat=0.0)
        most_recent_dt = self._most_recent_mention_dt(entity_names_to_check, dir_memories)

        if most_recent_dt is None:
            return 0.7  # No recent memory found

        now = datetime.now(UTC)
        hours_elapsed = (now - most_recent_dt).total_seconds() / 3600.0

        if hours_elapsed < 1.0:
            return 0.1  # Very recent = expected follow-up
        if hours_elapsed < 24.0:
            return 0.3  # Moderate gap
        return 0.7  # Old topic resurfacing = surprising

    # ── Signal 4: Structural novelty ─────────────────────────────────────

    @observe(tier="stage", metric="write.compute_structural_novelty")
    def _compute_structural_novelty(self, content: str, directory: str) -> float:
        """Signal 4: Does this content introduce new relationship types or causal patterns?

        New relationship type in graph: 0.8
        All relationship types already exist: 0.2
        """
        kg = self._retriever._graph
        extracted = kg.extract_entities_typed(content, directory)

        if not extracted:
            return 0.2  # No structure to analyze

        # Collect relationship contexts from extracted entities
        new_rel_contexts = set()
        for _name, _type, rel_context in extracted:
            if rel_context:
                new_rel_contexts.add(rel_context)

        if not new_rel_contexts:
            return 0.2  # No relationship signals

        # Check which relationship types already exist — use TTL-cached entity list.
        existing_rel_types = set()
        content_entity_names = {name for name, _, _ in extracted}
        content_entities = [
            e for e in self._get_cached_entities() if e["name"] in content_entity_names
        ]
        if content_entities:
            content_entity_ids = [e["id"] for e in content_entities]
            rels = self._storage.get_relationships_among_entities(content_entity_ids)
            for rel in rels:
                rtype = rel.get("relationship_type")
                if rtype:
                    existing_rel_types.add(rtype)

        # Check if any extracted relationship contexts are truly new
        has_new = False
        for rel_ctx in new_rel_contexts:
            if rel_ctx not in existing_rel_types:
                has_new = True
                break

        return 0.8 if has_new else 0.2
