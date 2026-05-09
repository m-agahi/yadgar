"""Astrocyte consolidation engine — background daemon that processes memories during idle time."""

import logging
import re
import threading
import time
from datetime import UTC, date, datetime
from itertools import combinations

from yadgar.cls_store import DualStoreCLS
from yadgar.config import Settings
from yadgar.curation import MemoryCurator
from yadgar.embeddings import EmbeddingEngine
from yadgar.knowledge_graph import KnowledgeGraph
from yadgar.sleep_compute import SleepComputeEngine
from yadgar.storage import StorageEngine
from yadgar.thermodynamics import MemoryThermodynamics

# Lazy imports to avoid circular dependencies
_AstrocytePool = None
_CausalDiscovery = None


def _get_pool_class():
    global _AstrocytePool
    if _AstrocytePool is None:
        from yadgar.astrocyte_pool import AstrocytePool

        _AstrocytePool = AstrocytePool
    return _AstrocytePool


def _get_causal_discovery_class():
    global _CausalDiscovery
    if _CausalDiscovery is None:
        from yadgar.causal_discovery import CausalDiscovery

        _CausalDiscovery = CausalDiscovery
    return _CausalDiscovery


logger = logging.getLogger(__name__)

# Regex patterns for entity extraction
_FILE_PATH_RE = re.compile(r"(?:\.{0,2}/)?(?:[\w@.-]+/)+[\w@.-]+\.\w+")
_PYTHON_DEF_RE = re.compile(r"\b(def|class)\s+(\w+)")
_JS_FUNCTION_RE = re.compile(r"\bfunction\s+(\w+)")
_ERROR_RE = re.compile(r"\b(\w*(?:Error|Exception))\b")
_TRACEBACK_RE = re.compile(r"Traceback \(most recent call last\)")
_IMPORT_RE = re.compile(r"(?:^|\n)\s*import\s+([\w.]+)")
_FROM_IMPORT_RE = re.compile(r"(?:^|\n)\s*from\s+([\w.]+)\s+import")
_REQUIRE_RE = re.compile(r"require\(['\"]([^'\"]+)['\"]\)")
_DECISION_RE = re.compile(
    r"(?:decided|chose|choosing|using|switched to|migrated to|replaced with)"
    r"\s+(\w+(?:\s+\w+){0,3})",
    re.IGNORECASE,
)

_CODE_EXTENSIONS = frozenset(
    (
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".go",
        ".rs",
        ".java",
        ".c",
        ".h",
        ".cpp",
        ".rb",
        ".toml",
        ".yaml",
        ".yml",
        ".json",
        ".md",
        ".txt",
        ".cfg",
        ".ini",
        ".sh",
        ".css",
        ".html",
        ".sql",
        ".proto",
    )
)


