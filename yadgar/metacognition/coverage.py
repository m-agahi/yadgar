"""Coverage assessment mixin — 'Do I have enough knowledge about this topic?'"""

from datetime import UTC, datetime


def _extract_entities(query: str) -> list[str]:
    """Re-use retrieval entity extraction without circular import."""
    from yadgar.retrieval import _extract_query_entities

    return _extract_query_entities(query)


class _CoverageMixin:
    """Metacognitive coverage assessment (MetaRAG signal 1)."""

    def assess_coverage(self, query: str, directory: str = "") -> dict:
        """Assess how well Yadgar can answer a query.

        Returns a dict with coverage score, confidence, suggestion,
        identified gaps, and detailed signal breakdowns.
        """
        # a) Memory density via FTS + vector search
        memory_count = 0
        matching_memories = []

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

        memory_count = len(matching_memories)

        # Density scoring: 0=0.0, 1-2=0.3, 3-5=0.6, 6+=0.9
        if memory_count == 0:
            density = 0.0
        elif memory_count <= 2:
            density = 0.3
        elif memory_count <= 5:
            density = 0.6
        else:
            density = 0.9

        # b) Entity coverage: what fraction of query entities exist in the graph
        query_entities = _extract_entities(query)
        total_query_entities = len(query_entities)
        known_entities = 0
        unknown_entities = []

        for entity_name in query_entities:
            entity = self._storage.get_entity_by_name(entity_name)
            if entity:
                known_entities += 1
            else:
                unknown_entities.append(entity_name)

        if total_query_entities > 0:
            entity_coverage = known_entities / total_query_entities
        else:
            entity_coverage = 0.0

        # c) Recency: age of most recent relevant memory
        recency_score = 0.0
        if matching_memories:
            now = datetime.now(UTC)
            most_recent = None
            for m in matching_memories:
                created = m.get("created_at")
                if created:
                    if isinstance(created, str):
                        try:
                            created = datetime.fromisoformat(created)
                        except (ValueError, TypeError) as _e:
                            continue
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=UTC)
                    if most_recent is None or created > most_recent:
                        most_recent = created
            if most_recent is not None:
                age_days = (now - most_recent).total_seconds() / 86400
                if age_days < 1:
                    recency_score = 1.0
                elif age_days < 7:
                    recency_score = 0.7
                elif age_days < 30:
                    recency_score = 0.4
                else:
                    recency_score = 0.2

        # d) Confidence: average confidence score of matching memories
        confidence_score = 0.0
        if matching_memories:
            confidences = [m.get("confidence", 1.0) for m in matching_memories]
            confidence_score = sum(confidences) / len(confidences)

        # Overall coverage (weighted blend)
        overall = (
            0.3 * density + 0.3 * entity_coverage + 0.2 * recency_score + 0.2 * confidence_score
        )

        # Suggestion
        if overall >= 0.7:
            suggestion = "sufficient"
            detail = "Proceed with confidence — strong knowledge coverage."
        elif overall >= 0.4:
            suggestion = "partial"
            detail = "Answer available but may be incomplete — consider investigating gaps."
        else:
            suggestion = "insufficient"
            detail = "Limited knowledge — investigate further before answering."

        # Identify gaps
        gaps = list(unknown_entities)
        if memory_count == 0:
            gaps.append(f"No memories found matching query: {query[:80]}")

        return {
            "coverage": round(overall, 4),
            "confidence": round(confidence_score, 4),
            "suggestion": suggestion,
            "gaps": gaps,
            "memory_count": memory_count,
            "entity_coverage": round(entity_coverage, 4),
            "recency_score": round(recency_score, 4),
            "detail": detail,
        }
