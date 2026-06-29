"""Astrocyte consolidation engine — processes memories on explicit invocation.

v5.7.0 PR-0: background daemon (periodic auto-consolidation) removed.
Consolidation runs via force_consolidate() (MCP consolidate_now) or the
nightly cron (PR-1). record_activity() is kept as a no-op for HTTP/MCP
callers that still call it.
"""

import logging
from datetime import UTC, datetime

from yadgar.cls_store import DualStoreCLS
from yadgar.config import Settings
from yadgar.consolidation.causal import _CausalMixin
from yadgar.consolidation.cleanup import _CleanupMixin
from yadgar.consolidation.cls import _CLSMixin
from yadgar.consolidation.heat_decay import _HeatDecayMixin
from yadgar.consolidation.orchestrator import _OrchestratorMixin
from yadgar.curation import MemoryCurator
from yadgar.embeddings import EmbeddingEngine
from yadgar.knowledge_graph import KnowledgeGraph
from yadgar.ops import _fire_vacuum_service
from yadgar.sleep_compute import SleepComputeEngine
from yadgar.storage import StorageEngine
from yadgar.thermodynamics import MemoryThermodynamics

# Lazy imports to avoid circular dependencies
_AstrocytePool = None
_CausalDiscovery = None


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


logger = logging.getLogger("yadgar.consolidation")


class ConsolidationScheduler(
    _HeatDecayMixin,
    _CLSMixin,
    _CausalMixin,
    _CleanupMixin,
    _OrchestratorMixin,
):
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
        self._last_consolidation_date = None
        self._last_cycle_completed_at: datetime = datetime.fromtimestamp(0, UTC)

        # last_activity / is_running kept as inert attributes: HTTP/MCP handlers
        # still call record_activity(); test_vacuum_auto_trigger sets is_running.
        # The daemon thread and stop_event are gone (v5.7.0 PR-0).
        self.last_activity: datetime = datetime.now(UTC)
        self.is_running: bool = False
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
        if not getattr(settings, "ASTROCYTE_POOL_ENABLED", True):
            logger.warning(
                "AstrocytePool is DISABLED (ASTROCYTE_POOL_ENABLED=False). "
                "Domain-aware consolidation will not run."
            )
        else:
            try:
                PoolCls = _get_pool_class()
                self._pool = PoolCls(storage, embeddings, self._graph, self._thermo, settings)
                self._pool.init_processes()
            except Exception:
                logger.exception("Failed to initialize AstrocytePool")

        # v4.9: vacuum auto-trigger cooldown timestamp (in-memory; resets on restart)
        self._last_vacuum_at: datetime | None = None

    # -- Public API --

    def record_activity(self) -> None:
        """No-op kept for HTTP/MCP callers (v5.7.0 PR-0: daemon removed)."""
        self.last_activity = datetime.now(UTC)

    def force_consolidate(self) -> dict:
        """Run a consolidation cycle immediately. Returns the cycle stats."""
        return self._consolidation_cycle()

    def run_nightly_consolidation(self) -> dict:
        """Nightly entrypoint: a full consolidation cycle followed by a gated sleep cycle.

        This is the cron-only path (yadgar/scripts/nightly_cycle.py). It runs the
        regular consolidation cycle, then invokes ``_maybe_sleep_cycle()`` so the
        dream/community/cluster/reembed/compress/narrate phases run at most once
        every 6 hours (v5.7.0 PR-1 wiring — the sleep cycle had been dead since
        the daemon loop was removed; #37).

        The interactive MCP path (consolidate_now) deliberately does NOT call
        this — ``light`` stays fast and ``full`` triggers its own sleep cycle —
        so the gated sleep runs only on the nightly cron, avoiding a double-fire.
        """
        stats = self._consolidation_cycle()
        sleep_stats = self._maybe_sleep_cycle()
        # v5.86 (OT-C4): mandatory full similarity-link reconcile after sleep when
        # re-embedding mutated old↔old similarity, or on the weekly cadence. Inert
        # unless SIMILARITY_LINKING_INCREMENTAL_ENABLED (default OFF).
        self._maybe_full_reconcile(sleep_stats)
        # v5.88: precompute + cache the 3D graph layout. Nightly-only (never the
        # light consolidate_now path) + signature-gated. Inert unless
        # VIZ_PRECOMPUTED_LAYOUT_ENABLED (default OFF).
        self._maybe_precompute_graph_layout()
        return stats

    # -- Properties --

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

    # -- Auto-vacuum (kept here so tests can patch module-level _now_local / _subprocess) --

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