class ConsolidationScheduler:
    """Background consolidation daemon inspired by astrocyte glial cells.

    Wakes up after a period of user inactivity to:
    - Apply thermodynamic decay to memory/entity heat values
    - Extract entities from new episodes and build the knowledge graph
    - Merge near-duplicate memories
    """

    def __init__(
        self,
        storage: StorageEngine,
        embeddings: EmbeddingEngine,
        settings: Settings,
    ) -> None:
        self._storage = storage
        self._embeddings = embeddings
        self._settings = settings
        self._thermo = MemoryThermodynamics(storage, embeddings, settings)
        self._graph = KnowledgeGraph(storage, settings)
        self._curator = MemoryCurator(storage, embeddings, self._thermo, settings)
        self._sleep_engine = SleepComputeEngine(
            storage, embeddings, self._graph, self._curator, self._thermo, settings
        )
        self._cls = DualStoreCLS(storage, embeddings, settings)
        self._last_sleep_cycle: datetime | None = None
        self._last_consolidation_date: date | None = None

        self.last_activity: datetime = datetime.now(UTC)
        self.is_running: bool = False
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._last_consolidated_episode_id: int = 0

        # Initialize causal discovery engine
        self._causal_discovery = None
        self._events_since_last_discovery = 0
        try:
            CausalDiscoveryCls = _get_causal_discovery_class()
            self._causal_discovery = CausalDiscoveryCls(storage, self._graph, settings)
        except Exception:
            logger.exception("Failed to initialize CausalDiscovery")

        # Initialize astrocyte pool for domain-aware consolidation
        self._pool = None
        try:
            PoolCls = _get_pool_class()
            self._pool = PoolCls(storage, embeddings, self._graph, self._thermo, settings)
            self._pool.init_processes()
        except Exception:
            logger.exception("Failed to initialize AstrocytePool")

    # -- Public API --

    def start(self) -> None:
        if self.is_running:
            return
        self._stop_event.clear()
        self._last_consolidated_episode_id = self._storage.get_max_episode_id()
        self._thread = threading.Thread(target=self._daemon_loop, daemon=True)
        self.is_running = True
        self._thread.start()
        logger.info("Astrocyte daemon started")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        self.is_running = False
        self._thread = None
        logger.info("Astrocyte daemon stopped")

    def record_activity(self) -> None:
        self.last_activity = datetime.now(UTC)

    def force_consolidate(self) -> dict:
        """Run a consolidation cycle immediately. Returns the cycle stats."""
        return self._consolidation_cycle()

    # -- Daemon loop --

    def _daemon_loop(self) -> None:
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=self._settings.DAEMON_CHECK_INTERVAL)
            if self._stop_event.is_set():
                break
            now = datetime.now(UTC)
            today = now.date()
            # Idle-triggered consolidation: process new episodes when system is idle
            idle_seconds = (now - self.last_activity).total_seconds()
            if idle_seconds >= self._settings.IDLE_THRESHOLD_SECONDS:
                new_episodes = self._storage.get_episodes_since(self._last_consolidated_episode_id)
                if new_episodes:
                    try:
                        self._consolidation_cycle()
                    except Exception:
                        logger.exception("Idle consolidation cycle failed")
            # Run once per day at 18:30 UTC (18:30–18:31 window)
            if now.hour == 18 and now.minute == 30 and self._last_consolidation_date != today:
                try:
                    self._consolidation_cycle()
                    self._last_consolidation_date = today
                except Exception:
                    logger.exception("Consolidation cycle failed")
                self._maybe_sleep_cycle()

    def _maybe_sleep_cycle(self) -> None:
        """Run a full sleep cycle if at least 6 hours since the last one."""
        now = datetime.now(UTC)
        if self._last_sleep_cycle is not None:
            hours_since = (now - self._last_sleep_cycle).total_seconds() / 3600.0
            if hours_since < 6.0:
                return
        try:
            stats = self._sleep_engine.run_sleep_cycle()
            self._last_sleep_cycle = now
            logger.info("Sleep cycle complete: %s", stats)
        except Exception:
            logger.exception("Sleep cycle failed")

    # -- Core consolidation --

    def _consolidation_cycle(self) -> dict:
        start = time.monotonic()
        stats = {
            "memories_added": 0,
            "memories_updated": 0,
            "memories_archived": 0,
            "memories_deleted": 0,
        }

        self._apply_decay(stats)
        self._process_new_episodes(stats)
        self._merge_duplicates(stats)

        # Semantic similarity linking — create relationships between similar memories
        try:
            self._link_similar_memories(stats)
        except Exception:
            logger.exception("Similarity linking failed")

        try:
            self._graph.detect_causality()
        except Exception:
            logger.exception("Causal detection failed")

        # Run formal causal discovery (PC algorithm) periodically
        if self._causal_discovery is not None:
            self._events_since_last_discovery += stats.get("memories_added", 0)
            if self._events_since_last_discovery >= 50:
                try:
                    dag = self._causal_discovery.discover_dag()
                    stats["causal_dag_edges"] = dag.get("metadata", {}).get("directed_count", 0)
                    self._events_since_last_discovery = 0
                except Exception:
                    logger.exception("Causal discovery failed")

        # Run memify self-improvement cycle
        try:
            memify_stats = self._curator.memify_cycle()
            stats["memify_pruned"] = memify_stats.get("pruned", 0)
            stats["memify_strengthened"] = memify_stats.get("strengthened", 0)
            stats["memify_reweighted"] = memify_stats.get("reweighted", 0)
            stats["memify_derived"] = memify_stats.get("derived", 0)
        except Exception:
            logger.exception("Memify cycle failed")

        # Run CLS dual-store consolidation (Go-CLS: episodic → semantic)
        try:
            cls_stats = self._cls.consolidation_cycle()
            stats["cls_patterns_found"] = cls_stats.get("patterns_found", 0)
            stats["cls_promoted"] = cls_stats.get("promoted", 0)
            stats["cls_skipped_inconsistent"] = cls_stats.get("skipped_inconsistent", 0)
        except Exception:
            logger.exception("CLS consolidation cycle failed")

        # Compression disabled: memory content must stay intact for LLM usage.

        # Process action_log entries into real memories
        try:
            action_stats = self._process_action_log()
            stats["actions_processed"] = action_stats.get("processed", 0)
            stats["action_memories_created"] = action_stats.get("memories_created", 0)
        except Exception:
            logger.exception("Action log processing failed")

        duration_ms = int((time.monotonic() - start) * 1000)
        self._storage.insert_consolidation_log(
            {
                **stats,
                "duration_ms": duration_ms,
            }
        )
        logger.info("Consolidation complete in %dms: %s", duration_ms, stats)

        # Post-consolidation MTREE health probe: bulk embedding writes during
        # consolidation are the primary trigger for SurrealDB 2.6.x index
        # corruption. Detect and auto-recover while the damage is still fresh.
        if not self._storage.probe_vector_indexes():
            logger.warning("MTREE index corruption detected after consolidation — rebuilding")
            if self._storage.rebuild_vector_indexes():
                logger.info("MTREE indexes rebuilt successfully")
            else:
                logger.critical(
                    "MTREE index rebuild failed; vector search will be degraded "
                    "until the container is restarted"
                )

        return stats

    @property
    def pool(self):
        """Access the AstrocytePool for domain-aware operations."""
        return self._pool

    @property
    def causal_discovery(self):
        """Access the CausalDiscovery engine."""
        return self._causal_discovery

    @property
    def cls(self):
        """Access the DualStoreCLS for episodic/semantic classification."""
        return self._cls

    def _run_domain_consolidation(self) -> list[dict]:
        """Run consolidation for each active astrocyte process domain."""
        results = []
        for proc_stat in self._pool.get_process_stats():
            name = proc_stat["name"]
            try:
                domain_result = self._pool.consolidate_domain(name)
                results.append(domain_result)
            except Exception:
                logger.exception("Domain consolidation failed for %s", name)
        return results

    # -- Thermodynamic decay --

    def _apply_decay(self, stats: dict) -> None:
        now = datetime.now(UTC)
        decay = self._settings.DECAY_FACTOR
        cold = self._settings.COLD_THRESHOLD
        action_stream_cold = self._settings.ACTION_STREAM_COLD_THRESHOLD

        for mem in self._storage.get_all_memories_for_decay():
            if mem.get("is_protected"):
                continue
            last = datetime.fromisoformat(mem["last_accessed"])
            hours = (now - last).total_seconds() / 3600.0
            new_heat = self._thermo.compute_decay(mem, hours)
            tags = mem.get("tags") or []
            if isinstance(tags, str):
                import json

                tags = json.loads(tags)
            effective_cold = action_stream_cold if "_action_stream" in tags else cold
            if new_heat < effective_cold:
                new_heat = 0.0
                stats["memories_archived"] += 1
            if abs(new_heat - mem["heat"]) > 1e-9:
                self._storage.update_memory_heat(mem["id"], new_heat)
                stats["memories_updated"] += 1

        for ent in self._storage.get_all_entities_for_decay():
            last = datetime.fromisoformat(ent["last_accessed"])
            hours = (now - last).total_seconds() / 3600.0
            new_heat = ent["heat"] * (decay**hours)
            if new_heat < cold:
                new_heat = 0.0
                self._storage.archive_entity(ent["id"])
            if abs(new_heat - ent["heat"]) > 1e-9:
                self._storage.update_entity_heat(ent["id"], new_heat)

    # -- Entity extraction and graph building --

    def _process_new_episodes(self, stats: dict) -> None:
        episodes = self._storage.get_episodes_since(self._last_consolidated_episode_id)
        for ep in episodes:
            # Use typed extraction for richer relationships
            typed_entities = self._graph.extract_entities_typed(
                ep["raw_content"], ep.get("directory", "")
            )
            # Fall back to legacy extraction for broad coverage
            legacy_entities = self._extract_entities(ep["raw_content"])

            # Merge: typed triples -> (name, type) pairs + relationship context
            entity_map: dict[str, str] = {}  # name -> type
            rel_contexts: dict[str, str] = {}  # name -> relationship context
            for name, etype, ctx in typed_entities:
                entity_map[name] = etype
                if ctx:
                    rel_contexts[name] = ctx
            for name, etype in legacy_entities:
                if name not in entity_map:
                    entity_map[name] = etype

            entity_ids = []
            entity_names = []
            for name, etype in entity_map.items():
                existing = self._storage.get_entity_by_name(name)
                if existing:
                    self._storage.reinforce_entity(existing["id"])
                    entity_ids.append(existing["id"])
                else:
                    eid = self._storage.insert_entity({"name": name, "type": etype})
                    entity_ids.append(eid)
                entity_names.append(name)

            # Build co-occurrence relationships
            for id_a, id_b in combinations(entity_ids, 2):
                rel = self._storage.get_relationship_between(id_a, id_b)
                if rel:
                    self._storage.reinforce_relationship(rel["id"])
                else:
                    self._storage.insert_relationship(
                        {
                            "source_entity_id": id_a,
                            "target_entity_id": id_b,
                            "relationship_type": "co_occurrence",
                        }
                    )

            # Build typed relationships from extraction context
            for name, ctx in rel_contexts.items():
                if ctx == "imports":
                    # Find the module this was imported from (nearest dependency)
                    for other_name, other_type in entity_map.items():
                        if other_type == "dependency" and other_name != name:
                            self._graph.add_relationship(name, other_name, "imports")
                            break
                elif ctx == "calls":
                    pass  # calls are implicit from co_occurrence for now
                elif ctx == "resolved_by":
                    for other_name, other_type in entity_map.items():
                        if other_type == "solution" and other_name != name:
                            self._graph.add_relationship(other_name, name, "resolved_by")
                            break
                elif ctx == "decided_to_use":
                    pass  # decision pairs handled by extract_entities_typed

            # Synaptic boost: if any associated memory has high importance,
            # boost nearby memories in the time window
            if ep.get("source_episode_id") is not None:
                source_mem = self._storage.get_memory(ep["source_episode_id"])
                if source_mem and source_mem.get("importance", 0.5) > 0.7:
                    self._thermo.synaptic_boost(source_mem["id"], source_mem["heat"])

            self._last_consolidated_episode_id = max(self._last_consolidated_episode_id, ep["id"])

    @staticmethod
    def _extract_entities(content: str) -> list[tuple[str, str]]:
        """Extract (name, type) pairs from raw episode content."""
        entities: list[tuple[str, str]] = []

        # File paths
        for m in _FILE_PATH_RE.finditer(content):
            path = m.group(0)
            if any(path.endswith(ext) for ext in _CODE_EXTENSIONS):
                entities.append((path, "file"))

        # Python def/class
        for m in _PYTHON_DEF_RE.finditer(content):
            entities.append((m.group(2), "function"))

        # JS function keyword
        for m in _JS_FUNCTION_RE.finditer(content):
            entities.append((m.group(1), "function"))

        # Error/Exception types
        for m in _ERROR_RE.finditer(content):
            entities.append((m.group(1), "error"))

        # Traceback header
        if _TRACEBACK_RE.search(content):
            entities.append(("Traceback", "error"))

        # Python imports
        for m in _IMPORT_RE.finditer(content):
            entities.append((m.group(1), "dependency"))
        for m in _FROM_IMPORT_RE.finditer(content):
            entities.append((m.group(1), "dependency"))

        # JS require
        for m in _REQUIRE_RE.finditer(content):
            entities.append((m.group(1), "dependency"))

        # Decisions
        for m in _DECISION_RE.finditer(content):
            entities.append((m.group(0).strip(), "decision"))

        # Deduplicate preserving order
        seen: set[tuple[str, str]] = set()
        unique: list[tuple[str, str]] = []
        for pair in entities:
            if pair not in seen:
                seen.add(pair)
                unique.append(pair)
        return unique

    # -- Semantic similarity linking --

    def _link_similar_memories(self, stats: dict) -> None:
        """Create memory_similarity_link records between semantically similar memories.

        Uses numpy matrix multiplication for fast pairwise cosine similarity,
        then upserts into memory_similarity_link (no entity-table rows created).
        Capped per cycle to keep consolidation fast.
        """
        import numpy as np

        memories = self._storage.get_all_memories_with_embeddings()
        if len(memories) < 2:
            return

        threshold = 0.78
        max_new_links = 100  # cap per consolidation cycle

        # Build embedding matrix (only memories with valid embeddings)
        valid = []
        for m in memories:
            emb = m.get("embedding")
            if emb and len(emb) > 0:
                try:
                    arr = np.frombuffer(emb, dtype=np.float32)
                    if len(arr) > 0 and np.linalg.norm(arr) > 0:
                        valid.append((m["id"], arr / np.linalg.norm(arr)))
                except Exception:
                    continue

        if len(valid) < 2:
            return

        ids = [v[0] for v in valid]
        matrix = np.stack([v[1] for v in valid])  # N x D

        # Pairwise cosine similarity via matrix multiplication (fast)
        sim_matrix = matrix @ matrix.T  # N x N

        # Pre-load all existing links to avoid per-pair read roundtrips.
        # Key is canonical (source_memory_id, target_memory_id) — already stored as min/max.
        existing_links: dict[tuple[int, int], dict] = {}
        for link in self._storage.get_all_memory_similarity_links():
            key = (link["source_memory_id"], link["target_memory_id"])
            existing_links[key] = link

        # Find pairs above threshold (upper triangle only)
        # Get indices sorted by descending similarity for best-first linking
        links_created = 0
        rows, cols = np.where(np.triu(sim_matrix, k=1) >= threshold)
        sims = sim_matrix[rows, cols]
        order = np.argsort(-sims)

        pending_inserts: list[tuple[int, int, float]] = []  # (mid_a, mid_b, weight)
        pending_reinforces: list[tuple[int, float]] = []  # (link_id, delta)

        for idx in order:
            if links_created >= max_new_links:
                break
            i, j = int(rows[idx]), int(cols[idx])
            sim = float(sims[idx])
            mid_a, mid_b = ids[i], ids[j]

            # Canonical key matches storage convention (source < target)
            key = (mid_a, mid_b) if mid_a < mid_b else (mid_b, mid_a)
            existing = existing_links.get(key)
            if existing:
                # Reinforce if new similarity is higher
                if sim > existing.get("weight", 0):
                    pending_reinforces.append((existing["id"], sim - existing["weight"]))
                continue

            pending_inserts.append((mid_a, mid_b, round(sim, 4)))
            links_created += 1

        # Batch all writes into a single transaction
        now = self._storage._now_iso()
        batch: list[tuple[str, dict | None]] = []

        for mid_a, mid_b, weight in pending_inserts:
            src, tgt = (mid_a, mid_b) if mid_a < mid_b else (mid_b, mid_a)
            lid = self._storage._next_id("memory_similarity_link")
            batch.append(
                (
                    "CREATE type::record('memory_similarity_link', $id) SET "
                    "source_memory_id = $src, target_memory_id = $tgt, "
                    "weight = $weight, created_at = $created_at, updated_at = $updated_at",
                    {
                        "id": lid,
                        "src": src,
                        "tgt": tgt,
                        "weight": weight,
                        "created_at": now,
                        "updated_at": now,
                    },
                )
            )

        for link_id, delta in pending_reinforces:
            batch.append(
                (
                    "UPDATE type::record('memory_similarity_link', $id) SET "
                    "weight = weight + $delta, updated_at = $now",
                    {"id": link_id, "delta": delta, "now": now},
                )
            )

        self._storage.batch_writes(batch)

        stats["similarity_links_created"] = links_created

    # -- Duplicate merging --

    def _merge_duplicates(self, stats: dict) -> None:
        memories = self._storage.get_all_memories_with_embeddings()
        if len(memories) < 2:
            return

        to_delete: set[int] = set()
        for i, mem_a in enumerate(memories):
            if mem_a["id"] in to_delete:
                continue
            for mem_b in memories[i + 1 :]:
                if mem_b["id"] in to_delete:
                    continue
                # Identical content is always a duplicate (catches prefix-embedding mismatch)
                content_a = mem_a.get("content") or ""
                content_b = mem_b.get("content") or ""
                is_duplicate = content_a and content_a == content_b
                if not is_duplicate:
                    if mem_a["embedding"] is None or mem_b["embedding"] is None:
                        continue
                    sim = self._embeddings.similarity(mem_a["embedding"], mem_b["embedding"])
                    is_duplicate = sim > 0.95
                if is_duplicate:
                    victim = mem_b["id"] if mem_a["heat"] >= mem_b["heat"] else mem_a["id"]
                    to_delete.add(victim)

        for mid in to_delete:
            self._storage.delete_memory(mid)
            stats["memories_deleted"] += 1

    # -- Action log processing --

    def _process_action_log(self) -> dict:
        """Process unprocessed action_log entries into summarized memories.

        Groups actions by directory + 30-minute time windows, then creates
        a summary memory for each group. This is the cold path — the hot
        path (PostToolCall hook) just writes to action_log.
        """
        from datetime import datetime

        stats = {"processed": 0, "memories_created": 0}

        try:
            rows = self._storage.get_unprocessed_actions(limit=200)
        except Exception:
            return stats

        if not rows:
            return stats

        # Group by directory + 30-min windows
        groups: dict[str, list] = {}
        for row in rows:
            directory = row.get("directory") or "unknown"
            timestamp = row.get("timestamp", "")
            # Create a window key: directory + 30-min bucket
            try:
                dt = datetime.fromisoformat(timestamp)
                bucket = dt.strftime("%Y-%m-%d-%H") + f"-{dt.minute // 30}"
            except (ValueError, TypeError):
                bucket = "unknown"
            key = f"{directory}|{bucket}"
            if key not in groups:
                groups[key] = []
            groups[key].append(
                {
                    "id": row.get("id"),
                    "tool": row.get("tool_name", ""),
                    "summary": row.get("tool_input_summary", ""),
                    "directory": directory,
                }
            )

        # Create a summary memory for each group with 3+ actions
        for _key, actions in groups.items():
            directory = actions[0]["directory"]

            # Build action summary
            tool_counts: dict[str, int] = {}
            details = []
            for a in actions:
                tool_counts[a["tool"]] = tool_counts.get(a["tool"], 0) + 1
                if a["summary"] and len(details) < 5:
                    details.append(f"{a['tool']}: {a['summary'][:80]}")

            if len(actions) >= 3:
                tools_str = ", ".join(
                    f"{t}({c})" for t, c in sorted(tool_counts.items(), key=lambda x: -x[1])
                )
                content = f"Session activity [{tools_str}]: {len(actions)} tool calls"
                if details:
                    content += "\n" + "\n".join(f"- {d}" for d in details)

                # Store as a low-heat episodic memory (will be consolidated normally)
                embedding = self._embeddings.encode(content)
                self._storage.insert_memory(
                    {
                        "content": content,
                        "embedding": embedding,
                        "tags": ["_action_stream", "_auto"],
                        "directory_context": directory,
                        "heat": 0.4,
                        "confidence": 0.0,
                        "is_stale": False,
                        "file_hash": None,
                        "embedding_model": self._embeddings.get_model_name(),
                    }
                )
                stats["memories_created"] += 1

            # Mark all as processed
            ids = [a["id"] for a in actions]
            self._storage.mark_actions_processed(ids)
            stats["processed"] += len(actions)

        return stats
