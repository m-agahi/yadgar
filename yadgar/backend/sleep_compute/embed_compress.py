"""Re-embedding and compression mixin for SleepComputeEngine."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from yadgar._shared.observability.tracing import trace_span

# Sentence boundary splitter
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")

# Entity-like patterns for identifying key sentences during compression
_ENTITY_PATTERN_RE = re.compile(
    r"(?:[\w@.-]+/[\w@.-]+\.\w+"  # file paths
    r"|\bdef\s+\w+"  # python defs
    r"|\bclass\s+\w+"  # python classes
    r"|\b\w*(?:Error|Exception)\b"  # error types
    r"|\bimport\s+\w+"  # imports
    r"|\bfrom\s+\w+)",  # from imports
)


class _EmbedCompressMixin:
    """Re-embedding and memory compression operations."""

    @trace_span()
    def reembed_stale(self) -> int:
        """Re-embed memories whose embedding_model differs from the current model."""
        current_model = self._embeddings.get_model_name()
        stale = self._storage.get_memories_needing_reembedding(current_model)

        if not stale:
            return 0

        count = 0
        batch_size = 50

        for i in range(0, len(stale), batch_size):
            batch = stale[i : i + batch_size]
            texts = [m["content"] for m in batch]
            embeddings = self._embeddings.encode_batch(texts)

            for mem, emb in zip(batch, embeddings, strict=False):
                if emb is not None:
                    self._storage.update_memory_embedding(mem["id"], emb, current_model)
                    count += 1

        return count

    @trace_span()
    def compress_old_memories(self, days_threshold: int = 30) -> int:
        """Compress old verbose memories by extracting key entity-bearing sentences."""
        cutoff = (datetime.now(UTC) - timedelta(days=days_threshold)).isoformat()

        all_memories = self._storage.get_all_memories_for_decay()
        rows = [
            m
            for m in all_memories
            if m.get("created_at", "") < cutoff
            and len(m.get("content", "")) > 1000
            and not m.get("compressed", False)
        ]

        compressed_count = 0
        for row in rows:
            mem_id, content = row["id"], row["content"]

            # Split into sentences and keep those containing entity patterns
            sentences = _SENTENCE_RE.split(content)
            key_sentences = [s for s in sentences if _ENTITY_PATTERN_RE.search(s)]

            if not key_sentences:
                # Fallback: keep first and last sentences
                key_sentences = [sentences[0]]
                if len(sentences) > 1:
                    key_sentences.append(sentences[-1])

            compressed_content = " ".join(key_sentences)

            # Only compress if actually shorter
            if len(compressed_content) >= len(content):
                continue

            # Update content, set compressed flag, re-embed
            new_embedding = self._embeddings.encode(compressed_content)
            self._storage.update_memory_fields(mem_id, content=compressed_content, compressed=1)

            if new_embedding is not None:
                self._storage.update_memory_embedding(
                    mem_id, new_embedding, self._embeddings.get_model_name()
                )

            compressed_count += 1

        return compressed_count
