"""Astrocyte consolidation engine — background daemon that processes memories during idle time."""

import logging
import re
import subprocess as _subprocess
import threading
import time
from datetime import UTC, date, datetime
from itertools import combinations

from yadgar.cls_store import DualStoreCLS
from yadgar.config import Settings
from yadgar.curation import MemoryCurator
from yadgar.embeddings import EmbeddingEngine
from yadgar.knowledge_graph import KnowledgeGraph
from yadgar.ops import _fire_vacuum_service
from yadgar.sleep_compute import SleepComputeEngine
from yadgar.storage import StorageEngine
from yadgar.thermodynamics import MemoryThermodynamics

# Lazy imports to avoid circular dependencies
_AstrocytePool = None

# ---------------------------------------------------------------------------
# Vacuum auto-trigger helpers (v4.9)
# ---------------------------------------------------------------------------


def _now_local() -> datetime:
    """Return current local time as a naive datetime. Overridable in tests."""
    return datetime.now()


def _in_window(now: datetime, window_start: str, window_end: str) -> bool:
    """Return True if *now* (naive local datetime) falls within [start, end).

    Supports cross-midnight windows (e.g. start=23:00, end=02:00).
    Equal start and end is treated as a zero-length window → always False.

    Args:
        now: Current local time (naive datetime, from _now_local()).
        window_start: HH:MM string, inclusive start.
        window_end: HH:MM string, exclusive end.
    """
    sh, sm = (int(x) for x in window_start.split(":"))
    eh, em = (int(x) for x in window_end.split(":"))
    start_m = sh * 60 + sm
    end_m = eh * 60 + em
    now_m = now.hour * 60 + now.minute
    if start_m == end_m:
        return False
    if start_m < end_m:
        return start_m <= now_m < end_m
    # Cross-midnight: e.g. start=23:00 (1380), end=02:00 (120)
    return now_m >= start_m or now_m < end_m


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
        self._last_cycle_completed_at: datetime = datetime.fromtimestamp(0, UTC)

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

        # v4.9: vacuum auto-trigger cooldown timestamp (in-memory; resets on restart)
        self._last_vacuum_at: datetime | None = None

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
        """Run a consolidation cycle immediately. Returns the cycle stats.

        Ignores CONSOLIDATION_COOLDOWN_SECONDS — an explicit user/MCP request
        beats throttling.
        """
        return self._consolidation_cycle()

    # -- Daemon loop --

    def _daemon_loop(self) -> None:
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=self._settings.DAEMON_CHECK_INTERVAL)
            if self._stop_event.is_set():
                break
            now = datetime.now(UTC)
            today = now.date()

            # Cooldown gate for idle-triggered cycles. last_activity is only reset
            # by external API hits, so after a cycle completes idle_seconds is still
            # >= IDLE_THRESHOLD_SECONDS and the next wake-up would immediately fire
            # again. _last_cycle_completed_at prevents that.
            cooldown = self._settings.CONSOLIDATION_COOLDOWN_SECONDS
            since_last_cycle = (now - self._last_cycle_completed_at).total_seconds()
            if cooldown > 0 and since_last_cycle < cooldown:
                # Still within cooldown window — skip idle check, fall through to
                # the daily 18:30 UTC block which is time-gated independently.
                pass
            else:
                # Idle-triggered consolidation: process new episodes when system is idle
                idle_seconds = (now - self.last_activity).total_seconds()
                if idle_seconds >= self._settings.IDLE_THRESHOLD_SECONDS:
                    new_episodes = self._storage.get_episodes_since(
                        self._last_consolidated_episode_id
                    )
                    if new_episodes:
                        try:
                            self._consolidation_cycle()
                        except Exception:
                            logger.exception("Idle consolidation cycle failed")
                        finally:
                            self._last_cycle_completed_at = datetime.now(UTC)

            # Run once per day at 18:30 UTC (18:30–18:31 window)
            if now.hour == 18 and now.minute == 30 and self._last_consolidation_date != today:
                try:
                    self._consolidation_cycle()
                    self._last_consolidation_date = today
                except Exception:
                    logger.exception("Consolidation cycle failed")
                finally:
                    self._last_cycle_completed_at = datetime.now(UTC)
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

    def _maybe_auto_vacuum(self) -> None:
        """v4.9: Fire yadgar-vacuum.service if DB is over threshold and in window.

        Cooldown: 6 hours since last auto-fire (in-memory; resets on restart).
        """
        settings = self._settings
        threshold = settings.VACUUM_AUTO_THRESHOLD_BYTES

        # Cooldown check (6-hour hard-coded per plan)
        _COOLDOWN_HOURS = 6.0
        if self._last_vacuum_at is not None:
            hours_since = (datetime.now(UTC) - self._last_vacuum_at).total_seconds() / 3600.0
            if hours_since < _COOLDOWN_HOURS:
                return

        db_size_info = self._storage.get_db_size()
        size = db_size_info.get("db_size_bytes", 0)

        if size <= threshold:
            return  # Below threshold — nothing to do

        # Over threshold — check if we're in the configured window
        now_local = _now_local()
        if _in_window(
            now_local, settings.VACUUM_AUTO_WINDOW_START, settings.VACUUM_AUTO_WINDOW_END
        ):
            # Pre-check: skip if yadgar-vacuum.service is already active/activating
            try:
                out = _subprocess.check_output(
                    ["systemctl", "--user", "is-active", "yadgar-vacuum.service"],
                    stderr=_subprocess.DEVNULL,
                )
                state = out.decode(errors="replace").strip()
                if state in ("active", "activating"):
                    logger.debug("Auto-vacuum skipped: yadgar-vacuum.service is %s", state)
                    return  # Do NOT update _last_vacuum_at — retry next cycle
            except FileNotFoundError:
                logger.debug("Auto-vacuum skipped: systemctl not available")
                return  # Do NOT update _last_vacuum_at
            except _subprocess.CalledProcessError:
                pass  # is-active returns non-zero for inactive/failed — proceed

            _fire_vacuum_service()
            self._last_vacuum_at = datetime.now(UTC)
            logger.warning(
                "Auto-vacuum triggered: db=%d MiB > %d MiB threshold",
                size >> 20,
                threshold >> 20,
            )
        else:
            logger.warning(
                "DB over auto-vacuum threshold (%d MiB) but outside window (%s–%s); deferred",
                size >> 20,
                settings.VACUUM_AUTO_WINDOW_START,
                settings.VACUUM_AUTO_WINDOW_END,
            )

    # -- Core consolidation --

    def _consolidation_cycle(self) -> dict:
        start = time.monotonic()
        stats = {
            "memories_added": 0,
            "memories_updated": 0,
            "memories_archived": 0,
            "memories_deleted": 0,
        }

        _t = time.monotonic()
        logger.info("phase: apply_decay starting")
        self._apply_decay(stats)
        logger.info("phase: apply_decay complete in %dms", int((time.monotonic() - _t) * 1000))

        _t = time.monotonic()
        logger.info("phase: process_episodes starting")
        self._process_new_episodes(stats)
        logger.info("phase: process_episodes complete in %dms", int((time.monotonic() - _t) * 1000))

        # Prune old episodes to keep the table bounded and _check_temporal_order fast
        try:
            retention = self._settings.EPISODE_RETENTION_DAYS
            pruned = self._storage.prune_old_episodes(older_than_days=retention)
            if pruned:
                logger.info("phase: pruned %d old episodes (retention=%dd)", pruned, retention)
        except Exception:
            logger.debug("Episode prune failed (non-fatal)", exc_info=True)

        _t = time.monotonic()
        logger.info("phase: merge_duplicates starting")
        self._merge_duplicates(stats)
        logger.info("phase: merge_duplicates complete in %dms", int((time.monotonic() - _t) * 1000))

        # Semantic similarity linking — create relationships between similar memories
        try:
            _t = time.monotonic()
            logger.info("phase: link_similar starting")
            self._link_similar_memories(stats)
            logger.info("phase: link_similar complete in %dms", int((time.monotonic() - _t) * 1000))
        except Exception:
            logger.exception("Similarity linking failed")

        try:
            _t = time.monotonic()
            logger.info("phase: detect_causality starting")
            self._graph.detect_causality()
            logger.info(
                "phase: detect_causality complete in %dms", int((time.monotonic() - _t) * 1000)
            )
        except Exception:
            logger.exception("Causal detection failed")

        # Run formal causal discovery (PC algorithm) periodically
        if self._causal_discovery is not None:
            self._events_since_last_discovery += stats.get("memories_added", 0)
            if self._events_since_last_discovery >= 50:
                try:
                    _t = time.monotonic()
                    logger.info("phase: causal_discovery starting")
                    dag = self._causal_discovery.discover_dag()
                    stats["causal_dag_edges"] = dag.get("metadata", {}).get("directed_count", 0)
                    self._events_since_last_discovery = 0
                    logger.info(
                        "phase: causal_discovery complete in %dms",
                        int((time.monotonic() - _t) * 1000),
                    )
                except Exception:
                    logger.exception("Causal discovery failed")

        # Run memify self-improvement cycle
        try:
            _t = time.monotonic()
            logger.info("phase: memify starting")
            memify_stats = self._curator.memify_cycle()
            stats["memify_pruned"] = memify_stats.get("pruned", 0)
            stats["memify_strengthened"] = memify_stats.get("strengthened", 0)
            stats["memify_reweighted"] = memify_stats.get("reweighted", 0)
            stats["memify_derived"] = memify_stats.get("derived", 0)
            logger.info("phase: memify complete in %dms", int((time.monotonic() - _t) * 1000))
        except Exception:
            logger.exception("Memify cycle failed")

        # Run CLS dual-store consolidation (Go-CLS: episodic → semantic)
        try:
            _t = time.monotonic()
            logger.info("phase: cls_consolidation starting")
            cls_stats = self._cls.consolidation_cycle()
            stats["cls_patterns_found"] = cls_stats.get("patterns_found", 0)
            stats["cls_promoted"] = cls_stats.get("promoted", 0)
            stats["cls_skipped_inconsistent"] = cls_stats.get("skipped_inconsistent", 0)
            logger.info(
                "phase: cls_consolidation complete in %dms", int((time.monotonic() - _t) * 1000)
            )
        except Exception:
            logger.exception("CLS consolidation cycle failed")

        # Compression disabled: memory content must stay intact for LLM usage.

        # Process action_log entries into real memories
        try:
            _t = time.monotonic()
            logger.info("phase: action_log starting")
            action_stats = self._process_action_log()
            stats["actions_processed"] = action_stats.get("processed", 0)
            stats["action_memories_created"] = action_stats.get("memories_created", 0)
            logger.info("phase: action_log complete in %dms", int((time.monotonic() - _t) * 1000))
        except Exception:
            logger.exception("Action log processing failed")

        # Table retention prunes — each is non-fatal; logged at debug on error.
        _retention_tasks = [
            ("narrative_entry", self._settings.NARRATIVE_ENTRY_RETENTION_DAYS, "created_at", None),
            (
                "astrocyte_process",
                self._settings.ASTROCYTE_PROCESS_RETENTION_DAYS,
                "created_at",
                None,
            ),
            ("memory_cluster", self._settings.MEMORY_CLUSTER_RETENTION_DAYS, "created_at", None),
            ("derived_belief", self._settings.DERIVED_BELIEF_RETENTION_DAYS, "created_at", None),
            (
                "prospective_memory",
                self._settings.PROSPECTIVE_MEMORY_RETENTION_DAYS,
                "created_at",
                "is_active = false",  # never delete pending reminders
            ),
        ]
        for _table, _days, _field, _extra in _retention_tasks:
            try:
                pruned = self._storage.prune_old_rows(
                    _table, _days, age_field=_field, extra_where=_extra
                )
                if pruned:
                    logger.info("retention: pruned %d old rows from %s", pruned, _table)
            except Exception:
                logger.debug("retention prune failed for %s (non-fatal)", _table, exc_info=True)

        # Run invariant checks — non-fatal; violations are logged CRITICAL
        try:
            from yadgar.server import _run_check_invariants

            inv = _run_check_invariants(self._storage)
            if not inv["ok"]:
                logger.critical(
                    "check_invariants: %d violation(s) detected after consolidation: %s",
                    len(inv["violations"]),
                    inv["violations"],
                )
        except Exception:
            logger.debug("check_invariants failed (non-fatal)", exc_info=True)

        # v4.9: threshold auto-trigger vacuum — non-fatal end-of-cycle check
        if self._settings.VACUUM_AUTO_ENABLED:
            try:
                self._maybe_auto_vacuum()
            except Exception:
                logger.debug("auto-vacuum check failed (non-fatal)", exc_info=True)

        _t = time.monotonic()
        logger.info("phase: insert_consolidation_log starting")
        duration_ms = int((time.monotonic() - start) * 1000)
        self._storage.insert_consolidation_log(
            {
                **stats,
                "duration_ms": duration_ms,
            }
        )
        logger.info(
            "phase: insert_consolidation_log complete in %dms", int((time.monotonic() - _t) * 1000)
        )
        logger.info("Consolidation complete in %dms: %s", duration_ms, stats)

        # Post-consolidation MTREE health probe: bulk embedding writes during
        # consolidation are the primary trigger for SurrealDB 2.6.x index
        # corruption. Detect and auto-recover while the damage is still fresh.
        logger.info("phase: mtree_probe starting")
        _t = time.monotonic()
        if not self._storage.probe_vector_indexes():
            logger.warning("MTREE index corruption detected after consolidation — rebuilding")
            if self._storage.rebuild_vector_indexes():
                logger.info("MTREE indexes rebuilt successfully")
            else:
                logger.critical(
                    "MTREE index rebuild failed; vector search will be degraded "
                    "until the container is restarted"
                )
        logger.info("phase: mtree_probe complete in %dms", int((time.monotonic() - _t) * 1000))

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

        mem_batch: list[tuple[str, dict | None]] = []
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
                mem_batch.append(
                    (
                        "UPDATE type::record('memory', $id) SET heat = $heat",
                        {"id": mem["id"], "heat": new_heat},
                    )
                )
                stats["memories_updated"] += 1

        if mem_batch:
            self._storage.batch_writes(mem_batch)

        ent_batch: list[tuple[str, dict | None]] = []
        for ent in self._storage.get_all_entities_for_decay():
            last = datetime.fromisoformat(ent["last_accessed"])
            hours = (now - last).total_seconds() / 3600.0
            new_heat = ent["heat"] * (decay**hours)
            goes_cold = new_heat < cold
            if goes_cold:
                new_heat = 0.0
            if abs(new_heat - ent["heat"]) > 1e-9 or goes_cold:
                set_clause = "heat = $heat"
                if goes_cold:
                    set_clause += ", archived = true"
                ent_batch.append(
                    (
                        f"UPDATE type::record('entity', $id) SET {set_clause}",
                        {"id": ent["id"], "heat": new_heat},
                    )
                )

        if ent_batch:
            self._storage.batch_writes(ent_batch)

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

            # Build co-occurrence relationships — ONE bulk fetch + batched writes
            # instead of O(N²) per-pair HTTP calls.
            existing_rels = self._storage.get_relationships_among_entities(entity_ids)
            rel_index: dict[tuple[int, int], dict] = {
                (
                    min(r["source_entity_id"], r["target_entity_id"]),
                    max(r["source_entity_id"], r["target_entity_id"]),
                ): r
                for r in existing_rels
            }
            to_reinforce: list[int] = []
            to_insert: list[tuple[int, int]] = []
            for id_a, id_b in combinations(entity_ids, 2):
                key = (min(id_a, id_b), max(id_a, id_b))
                rel = rel_index.get(key)
                if rel:
                    to_reinforce.append(rel["id"])
                else:
                    to_insert.append((id_a, id_b))

            now = self._storage._now_iso()
            batch: list[tuple[str, dict | None]] = []

            if to_reinforce:
                for rid in to_reinforce:
                    batch.append(
                        (
                            "UPDATE type::record('relationship', $id) SET "
                            "weight = weight + $inc, last_reinforced = $now",
                            {"id": rid, "inc": 1.0, "now": now},
                        )
                    )

            if to_insert:
                new_ids = self._storage._reserve_ids("relationship", len(to_insert))
                for (id_a, id_b), rid in zip(to_insert, new_ids, strict=True):
                    batch.append(
                        (
                            "CREATE type::record('relationship', $id) SET "
                            "source_entity_id = $src, target_entity_id = $tgt, "
                            "relationship_type = 'co_occurrence', weight = 1.0, "
                            "created_at = $now, last_reinforced = $now",
                            {"id": rid, "src": id_a, "tgt": id_b, "now": now},
                        )
                    )

            if batch:
                self._storage.batch_writes(batch)

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

        max_candidates = self._settings.SIMILARITY_MATRIX_MAX_CANDIDATES
        memories = self._storage.get_memories_with_embeddings(
            limit=max_candidates, order_by="last_accessed"
        )
        if len(memories) < 2:
            return

        threshold = self._settings.SIMILARITY_LINK_THRESHOLD
        max_new_links = 100  # cap per consolidation cycle
        max_degree = self._settings.MAX_SIMILARITY_LINKS_PER_MEMORY

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
        degree: dict[int, int] = {}  # memory_id -> current link count
        for link in self._storage.get_all_memory_similarity_links():
            src_id, tgt_id = link["source_memory_id"], link["target_memory_id"]
            existing_links[(src_id, tgt_id)] = link
            degree[src_id] = degree.get(src_id, 0) + 1
            degree[tgt_id] = degree.get(tgt_id, 0) + 1

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

            # Degree cap — keep the similarity graph sparse. Pairs are processed
            # in descending-similarity order, so a capped-out memory has already
            # kept its strongest links.
            if degree.get(mid_a, 0) >= max_degree or degree.get(mid_b, 0) >= max_degree:
                continue

            pending_inserts.append((mid_a, mid_b, round(sim, 4)))
            degree[mid_a] = degree.get(mid_a, 0) + 1
            degree[mid_b] = degree.get(mid_b, 0) + 1
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
        """Delete near-duplicate memories (cosine similarity > 0.95), keeping the hotter one.

        Uses numpy matrix multiplication for O(N·D) pairwise cosine similarity
        (same approach as _link_similar_memories) instead of O(N²) per-pair calls
        to EmbeddingEngine.similarity().

        Two-pass approach:
        1. Exact-content match pre-pass — catches duplicates even when one embedding
           is missing/corrupt (preserves existing short-circuit semantics).
        2. Embedding-similarity pass — numpy matmul over valid-embedding subset.
        """
        import numpy as np

        max_candidates = self._settings.SIMILARITY_MATRIX_MAX_CANDIDATES
        memories = self._storage.get_memories_with_embeddings(
            limit=max_candidates, order_by="last_accessed"
        )
        if len(memories) < 2:
            return

        to_delete: set[int] = set()

        # Pass 1: exact-content match (cheap, handles missing embeddings)
        content_index: dict[str, int] = {}  # content → first-seen memory id
        content_heat: dict[str, float] = {}  # content → heat of winner
        for mem in memories:
            content = mem.get("content") or ""
            if not content:
                continue
            if content in content_index:
                existing_id = content_index[content]
                existing_heat = content_heat[content]
                if mem["heat"] > existing_heat:
                    # New one is hotter — evict the old winner
                    to_delete.add(existing_id)
                    content_index[content] = mem["id"]
                    content_heat[content] = mem["heat"]
                else:
                    to_delete.add(mem["id"])
            else:
                content_index[content] = mem["id"]
                content_heat[content] = mem["heat"]

        # Pass 2: embedding-similarity pass via numpy matmul
        # Only consider memories not already marked for deletion and with valid embeddings.
        valid: list[tuple[int, np.ndarray, float]] = []  # (id, unit_vec, heat)
        for mem in memories:
            if mem["id"] in to_delete:
                continue
            emb = mem.get("embedding")
            if not emb or len(emb) == 0:
                continue
            try:
                arr = np.frombuffer(emb, dtype=np.float32)
                norm = np.linalg.norm(arr)
                if len(arr) == 0 or norm == 0:
                    continue
                valid.append((mem["id"], arr / norm, mem["heat"]))
            except Exception:
                continue

        if len(valid) >= 2:
            ids = [v[0] for v in valid]
            heats = [v[2] for v in valid]
            matrix = np.stack([v[1] for v in valid])  # N x D

            # Pairwise cosine similarity via matrix multiplication (fast, O(N·D))
            sim_matrix = matrix @ matrix.T  # N x N

            # Find pairs strictly above 0.95 in upper triangle (same semantics as legacy > 0.95)
            rows, cols = np.where(np.triu(sim_matrix, k=1) > 0.95)

            for i, j in zip(rows.tolist(), cols.tolist(), strict=False):
                mid_a, mid_b = ids[i], ids[j]
                if mid_a in to_delete or mid_b in to_delete:
                    continue
                # Keep higher-heat memory; on tie, keep the one with lower index (stable)
                heat_a, heat_b = heats[i], heats[j]
                victim = mid_b if heat_a >= heat_b else mid_a
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

        # Prune old processed rows so action_log doesn't grow without bound.
        try:
            retention = self._settings.ACTION_LOG_RETENTION_DAYS
            self._storage.prune_processed_action_log(older_than_days=retention)
        except Exception:
            logger.debug("action_log prune failed (non-fatal)", exc_info=True)

        return stats
