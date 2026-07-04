"""Coverage assessment mixin — 'Do I have enough knowledge about this topic?'"""

from datetime import UTC, datetime

from yadgar.observability.observe import observe


def _extract_entities(query: str) -> list[str]:
    """Re-use retrieval entity extraction without circular import."""
    from yadgar.retrieval import _extract_query_entities

    return _extract_query_entities(query)


@observe(tier="stage")
def _density_score(memory_count: int) -> float:
    """Map memory count to a density score bucket."""
    if memory_count == 0:
        return 0.0
    if memory_count <= 2:
        return 0.3
    if memory_count <= 5:
        return 0.6
    return 0.9


@observe(tier="stage")
def _parse_created_at(value) -> datetime | None:
    """Parse a created_at value to a timezone-aware datetime, or None."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except (ValueError, TypeError):  # fmt: skip
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value


@observe(tier="stage")
def _recency_score(memories: list[dict]) -> float:
    """Return recency score based on most recent memory timestamp."""
    if not memories:
        return 0.0
    now = datetime.now(UTC)
    most_recent = None
    for m in memories:
        created = _parse_created_at(m.get("created_at"))
        if created is None:
            continue
        if most_recent is None or created > most_recent:
            most_recent = created
    if most_recent is None:
        return 0.0
    age_days = (now - most_recent).total_seconds() / 86400
    if age_days < 1:
        return 1.0
    if age_days < 7:
        return 0.7
    if age_days < 30:
        return 0.4
    return 0.2


@observe(tier="stage")
def _confidence_score(memories: list[dict]) -> float:
    """Return average confidence score of matching memories."""
    if not memories:
        return 0.0
    confidences = [m.get("confidence", 1.0) for m in memories]
    return sum(confidences) / len(confidences)


@observe(tier="stage")
def _suggestion(overall: float) -> tuple[str, str]:
    """Map overall score to suggestion label and detail string."""
    if overall >= 0.7:
        return "sufficient", "Proceed with confidence — strong knowledge coverage."
    if overall >= 0.4:
        return "partial", "Answer available but may be incomplete — consider investigating gaps."
    return "insufficient", "Limited knowledge — investigate further before answering."


class _CoverageMixin:
    """Metacognitive coverage assessment (MetaRAG signal 1)."""

    @observe(tier="stage")
    def _gather_memories(self, query: str) -> list[dict]:
        """Collect matching memories via FTS + vector search with deduplication."""
        matching_memories: list[dict] = []
        try:
            fts_results = self._storage.search_memories_fts(query, min_heat=0.0, limit=50)
            if fts_results:
                matching_memories.extend(fts_results)
        except Exception:
            pass

        query_embedding = self._embeddings.encode(query)
        if query_embedding is not None:
            vec_hits = self._storage.search_vectors(query_embedding, top_k=50, min_heat=0.0)
            seen_ids = {m["id"] for m in matching_memories}
            for mid, _distance in vec_hits:
                if mid not in seen_ids:
                    mem = self._storage.get_memory(mid)
                    if mem:
                        matching_memories.append(mem)
                        seen_ids.add(mid)
        return matching_memories

    @observe(tier="stage")
    def _entity_coverage(self, query: str) -> tuple[float, list[str]]:
        """Return (entity_coverage_ratio, unknown_entity_names)."""
        query_entities = _extract_entities(query)
        total = len(query_entities)
        if total == 0:
            return 0.0, []
        unknown: list[str] = []
        known = 0
        for name in query_entities:
            if self._storage.get_entity_by_name(name):
                known += 1
            else:
                unknown.append(name)
        return known / total, unknown

    @observe(tier="boundary")
    def assess_coverage(self, query: str, directory: str = "") -> dict:
        """Assess how well Yadgar can answer a query.

        Returns a dict with coverage score, confidence, suggestion,
        identified gaps, and detailed signal breakdowns.
        """
        matching_memories = self._gather_memories(query)
        memory_count = len(matching_memories)

        density = _density_score(memory_count)
        entity_coverage, unknown_entities = self._entity_coverage(query)
        recency = _recency_score(matching_memories)
        confidence = _confidence_score(matching_memories)

        overall = 0.3 * density + 0.3 * entity_coverage + 0.2 * recency + 0.2 * confidence

        suggestion, detail = _suggestion(overall)

        gaps = list(unknown_entities)
        if memory_count == 0:
            gaps.append(f"No memories found matching query: {query[:80]}")

        return {
            "coverage": round(overall, 4),
            "confidence": round(confidence, 4),
            "suggestion": suggestion,
            "gaps": gaps,
            "memory_count": memory_count,
            "entity_coverage": round(entity_coverage, 4),
            "recency_score": round(recency, 4),
            "detail": detail,
        }
