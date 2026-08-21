"""Re-embedding and compression mixin for SleepComputeEngine."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from yadgar._shared.observability.tracing import trace_span

logger = logging.getLogger(__name__)

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

    if TYPE_CHECKING:
        # Provided by SleepComputeEngine, which mixes this class in. Declared
        # here (type-check time only, zero runtime effect) so mypy can resolve
        # the self._storage / self._embeddings / self._settings call sites in
        # this module instead of reporting them all as attr-defined errors —
        # the same pattern _MemoryMixin uses for the attributes _ClientMixin
        # supplies. `_settings` is newly read here (task 296), so without this
        # the file would GAIN type errors against the ratchet baseline.
        _storage: Any
        _embeddings: Any
        _settings: Any

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

            # task 296: compression is the WORST carrier of the task-94 defect —
            # nightly, unattended, corpus-wide. update_memory_fields above has
            # already nulled the stale enrichment columns (the floor), but this
            # path can do better: it holds the settings and the embeddings
            # engine, so it re-derives from the COMPRESSED text via the same
            # producer insert_memory uses. Deliberately AFTER the raw re-embed
            # (the resync's own encode_document_enriched write must win, exactly
            # as it does at insert time and in memory_update) and OUTSIDE its
            # guard, so a failed raw re-embed still leaves the row honest.
            try:
                self._storage.resync_enrichment_on_content_change(
                    mem_id, compressed_content, self._settings, self._embeddings, new_embedding
                )
            except Exception:  # noqa: BLE001 — best-effort; must not abort the remaining rows
                logger.warning(
                    "enrichment resync failed for memory %s during compression "
                    "(content+embedding write committed; stale enrichment already nulled)",
                    mem_id,
                    exc_info=True,
                )

            compressed_count += 1

        return compressed_count
