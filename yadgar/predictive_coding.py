"""Predictive coding write gate — stores only prediction errors (surprising information).

The brain maintains a generative model and only encodes prediction errors —
information that violates expectations. For Yadgar, the "generative model"
is the aggregate of existing memories for a directory context. New observations
that are EXPECTED (low surprisal) are skipped. Only SURPRISING observations
are stored.

References:
  - Friston, "Active Inference" (2020): Free energy minimization drives memory
  - Barron et al. (Progress in Neurobiology, 2020): Hippocampus as prediction error generator
  - Titans (Google, arXiv:2501.00663): Surprise metric drives memory retention in ML
"""
# Module size justified: single-responsibility write gate — all sections (cache, continuity, surprisal, boundary, model) feed should_store(); shared _recent_stores/_entity_cache state prevents split.

import logging
import re
from collections import Counter, deque
from datetime import UTC, datetime
from typing import Any

import numpy as np

from yadgar.config import Settings
from yadgar.embeddings import EmbeddingEngine
from yadgar.retrieval import Retriever
from yadgar.storage import StorageEngine

logger = logging.getLogger(__name__)

# Bypass keywords — content matching these is ALWAYS stored regardless of surprisal
_ERROR_BYPASS_RE = re.compile(
    r"\b(error|exception|traceback|failed|bug|crash)\b",
    re.IGNORECASE,
)
_DECISION_BYPASS_RE = re.compile(
    r"\b(decided|chose|switched to|migrated|architecture)\b",
    re.IGNORECASE,
)
_BYPASS_TAGS = frozenset({"important", "critical"})


