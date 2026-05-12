"""Active memory curation engine — deduplication, merging, contradiction detection, and self-improvement."""

import json
import logging
import re
import time

from yadgar.config import Settings
from yadgar.embeddings import EmbeddingEngine
from yadgar.storage import StorageEngine
from yadgar.thermodynamics import MemoryThermodynamics

logger = logging.getLogger(__name__)

# Negation patterns for contradiction detection
_NEGATION_RE = re.compile(
    r"\b(not|don't|doesn't|didn't|won't|can't|cannot|isn't|aren't|wasn't|weren't|"
    r"no longer|instead of|rather than|replaced|switched from|stopped using|"
    r"removed|deprecated|dropped|never)\b",
    re.IGNORECASE,
)

# Verb extraction for entity-action comparison
_ACTION_RE = re.compile(
    r"\b(use|using|uses|prefer|prefers|run|runs|running|install|installed|"
    r"deploy|deployed|enable|enabled|disable|disabled|add|added|remove|removed|"
    r"switch|switched|migrate|migrated|choose|chose|set|configured)\b",
    re.IGNORECASE,
)

# Moderate similarity range for linking
_LINK_LOW = 0.6
_LINK_HIGH = 0.85


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

        # Check for moderate similarity -> link
        for mem_id, sim in similar:
            if _LINK_LOW <= sim < threshold:
                new_id = self._insert_new_memory(
                    content,
                    context,
                    tags,
                    embedding,
                    initial_heat,
                    file_hash,
                    embedding_model,
                    contextual_prefix,
                    surprise,
                    importance,
                    valence,
                )
                self._create_link(new_id, mem_id)
                return {"action": "linked", "memory_id": new_id, "linked_to": mem_id}

        # No similar memory -> create new
        new_id = self._insert_new_memory(
            content,
            context,
            tags,
            embedding,
            initial_heat,
            file_hash,
            embedding_model,
            contextual_prefix,
            surprise,
            importance,
            valence,
        )
        return {"action": "created", "memory_id": new_id}

    def _find_similar_memories(
        self, embedding: bytes, min_sim: float = 0.6
    ) -> list[tuple[int, float]]:
        """Find existing memories above min_sim, sorted by descending similarity."""
        if embedding is None:
            return []

        vec_hits = self._storage.search_vectors(embedding, top_k=10, min_heat=0.0)
        results = []
        for mid, _distance in vec_hits:
            mem = self._storage.get_memory(mid)
            if mem and mem.get("embedding"):
                sim = self._embeddings.similarity(embedding, mem["embedding"])
                if sim >= min_sim:
                    results.append((mid, sim))

        results.sort(key=lambda x: x[1], reverse=True)
        return results

    @staticmethod
    def _has_textual_overlap(new_content: str, existing_content: str) -> bool:
        """Check if new content has meaningful textual overlap with existing.

        Prevents merging memories that are semantically similar (high embedding
        similarity) but clearly different pieces of information.
        """
        new_words = set(new_content.lower().split())
        existing_words = set(existing_content.lower().split())
        if not new_words or not existing_words:
            return False
        intersection = new_words & existing_words
        union = new_words | existing_words
        jaccard = len(intersection) / len(union) if union else 0.0
        return jaccard > 0.5

    def _merge_memory(
        self,
        existing_id: int,
        new_content: str,
        new_tags: list[str],
        new_embedding: bytes,
        contextual_prefix: str | None,
    ) -> dict:
        """Merge new content into an existing memory."""
        existing = self._storage.get_memory(existing_id)
        if existing is None:
            # Race: candidate was deleted between search and merge — signal caller to handle gracefully
            return {"action": "created", "memory_id": None}

        # Combine content
        merged_content = existing["content"] + "\n" + new_content

        # Union tags
        existing_tags = existing.get("tags", [])
        if isinstance(existing_tags, str):
            existing_tags = json.loads(existing_tags)
        merged_tags = list(set(existing_tags) | set(new_tags))

        # Re-embed merged content
        embed_text = f"{contextual_prefix}{merged_content}" if contextual_prefix else merged_content
        merged_embedding = self._embeddings.encode(embed_text)

        # Update memory in DB
        self._storage.update_memory_fields(
            existing_id,
            content=merged_content,
            tags=json.dumps(merged_tags),
            heat=1.0,
            last_accessed=self._storage._now_iso(),
        )

        # Update embedding in vec0
        if merged_embedding is not None:
            self._storage.update_memory_fields(existing_id, embedding=merged_embedding)
            try:
                self._storage.update_vector(existing_id, merged_embedding)
            except Exception:
                pass

        # Update FTS content is handled by the trigger on memories table

        logger.debug("Merged new content into memory %d", existing_id)
        return {"action": "merged", "memory_id": existing_id}

    def _insert_new_memory(
        self,
        content: str,
        context: str,
        tags: list[str],
        embedding: bytes,
        heat: float,
        file_hash: str | None,
        embedding_model: str | None,
        contextual_prefix: str | None,
        surprise: float,
        importance: float,
        valence: float,
    ) -> int:
        """Insert a brand-new memory and set its scores."""
        memory_id = self._storage.insert_memory(
            {
                "content": content,
                "embedding": embedding,
                "tags": tags,
                "directory_context": context,
                "heat": heat,
                "is_stale": False,
                "file_hash": file_hash,
                "embedding_model": embedding_model,
            }
        )

        if contextual_prefix:
            self._storage.update_memory_fields(memory_id, contextual_prefix=contextual_prefix)

        self._storage.update_memory_scores(
            memory_id,
            surprise_score=surprise,
            importance=importance,
            emotional_valence=valence,
        )

        return memory_id

    def _create_link(self, new_id: int, existing_id: int) -> None:
        """Create a derived_from relationship between two memories via entities."""
        self._storage._now_iso()
        # Use entity system: create ephemeral entity nodes for both memories
        # and link them with a derived_from relationship
        src_entity = self._storage.get_entity_by_name(f"memory:{new_id}")
        if src_entity is None:
            src_eid = self._storage.insert_entity({"name": f"memory:{new_id}", "type": "file"})
        else:
            src_eid = src_entity["id"]

        tgt_entity = self._storage.get_entity_by_name(f"memory:{existing_id}")
        if tgt_entity is None:
            tgt_eid = self._storage.insert_entity({"name": f"memory:{existing_id}", "type": "file"})
        else:
            tgt_eid = tgt_entity["id"]

        self._storage.insert_relationship(
            {
                "source_entity_id": src_eid,
                "target_entity_id": tgt_eid,
                "relationship_type": "derived_from",
            }
        )
        logger.debug("Linked memory %d -> derived_from -> memory %d", new_id, existing_id)

    # ── b. Contradiction Detection ───────────────────────────────────────

    def detect_contradictions(self, new_content: str, new_embedding: bytes) -> list[dict]:
        """Find existing memories that may contradict new_content.

        Returns list of dicts: {"memory_id", "content", "similarity", "reason"}.
        """
        if new_embedding is None:
            return []

        similar = self._find_similar_memories(new_embedding, min_sim=0.7)
        contradictions = []

        new_has_negation = bool(_NEGATION_RE.search(new_content))
        new_actions = set(a.lower() for a in _ACTION_RE.findall(new_content))

        for mem_id, sim in similar:
            mem = self._storage.get_memory(mem_id)
            if mem is None:
                continue

            old_content = mem["content"]
            old_has_negation = bool(_NEGATION_RE.search(old_content))

            # Check 1: one has negation patterns, the other doesn't
            if new_has_negation != old_has_negation:
                contradictions.append(
                    {
                        "memory_id": mem_id,
                        "content": old_content,
                        "similarity": sim,
                        "reason": "negation_mismatch",
                    }
                )
                # Reduce confidence of old contradicting memory
                old_confidence = mem.get("confidence", 1.0)
                self._storage.update_memory_fields(
                    mem_id, confidence=max(old_confidence - 0.2, 0.1)
                )
                continue

            # Check 2: same entities but different actions
            old_actions = set(a.lower() for a in _ACTION_RE.findall(old_content))
            if new_actions and old_actions and new_actions != old_actions:
                # Only flag if there's meaningful overlap in subject matter
                # (similarity > 0.7 already ensures topical overlap)
                shared = new_actions & old_actions
                if len(shared) < len(new_actions | old_actions) * 0.5:
                    contradictions.append(
                        {
                            "memory_id": mem_id,
                            "content": old_content,
                            "similarity": sim,
                            "reason": "action_divergence",
                        }
                    )
                    old_confidence = mem.get("confidence", 1.0)
                    self._storage.update_memory_fields(
                        mem_id, confidence=max(old_confidence - 0.1, 0.1)
                    )

        return contradictions

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
        """Delete cold, unaccessed, stale auto-generated memories.

        Pass 1 (action-stream): summaries tagged _action_stream that are cold
        (heat<0.01), low-confidence (<0.3), and never accessed.

        Pass 2 (auto-generated): memories tagged "auto-generated" (derived facts,
        dream insights, CLS semantic promotions) that are cold (heat<COLD_THRESHOLD),
        never accessed (access_count==0 or NONE), older than
        AUTO_GENERATED_MEMORY_MAX_AGE_DAYS, and not protected.

        User-created memories are never touched by either pass.
        """
        from datetime import UTC, datetime, timedelta

        candidates = self._storage.get_memories_by_heat(min_heat=0.0, limit=10000)

        # --- Pass 1: action-stream summaries ---
        for mem in candidates:
            tags = mem.get("tags", [])
            if isinstance(tags, str):
                tags = json.loads(tags)
            if "_action_stream" not in tags:
                continue
            if mem.get("is_protected"):
                continue
            if (
                (mem.get("heat") or 0.0) < 0.01
                and (mem.get("confidence") or 1.0) < 0.3
                and (mem.get("access_count") or 0) == 0
            ):
                self._storage.delete_memory(mem["id"])
                stats["pruned"] += 1

        # --- Pass 2: cold unaccessed auto-generated memories ---
        max_age_days = self._settings.AUTO_GENERATED_MEMORY_MAX_AGE_DAYS
        if max_age_days <= 0:
            return  # disabled

        cold_threshold = self._settings.COLD_THRESHOLD
        age_cutoff = (datetime.now(UTC) - timedelta(days=max_age_days)).isoformat()

        for mem in candidates:
            tags = mem.get("tags", [])
            if isinstance(tags, str):
                tags = json.loads(tags)
            if "auto-generated" not in tags:
                continue
            if mem.get("is_protected"):
                continue
            if (mem.get("heat") or 0.0) >= cold_threshold:
                continue
            if (mem.get("access_count") or 0) != 0:
                continue
            created_at = mem.get("created_at") or ""
            if created_at > age_cutoff:
                continue  # too recent — spare it
            self._storage.delete_memory(mem["id"])
            stats["pruned"] += 1

    def _memify_strengthen(self, stats: dict) -> None:
        """Boost importance for memories accessed > 5 times with confidence > 0.8."""
        candidates = self._storage.get_memories_by_heat(min_heat=0.0, limit=10000)
        to_update: list[tuple[int, float]] = []
        for mem in candidates:
            if (
                (mem.get("access_count") or 0) > 5
                and (mem.get("confidence") or 0.0) > 0.8
                and (mem.get("importance") or 0.0) < 1.0
            ):
                current_importance = mem.get("importance") or 0.5
                new_importance = min(current_importance + 0.1, 1.0)
                to_update.append((mem["id"], new_importance))
                stats["strengthened"] += 1

        if to_update:
            batch = [
                (
                    "UPDATE type::record('memory', $id) SET importance = $importance",
                    {"id": mid, "importance": new_imp},
                )
                for mid, new_imp in to_update
            ]
            self._storage.batch_writes(batch)

    def _memify_reweight(self, stats: dict) -> None:
        """Adjust relationship weights based on usage patterns.

        Relationships between frequently co-retrieved memories get weight boost.
        Relationships between rarely-used entities get weight decay.
        """
        entities = self._storage.get_all_entities(min_heat=0.0, include_archived=True)
        entity_heat = {e["id"]: (e.get("heat") or 0.0) for e in entities}

        # ONE query for all relationships instead of O(N²) per-pair HTTP calls.
        relationships = self._storage.get_all_relationships()

        pending: list[tuple[int, float]] = []  # (rel_id, delta)
        for rel in relationships:
            sid = rel.get("source_entity_id")
            tid = rel.get("target_entity_id")
            if sid is None or tid is None:
                continue

            weight = rel.get("weight") or 1.0
            avg_heat = (entity_heat.get(sid, 0.0) + entity_heat.get(tid, 0.0)) / 2.0

            if avg_heat > 0.7 and weight >= 5.0:
                # Both entities are hot AND relationship is established -> boost
                pending.append((rel["id"], 0.5))
                stats["reweighted"] += 1
            elif avg_heat < 0.1:
                # Both entities are cold -> decay relationship
                new_weight = max(weight * 0.9, 0.1)
                delta = new_weight - weight
                if abs(delta) > 1e-9:
                    pending.append((rel["id"], delta))
                    stats["reweighted"] += 1

        if pending:
            now = self._storage._now_iso()
            batch = [
                (
                    "UPDATE type::record('relationship', $id) SET "
                    "weight = weight + $inc, last_reinforced = $now",
                    {"id": rel_id, "inc": delta, "now": now},
                )
                for rel_id, delta in pending
            ]
            self._storage.batch_writes(batch)

    def _memify_derive(self, stats: dict) -> None:
        """Generate synthetic derived-fact memories for high-weight entity pairs."""
        entities = self._storage.get_all_entities(min_heat=0.0, include_archived=True)
        entity_map = {e["id"]: e for e in entities}

        # Pre-build existing content set for dedup
        existing_contents = {m["content"] for m in self._storage.get_all_memories_with_embeddings()}

        now = self._storage._now_iso()
        model_name = self._embeddings.get_model_name()

        # ONE query for co_occurrence relationships instead of O(N²) per-pair HTTP calls.
        relationships = self._storage.get_relationships_by_types(["co_occurrence"])

        # Phase 1: walk relationships, compute embeddings, collect inserts
        to_insert: list[dict] = []
        for rel in relationships:
            sid = rel.get("source_entity_id")
            tid = rel.get("target_entity_id")
            if sid is None or tid is None:
                continue

            weight = rel.get("weight") or 0.0

            # Check if co-occurrence count is high enough (weight as proxy)
            if weight < 10.0:
                continue

            src_entity = entity_map.get(sid)
            tgt_entity = entity_map.get(tid)
            if src_entity is None or tgt_entity is None:
                continue

            src_name = src_entity["name"]
            tgt_name = tgt_entity["name"]

            # Check if we already derived a fact for this pair
            derived_content = f"{src_name} and {tgt_name} are frequently modified together"
            if derived_content in existing_contents:
                continue

            embedding = self._embeddings.encode(derived_content)
            memory_id = self._storage._next_id("memory")
            to_insert.append(
                {
                    "memory_id": memory_id,
                    "content": derived_content,
                    "embedding": embedding,
                    "src_name": src_name,
                    "tgt_name": tgt_name,
                }
            )
            existing_contents.add(derived_content)
            stats["derived"] += 1

        if not to_insert:
            return

        # Phase 2: batch all CREATEs and score UPDATEs into one transaction
        batch: list[tuple[str, dict | None]] = []
        for item in to_insert:
            mid = item["memory_id"]
            emb = item["embedding"]
            emb_floats = self._storage._bytes_to_floats(emb) if emb else None
            batch.append(
                (
                    "CREATE type::record('memory', $id) SET "
                    "content = $content, embedding = $embedding, tags = $tags, "
                    "source_episode_id = $source_episode_id, "
                    "directory_context = $directory_context, "
                    "created_at = $created_at, last_accessed = $last_accessed, "
                    "heat = $heat, is_stale = $is_stale, file_hash = $file_hash, "
                    "embedding_model = $embedding_model, "
                    "plasticity = $plasticity, stability = $stability, "
                    "excitability = $excitability, store_type = $store_type, "
                    "compression_level = $compression_level, sr_x = $sr_x, sr_y = $sr_y, "
                    "reconsolidation_count = $reconsolidation_count, "
                    "provenance_agent = $provenance_agent, vector_clock = $vector_clock, "
                    "is_protected = $is_protected",
                    {
                        "id": mid,
                        "content": item["content"],
                        "embedding": emb_floats,
                        "tags": ["derived", "auto-generated"],
                        "source_episode_id": None,
                        "directory_context": "system",
                        "created_at": now,
                        "last_accessed": now,
                        "heat": 0.5,
                        "is_stale": False,
                        "file_hash": None,
                        "embedding_model": model_name,
                        "plasticity": 1.0,
                        "stability": 0.0,
                        "excitability": 1.0,
                        "store_type": "episodic",
                        "compression_level": 0,
                        "sr_x": 0.0,
                        "sr_y": 0.0,
                        "reconsolidation_count": 0,
                        "provenance_agent": "default",
                        "vector_clock": "{}",
                        "is_protected": False,
                    },
                )
            )
            batch.append(
                (
                    "UPDATE type::record('memory', $id) SET "
                    "importance = $importance, surprise_score = $surprise_score, "
                    "emotional_valence = $emotional_valence",
                    {
                        "id": mid,
                        "importance": 0.6,
                        "surprise_score": 0.0,
                        "emotional_valence": 0.0,
                    },
                )
            )
        self._storage.batch_writes(batch)
