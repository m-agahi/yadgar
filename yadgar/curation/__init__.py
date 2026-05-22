"""Active memory curation engine — deduplication, merging, contradiction detection, and self-improvement."""

import logging
import time

from yadgar.config import Settings
from yadgar.curation.contradiction import _ACTION_RE, _NEGATION_RE, detect_contradictions
from yadgar.curation.ingestion import (
    _LINK_HIGH,
    _LINK_LOW,
    NewMemorySpec,
    create_link,
    find_similar_memories,
    has_textual_overlap,
    insert_new_memory,
    merge_memory,
)
from yadgar.curation.prune_passes import _memify_prune
from yadgar.curation.strengthen import _memify_derive, _memify_reweight, _memify_strengthen
from yadgar.embeddings import EmbeddingEngine
from yadgar.storage import StorageEngine
from yadgar.thermodynamics import MemoryThermodynamics

logger = logging.getLogger(__name__)

__all__ = [
    "MemoryCurator",
    # re-export constants for backward compatibility
    "_NEGATION_RE",
    "_ACTION_RE",
    "_LINK_LOW",
    "_LINK_HIGH",
]


class MemoryCurator:
    """Active memory curation on ingestion and self-improvement during consolidation.

    Implements:
    - Merge/link/create decisions on remember
    - Contradiction detection
    - Memify self-improvement cycle (prune, strengthen, reweight, derive)
    """

    def __init__(
        self,
        storage: StorageEngine,
        embeddings: EmbeddingEngine,
        thermodynamics: MemoryThermodynamics,
        settings: Settings,
    ) -> None:
        self._storage = storage
        self._embeddings = embeddings
        self._thermo = thermodynamics
        self._settings = settings

    # ── a. Active Curation on Ingestion ──────────────────────────────────

    def curate_on_remember(
        self,
        content: str,
        context: str,
        tags: list[str],
        embedding: bytes,
        *,
        initial_heat: float = 1.0,
        surprise: float = 0.0,
        importance: float = 0.5,
        valence: float = 0.0,
        file_hash: str | None = None,
        embedding_model: str | None = None,
        contextual_prefix: str | None = None,
    ) -> dict:
        """Decide whether to merge, link, or create a new memory.

        Returns dict with "action" key: "merged", "linked", or "created".
        """
        threshold = self._settings.CURATION_SIMILARITY_THRESHOLD

        # Search existing memories for similar content
        similar = self._find_similar_memories(embedding, min_sim=_LINK_LOW)

        # Check for high similarity -> merge (requires textual overlap too)
        for mem_id, sim in similar:
            if sim >= threshold:
                existing = self._storage.get_memory(mem_id)
                if existing and self._has_textual_overlap(content, existing["content"]):
                    return self._merge_memory(mem_id, content, tags, embedding, contextual_prefix)

        spec = NewMemorySpec(
            tags=tags,
            embedding=embedding,
            heat=initial_heat,
            file_hash=file_hash,
            embedding_model=embedding_model,
            contextual_prefix=contextual_prefix,
            surprise=surprise,
            importance=importance,
            valence=valence,
        )

        # Check for moderate similarity -> link
        for mem_id, sim in similar:
            if _LINK_LOW <= sim < threshold:
                new_id = self._insert_new_memory(content, context, spec)
                self._create_link(new_id, mem_id)
                return {"action": "linked", "memory_id": new_id, "linked_to": mem_id}

        # No similar memory -> create new
        new_id = self._insert_new_memory(content, context, spec)
        return {"action": "created", "memory_id": new_id}

    def _find_similar_memories(
        self, embedding: bytes, min_sim: float = 0.6
    ) -> list[tuple[int, float]]:
        """Find existing memories above min_sim, sorted by descending similarity."""
        return find_similar_memories(self._storage, self._embeddings, embedding, min_sim)

    @staticmethod
    def _has_textual_overlap(new_content: str, existing_content: str) -> bool:
        """Check if new content has meaningful textual overlap with existing."""
        return has_textual_overlap(new_content, existing_content)

    def _merge_memory(
        self,
        existing_id: int,
        new_content: str,
        new_tags: list[str],
        new_embedding: bytes,
        contextual_prefix: str | None,
    ) -> dict:
        """Merge new content into an existing memory."""
        return merge_memory(
            self._storage,
            self._embeddings,
            existing_id,
            new_content,
            new_tags,
            new_embedding,
            contextual_prefix,
        )

    def _insert_new_memory(
        self,
        content: str,
        context: str,
        spec: NewMemorySpec | None = None,
    ) -> int:
        """Insert a brand-new memory and set its scores."""
        return insert_new_memory(self._storage, content, context, spec)

    def _create_link(self, new_id: int, existing_id: int) -> None:
        """Create a derived_from relationship between two memories via entities."""
        create_link(self._storage, new_id, existing_id)

    # ── b. Contradiction Detection ───────────────────────────────────────

    def detect_contradictions(self, new_content: str, new_embedding: bytes) -> list[dict]:
        """Find existing memories that may contradict new_content.

        Returns list of dicts: {"memory_id", "content", "similarity", "reason"}.
        """
        if new_embedding is None:
            return []

        similar = self._find_similar_memories(new_embedding, min_sim=0.7)
        return detect_contradictions(self._storage, similar, new_content)

    # ── c. Memify Self-Improvement Layer ─────────────────────────────────

    def memify_cycle(self) -> dict:
        """Run the full memify self-improvement cycle.

        Returns stats: {pruned, strengthened, reweighted, derived}.
        """
        stats = {"pruned": 0, "strengthened": 0, "reweighted": 0, "derived": 0}
        _cycle_start = time.monotonic()

        _t = time.monotonic()
        logger.info("phase: memify_prune starting")
        self._memify_prune(stats)
        logger.info("phase: memify_prune complete in %dms", int((time.monotonic() - _t) * 1000))

        _t = time.monotonic()
        logger.info("phase: memify_strengthen starting")
        self._memify_strengthen(stats)
        logger.info(
            "phase: memify_strengthen complete in %dms", int((time.monotonic() - _t) * 1000)
        )

        _t = time.monotonic()
        logger.info("phase: memify_reweight starting")
        self._memify_reweight(stats)
        logger.info("phase: memify_reweight complete in %dms", int((time.monotonic() - _t) * 1000))

        _t = time.monotonic()
        logger.info("phase: memify_derive starting")
        self._memify_derive(stats)
        logger.info("phase: memify_derive complete in %dms", int((time.monotonic() - _t) * 1000))

        logger.info(
            "Memify cycle complete in %dms: %s",
            int((time.monotonic() - _cycle_start) * 1000),
            stats,
        )
        return stats

    def _memify_prune(self, stats: dict) -> None:
        """Delete cold, unaccessed, stale auto-generated memories."""
        _memify_prune(self._storage, self._settings, stats)

    def _memify_strengthen(self, stats: dict) -> None:
        """Boost importance for memories accessed > 5 times with confidence > 0.8."""
        _memify_strengthen(self._storage, stats)

    def _memify_reweight(self, stats: dict) -> None:
        """Adjust relationship weights based on usage patterns."""
        _memify_reweight(self._storage, stats)

    def _memify_derive(self, stats: dict) -> None:
        """Generate synthetic derived-fact memories for high-weight entity pairs."""
        _memify_derive(self._storage, self._embeddings, stats)
