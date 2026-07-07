"""Cognitive load management mixin — optimal 4±1 chunk context packing."""

from datetime import UTC, datetime

from yadgar._shared.observability.observe import observe


def _extract_entities(query: str) -> list[str]:
    """Re-use retrieval entity extraction without circular import."""
    from yadgar._shared.retrieval import _extract_query_entities

    return _extract_query_entities(query)


@observe(tier="stage")
def _precompute_memory_features(
    memories: list[dict],
) -> tuple[list[set[str]], list[datetime | None]]:
    """Extract entity sets and parsed timestamps for each memory.

    Returns a parallel pair of lists: entity_sets and timestamps.
    Invalid timestamp strings are replaced with None.
    """
    entity_sets: list[set[str]] = []
    timestamps: list[datetime | None] = []

    for mem in memories:
        entities = set(_extract_entities(mem.get("content", "")))
        tags = mem.get("tags", [])
        if isinstance(tags, list):
            entities.update(tags)
        entity_sets.append(entities)

        created = mem.get("created_at")
        if isinstance(created, str):
            try:
                created = datetime.fromisoformat(created)
            except (ValueError, TypeError):  # fmt: skip
                created = None
        timestamps.append(created)

    return entity_sets, timestamps


@observe(tier="stage")
def _memories_should_cluster(
    i: int,
    j: int,
    entity_sets: list[set[str]],
    timestamps: list[datetime | None],
) -> bool:
    """Return True if memories i and j should belong to the same chunk.

    Clustering criteria (either is sufficient):
    - Entity overlap: Jaccard similarity > 0.3
    - Temporal proximity: timestamps < 2 hours apart
    """
    ei, ej = entity_sets[i], entity_sets[j]
    if ei and ej:
        union = ei | ej
        jaccard = len(ei & ej) / len(union) if union else 0.0
        if jaccard > 0.3:
            return True

    ti, tj = timestamps[i], timestamps[j]
    if ti is not None and tj is not None:
        t_i = ti if ti.tzinfo is not None else ti.replace(tzinfo=UTC)
        t_j = tj if tj.tzinfo is not None else tj.replace(tzinfo=UTC)
        if abs((t_i - t_j).total_seconds()) < 7200:  # 2 hours
            return True

    return False