class WriteGate:
    """Write gate that filters incoming memories by surprisal.

    Only stores prediction errors — information that violates the existing
    generative model for a directory context. Boilerplate code changes
    (low surprise) are skipped; novel architectural decisions, unusual bugs,
    and unexpected failures (high surprise) are stored.
    """

    def __init__(
        self,
        storage: StorageEngine,
        embeddings: EmbeddingEngine,
        retriever: Retriever,
        settings: Settings,
    ) -> None:
        self._storage = storage
        self._embeddings = embeddings
        self._retriever = retriever
        self._settings = settings
        self._threshold = settings.WRITE_GATE_THRESHOLD
        # Task continuity tracking — recent stores form a "working context"
        self._recent_stores: deque[dict] = deque(maxlen=settings.WRITE_GATE_CONTINUITY_WINDOW)
        # Entity-set cache — avoids O(N·M) get_all_entities() on every write-gate eval.
        # _entity_cache: the cached list; _entity_cache_ts: monotonic time of last fetch.
        self._entity_cache: list[dict[str, Any]] | None = None
        self._entity_cache_ts: float = 0.0

    # ── Entity Cache ─────────────────────────────────────────────────────

    def _get_cached_entities(self) -> list[dict[str, Any]]:
        """Return all entities, using a TTL cache to avoid redundant DB fetches.

        Cache lifetime is controlled by PREDICTIVE_CODING_ENTITY_TTL_SECONDS.
        When TTL is 0 the cache is disabled (always fetches).
        """
        import time

        ttl = self._settings.PREDICTIVE_CODING_ENTITY_TTL_SECONDS
        now = time.monotonic()
        if self._entity_cache is None or ttl == 0 or (now - self._entity_cache_ts) >= ttl:
            self._entity_cache = self._storage.get_all_entities(min_heat=0.0, include_archived=True)
            self._entity_cache_ts = now
        return self._entity_cache

    def invalidate_entity_cache(self) -> None:
        """Invalidate the entity-set cache.

        Call after inserting or deleting entities so the next write-gate
        evaluation fetches a fresh set.
        """
        self._entity_cache = None
        self._entity_cache_ts = 0.0

    # ── Task Continuity ──────────────────────────────────────────────────

    def record_stored(self, content: str, directory: str, embedding) -> None:
        """Record a successfully stored memory for task continuity tracking.

        Called after a memory passes the gate and is stored. Builds up the
        'working context' that reduces the threshold for follow-up memories
        about the same task.
        """
        self._recent_stores.append(
            {
                "directory": directory,
                "embedding": embedding,
                "timestamp": datetime.now(UTC),
            }
        )

    def _compute_task_continuity(self, content: str, directory: str) -> float:
        """How task-continuous is this content with recent stores?

        Returns 0.0 (no continuity) to 1.0 (strong continuity).
        High continuity = should lower the write gate threshold because
        the user is actively working on this task and incremental progress
        matters even if it's not 'surprising'.

        Three signals:
          - Directory match: same project = likely same task
          - Temporal proximity: recent stores = active task
          - Semantic similarity: working on same concept
        """
        if not self._recent_stores:
            return 0.0

        n = len(self._recent_stores)

        # Signal 1: Directory match
        dir_matches = sum(1 for s in self._recent_stores if s["directory"] == directory)
        dir_continuity = dir_matches / n

        # Signal 2: Temporal proximity (within last hour)
        now = datetime.now(UTC)
        recent_count = sum(
            1 for s in self._recent_stores if (now - s["timestamp"]).total_seconds() < 3600
        )
        temporal_continuity = recent_count / n

        # Signal 3: Semantic similarity to recent stores
        semantic_continuity = 0.0
        embedding = self._embeddings.encode(content)
        if embedding is not None:
            sims = []
            for s in self._recent_stores:
                if s.get("embedding") is not None:
                    sim = self._embeddings.similarity(embedding, s["embedding"])
                    sims.append(sim)
            if sims:
                semantic_continuity = max(sims)

        # Weighted combination
        continuity = 0.3 * dir_continuity + 0.3 * temporal_continuity + 0.4 * semantic_continuity

        return min(1.0, continuity)

    # ── Core: Surprisal Computation ──────────────────────────────────────

    def compute_surprisal(self, content: str, directory: str, tags: list[str]) -> float:
        """Compute how surprising content is relative to the directory's generative model.

        Combines four signals:
          Signal 1 — Embedding novelty (weight 0.4)
          Signal 2 — Entity novelty (weight 0.25)
          Signal 3 — Temporal novelty (weight 0.2)
          Signal 4 — Structural novelty (weight 0.15)

        Returns a float in [0.0, 1.0] where 1.0 = maximally surprising.
        """
        # Build generative model for this directory
        recent_memories = self._storage.get_memories_for_directory(directory, min_heat=0.0)
        # Sort by created_at descending, take last 50
        recent_memories.sort(key=lambda m: m.get("created_at", ""), reverse=True)
        recent_memories = recent_memories[:50]

        if not recent_memories:
            # New directory = somewhat surprising
            return 0.8

        # Signal 1: Embedding novelty (weight 0.4)
        embedding_novelty = self._compute_embedding_novelty(content)

        # Signal 2: Entity novelty (weight 0.25)
        entity_novelty = self._compute_entity_novelty(content, directory)

        # Signal 3: Temporal novelty (weight 0.2)
        temporal_novelty = self._compute_temporal_novelty(content, directory)

        # Signal 4: Structural novelty (weight 0.15)
        structural_novelty = self._compute_structural_novelty(content, directory)

        # Weighted sum
        surprisal = (
            0.40 * embedding_novelty
            + 0.25 * entity_novelty
            + 0.20 * temporal_novelty
            + 0.15 * structural_novelty
        )

        return max(0.0, min(1.0, surprisal))

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

    def _compute_entity_novelty(self, content: str, directory: str) -> float:
        """Signal 2: How many entities in this content are new to the graph?

        entity_novelty = new_entities / total_entities (or 0.5 if no entities)
        """
        # Use knowledge graph entity extraction
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

    # ── Write Gate Decision ──────────────────────────────────────────────

    def should_store(
        self, content: str, directory: str, tags: list[str]
    ) -> tuple[bool, float, str]:
        """Decide whether to store a memory based on surprisal.

        Returns (should_store, surprisal_score, reason).

        Bypass conditions (always store):
          - Error/exception keywords in content
          - Decision keywords in content
          - Tags contain "important" or "critical"
        """
        if self._settings.WRITE_GATE_THRESHOLD <= 0.0:
            return True, 0.0, "gate_disabled"

        # Q10: assign content_lower and use for case-insensitive checks
        content_lower = content.lower()

        # Check bypass conditions FIRST
        if _ERROR_BYPASS_RE.search(content_lower):
            surprisal = self.compute_surprisal(content, directory, tags)
            logger.debug(
                "Write gate BYPASS (error keywords): surprisal=%.3f dir=%s",
                surprisal,
                directory,
            )
            return (True, surprisal, "bypass_error_keywords")

        if _DECISION_BYPASS_RE.search(content_lower):
            surprisal = self.compute_surprisal(content, directory, tags)
            logger.debug(
                "Write gate BYPASS (decision keywords): surprisal=%.3f dir=%s",
                surprisal,
                directory,
            )
            return (True, surprisal, "bypass_decision_keywords")

        if _BYPASS_TAGS & set(t.lower() for t in tags):
            surprisal = self.compute_surprisal(content, directory, tags)
            logger.debug(
                "Write gate BYPASS (important/critical tag): surprisal=%.3f dir=%s",
                surprisal,
                directory,
            )
            return (True, surprisal, "bypass_important_tag")

        # Compute surprisal for gating decision
        surprisal = self.compute_surprisal(content, directory, tags)

        # Adaptive threshold: lower it when working on the same task
        # This prevents the gate from blocking incremental progress
        continuity = self._compute_task_continuity(content, directory)
        discount = continuity * self._settings.WRITE_GATE_CONTINUITY_DISCOUNT
        effective_threshold = max(0.1, self._threshold - discount)

        if surprisal >= effective_threshold:
            logger.debug(
                "Write gate PASS: surprisal=%.3f >= effective_threshold=%.3f "
                "(base=%.3f, continuity=%.2f, discount=%.3f) dir=%s",
                surprisal,
                effective_threshold,
                self._threshold,
                continuity,
                discount,
                directory,
            )
            reason = "high_surprisal"
            if discount > 0:
                reason = f"task_continuous (threshold={effective_threshold:.2f})"
            return (True, surprisal, reason)
        else:
            logger.debug(
                "Write gate BLOCK: surprisal=%.3f < effective_threshold=%.3f "
                "(base=%.3f, continuity=%.2f) dir=%s",
                surprisal,
                effective_threshold,
                self._threshold,
                continuity,
                directory,
            )
            return (False, surprisal, f"below_threshold (effective={effective_threshold:.2f})")

    def would_reject_at(
        self,
        content: str,
        directory: str,
        tags: list[str],
        threshold: float,
        surprisal: float | None = None,
    ) -> bool:
        """Shadow-gate helper: would the gate REJECT this content at the given threshold?

        Faithful to should_store() logic — uses the same adaptive (continuity-adjusted)
        threshold. Called with WRITE_GATE_SHADOW_THRESHOLD; the actual WRITE_GATE_THRESHOLD
        is NOT used here, so a disabled gate (threshold=0.0) still produces meaningful shadow
        decisions.

        Bypass conditions (error/decision keywords, important/critical tags) → False (not
        rejected) because those memories are always stored regardless of surprisal.

        Args:
            content:   Memory content.
            directory: Directory context (same as passed to should_store).
            tags:      Memory tags.
            threshold: Shadow base threshold to evaluate against (typically
                       settings.WRITE_GATE_SHADOW_THRESHOLD).
            surprisal: Pre-computed surprisal from should_store() — reused to avoid a
                       second embedding call. When None, compute_surprisal() is called.

        Returns:
            True  — gate WOULD reject at this threshold (surprisal < effective_threshold).
            False — gate WOULD pass (surprisal >= effective_threshold or bypass applies).
        """
        if threshold <= 0.0:
            return False  # shadow threshold disabled — nothing would be rejected

        content_lower = content.lower()

        # Bypass conditions → always stored, never rejected
        if (
            _ERROR_BYPASS_RE.search(content_lower)
            or _DECISION_BYPASS_RE.search(content_lower)
            or (_BYPASS_TAGS & set(t.lower() for t in tags))
        ):
            return False

        if surprisal is None:
            surprisal = self.compute_surprisal(content, directory, tags)
        continuity = self._compute_task_continuity(content, directory)
        discount = continuity * self._settings.WRITE_GATE_CONTINUITY_DISCOUNT
        effective_threshold = max(0.1, threshold - discount)
        return surprisal < effective_threshold

    # ── Event Boundary Detection ─────────────────────────────────────────

    def compute_boundary_signal(self, content: str, previous_content: str) -> float:
        """Detect event boundaries — transitions between different topics/tasks.

        Encodes both contents, computes similarity.
        boundary_strength = 1.0 - similarity
        If boundary > 0.6, this is a strong topic transition.

        Returns boundary strength in [0.0, 1.0].
        """
        emb_current = self._embeddings.encode(content)
        emb_previous = self._embeddings.encode(previous_content)

        if emb_current is None or emb_previous is None:
            return 0.5  # Can't compute, moderate boundary

        similarity = self._embeddings.similarity(emb_current, emb_previous)
        boundary_strength = max(0.0, min(1.0, 1.0 - similarity))

        if boundary_strength > 0.6:
            logger.debug(
                "Strong topic transition detected: boundary=%.3f",
                boundary_strength,
            )

        return boundary_strength

    # ── Directory Generative Model ───────────────────────────────────────

    def _extract_common_tags(self, memories: list[dict]) -> list[str]:
        """Return the 10 most frequent tags across memories.

        Handles tags stored as JSON strings or as lists.
        """
        import json

        tag_counter: Counter = Counter()
        for m in memories:
            tags = m.get("tags", [])
            if isinstance(tags, str):
                try:
                    tags = json.loads(tags)
                except (ValueError, TypeError):  # fmt: skip
                    tags = []
            for tag in tags:
                tag_counter[tag] += 1
        return [tag for tag, _ in tag_counter.most_common(10)]

    def _entity_names_in(self, memories: list[dict], all_entities: list[dict]) -> set[str]:
        """Return names of cached entities that appear in any of the given memories."""
        found: set[str] = set()
        for m in memories:
            content = m.get("content", "")
            for e in all_entities:
                if e["name"] in content:
                    found.add(e["name"])
        return found

    def _compute_centroid(self, memories: list[dict]) -> bytes | None:
        """Compute the mean embedding of memories as bytes, or None if unavailable."""
        dim = self._embeddings.get_dimensions()
        embeddings_list = []
        for m in memories:
            emb = m.get("embedding")
            if emb is None:
                continue
            arr = np.frombuffer(emb, dtype=np.float32)
            if len(arr) == dim:
                embeddings_list.append(arr)
        if not embeddings_list:
            return None
        centroid = np.mean(embeddings_list, axis=0).astype(np.float32)
        return centroid.tobytes()

    def get_directory_model(self, directory: str) -> dict:
        """Build summary of what Yadgar 'knows' about a directory.

        Returns a dict with:
          - memory_count: number of memories for this directory
          - entity_count: number of unique entities mentioned
          - avg_heat: average heat of directory memories
          - common_tags: most frequent tags
          - recent_topics: recent entity names
          - centroid_embedding: mean of all directory memory embeddings (bytes or None)
        """
        memories = self._storage.get_memories_for_directory(directory, min_heat=0.0)

        if not memories:
            return {
                "memory_count": 0,
                "entity_count": 0,
                "avg_heat": 0.0,
                "common_tags": [],
                "recent_topics": [],
                "centroid_embedding": None,
            }

        memory_count = len(memories)
        avg_heat = sum(m["heat"] for m in memories) / memory_count
        common_tags = self._extract_common_tags(memories)

        # Entity count and recent topics — fetch cached list once, reuse for both calls.
        all_entities = self._get_cached_entities()
        entity_names = self._entity_names_in(memories, all_entities)

        recent_memories = sorted(memories, key=lambda m: m.get("created_at", ""), reverse=True)[:10]
        recent_entity_names = self._entity_names_in(recent_memories, all_entities)

        centroid_embedding = self._compute_centroid(memories)

        return {
            "memory_count": memory_count,
            "entity_count": len(entity_names),
            "avg_heat": round(avg_heat, 4),
            "common_tags": common_tags,
            "recent_topics": list(recent_entity_names)[:10],
            "centroid_embedding": centroid_embedding,
        }