class _CognitiveLoadMixin:
    """Cognitive load management via Cowan's 4±1 chunk limit (Cognitive Workspace)."""

    @observe(tier="boundary")
    def manage_context(self, memories: list[dict], max_chunks: int | None = None) -> list[dict]:
        """Apply Cowan's 4±1 cognitive load optimization.

        If memories fit within the chunk limit, return as-is.
        Otherwise, group into coherent chunks, rank, select top chunks,
        and apply primacy-recency positioning.
        """
        if max_chunks is None:
            max_chunks = self._chunk_limit

        if not memories:
            return []

        # Step 1: if under limit, return as-is with metadata
        if len(memories) <= max_chunks:
            result = []
            for i, mem in enumerate(memories):
                enriched = dict(mem)
                enriched["_chunk_id"] = i
                enriched["_position_reason"] = "within_limit"
                result.append(enriched)
            return result

        # Step 2: Group related memories into chunks
        chunks = self.chunk_memories(memories)

        # Step 3: Rank chunks by combined importance * heat * confidence
        def _chunk_score(chunk: list[dict]) -> float:
            total = 0.0
            for m in chunk:
                importance = m.get("importance", 0.5)
                heat = m.get("heat", 0.5)
                confidence = m.get("confidence", 1.0)
                total += importance * heat * confidence
            return total / len(chunk) if chunk else 0.0

        scored_chunks = [(i, chunk, _chunk_score(chunk)) for i, chunk in enumerate(chunks)]
        scored_chunks.sort(key=lambda x: x[2], reverse=True)

        # Step 4: Take top max_chunks
        selected = scored_chunks[:max_chunks]
        overflow = scored_chunks[max_chunks:]

        # Step 5: Apply primacy-recency positioning
        positioned = self._apply_primacy_recency(selected)

        # Step 6: Flatten chunks into memory list with metadata
        result = []
        for pos_idx, (chunk_id, chunk, _score) in enumerate(positioned):
            reason = self._position_reason(pos_idx, len(positioned))
            for mem in chunk:
                enriched = dict(mem)
                enriched["_chunk_id"] = chunk_id
                enriched["_position_reason"] = reason
                result.append(enriched)

        # Summarize overflow if any
        if overflow:
            overflow_memories = []
            for _, chunk, _ in overflow:
                overflow_memories.extend(chunk)
            summaries = self.summarize_overflow(overflow_memories)
            for summary in summaries:
                summary["_chunk_id"] = -1
                summary["_position_reason"] = "overflow_summary"
                result.append(summary)

        return result

    @observe(tier="stage")
    def chunk_memories(self, memories: list[dict]) -> list[list[dict]]:
        """Group related memories into coherent chunks.

        Uses entity overlap (Jaccard > 0.3) and temporal proximity (< 2h apart).
        Singleton memories that don't cluster stay as individual chunks.
        """
        if not memories:
            return []

        n = len(memories)
        assigned = [False] * n
        entity_sets, timestamps = _precompute_memory_features(memories)
        chunks: list[list[dict]] = []

        for i in range(n):
            if assigned[i]:
                continue
            chunk = [memories[i]]
            assigned[i] = True

            for j in range(i + 1, n):
                if assigned[j]:
                    continue
                if _memories_should_cluster(i, j, entity_sets, timestamps):
                    chunk.append(memories[j])
                    assigned[j] = True

            chunks.append(chunk)

        return chunks

    @observe(tier="stage")
    def summarize_overflow(self, excess_memories: list[dict], target_count: int = 1) -> list[dict]:
        """Compress multiple low-priority memories into summary chunks.

        Preserves verbatim: high-surprise (>0.7) and high-importance (>0.7).
        Summarizes remaining memories into brief summaries.
        """
        if not excess_memories:
            return []

        # Separate high-value memories that should be preserved
        preserved = []
        to_summarize = []

        for mem in excess_memories:
            surprise = mem.get("surprise_score", 0.0)
            importance = mem.get("importance", 0.5)
            if surprise > 0.7 or importance > 0.7:
                preserved.append(mem)
            else:
                to_summarize.append(mem)

        result = list(preserved)

        # Compress remaining memories into summaries
        if to_summarize:
            # Group into target_count summary chunks
            chunk_size = max(1, len(to_summarize) // max(1, target_count))
            for start in range(0, len(to_summarize), chunk_size):
                batch = to_summarize[start : start + chunk_size]
                if not batch:
                    continue

                # Build summary from content snippets
                snippets = []
                for m in batch:
                    content = m.get("content", "")
                    # Truncate to first 80 chars
                    snippet = content[:80].strip()
                    if len(content) > 80:
                        snippet += "..."
                    snippets.append(snippet)

                summary_content = f"[Summary of {len(batch)} memories] " + " | ".join(snippets)

                # Aggregate metadata
                avg_heat = sum(m.get("heat", 0.5) for m in batch) / len(batch)
                avg_importance = sum(m.get("importance", 0.5) for m in batch) / len(batch)
                avg_confidence = sum(m.get("confidence", 1.0) for m in batch) / len(batch)

                result.append(
                    {
                        "content": summary_content,
                        "heat": avg_heat,
                        "importance": avg_importance,
                        "confidence": avg_confidence,
                        "surprise_score": 0.0,
                        "tags": [],
                        "_is_summary": True,
                        "_summarized_count": len(batch),
                        "_source_ids": [m.get("id") for m in batch if m.get("id")],
                    }
                )

        return result

    @observe(tier="stage")
    def _apply_primacy_recency(
        self, scored_chunks: list[tuple[int, list[dict], float]]
    ) -> list[tuple[int, list[dict], float]]:
        """Position chunks for primacy-recency effect.

        - Highest importance → position 0 (primacy, first 20%)
        - Second highest → last position (recency, last 10%)
        - Others → middle positions by descending importance
        """
        if len(scored_chunks) <= 1:
            return list(scored_chunks)

        if len(scored_chunks) == 2:
            # First = highest, second = next highest at end
            return list(scored_chunks)

        # Already sorted by score descending
        first = scored_chunks[0]  # primacy
        last = scored_chunks[1]  # recency (second highest)
        middle = scored_chunks[2:]  # rest in middle

        return [first] + middle + [last]

    @staticmethod
    @observe(tier="stage")
    def _position_reason(position: int, total: int) -> str:
        """Return a human-readable reason for a chunk's position."""
        if position == 0:
            return "primacy"
        if position == total - 1 and total > 1:
            return "recency"
        return "middle"
