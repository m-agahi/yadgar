"""Astrocyte consolidation engine — processes memories on explicit invocation.

v5.7.0 PR-0: background daemon (periodic auto-consolidation) removed.
Consolidation runs via force_consolidate() (MCP consolidate_now) or the
nightly cron (PR-1). record_activity() is kept as a no-op for HTTP/MCP
callers that still call it.
"""

import logging
from datetime import UTC, datetime

from yadgar._shared.config import Settings
from yadgar._shared.embeddings import EmbeddingEngine
from yadgar._shared.knowledge_graph import KnowledgeGraph
from yadgar._shared.observability.observe import observe
from yadgar._shared.storage import StorageEngine
from yadgar._shared.thermodynamics import MemoryThermodynamics
from yadgar.backend.cls_store import DualStoreCLS
from yadgar.backend.consolidation.causal import _CausalMixin
from yadgar.backend.consolidation.cleanup import _CleanupMixin
from yadgar.backend.consolidation.cls import _CLSMixin
from yadgar.backend.consolidation.heat_decay import _HeatDecayMixin
from yadgar.backend.consolidation.orchestrator import _OrchestratorMixin
from yadgar.backend.curation import MemoryCurator
from yadgar.backend.sleep_compute import SleepComputeEngine

# Lazy imports to avoid circular dependencies
_AstrocytePool = None
_CausalDiscovery = None

# R3 Car 1 D1: _now_local / _in_window moved with the auto-vacuum trigger to
# yadgar.core.consolidation.orchestrator (host lifecycle, core-owned).


@observe(tier="hot", metric="consolidation.get_pool_class")
def _get_pool_class():
    global _AstrocytePool
    if _AstrocytePool is None:
        from yadgar._shared.astrocyte_pool import AstrocytePool

        _AstrocytePool = AstrocytePool
    return _AstrocytePool


@observe(tier="hot", metric="consolidation.get_causal_discovery_class")
def _get_causal_discovery_class():
    global _CausalDiscovery
    if _CausalDiscovery is None:
        from yadgar.backend.causal_discovery import CausalDiscovery

        _CausalDiscovery = CausalDiscovery
    return _CausalDiscovery


logger = logging.getLogger("yadgar.consolidation")

# Sentinel for the `pool` kwarg: distinguishes "not supplied" (build own — bare
# callers, backward-compat) from "supplied, possibly None" (composition root
# injects the standalone pool, or None when disabled/failed upstream). A plain
# None default would rebuild in the disabled case → double warning + identity
# divergence between _st._pool and _st._consolidation.pool (R2a Car A).
_POOL_UNSET = object()


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
        pool=_POOL_UNSET,
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

        # Initialize astrocyte pool for domain-aware consolidation.
        # R2a Car A: when a `pool` is injected (composition root builds it
        # standalone so the backend SLIM path can populate _st._pool without
        # importing this module), USE it verbatim — do NOT rebuild or re-run
        # init_processes(). The injected value may be a real AstrocytePool OR
        # None (disabled/failed upstream); either way we adopt it exactly so
        # _st._pool and _st._consolidation.pool stay the same object.
        # When `pool` is the sentinel (bare callers — tests, backward-compat),
        # build our own exactly as before.
        if pool is not _POOL_UNSET:
            self._pool = pool
        else:
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

        # R3 Car 1 D1: the v4.9 auto-vacuum cooldown timestamp (_last_vacuum_at)
        # moved with the trigger to the core orchestrator (module-level global).

    # -- Public API --

    def record_activity(self) -> None:
        """No-op kept for HTTP/MCP callers (v5.7.0 PR-0: daemon removed)."""
        self.last_activity = datetime.now(UTC)

    def force_consolidate(self) -> dict:
        """Run a consolidation cycle immediately. Returns the cycle stats."""
        return self._consolidation_cycle()

    @observe(tier="boundary", metric="consolidation.run_full")
    def run_full_consolidation(self) -> dict:
        """consolidate_now(mode='full') compute: a cycle + a FORCED sleep cycle.

        Runs the regular cycle then unconditionally runs the sleep cycle (bypasses
        the 6-hour gate — the manual full trigger is explicit intent), updating the
        gate timestamp so the nightly cron does not double-fire. The graph-layout
        precompute + anchor-audit tail are CORE-side (run by the core orchestrator
        around this forwarded compute — R3 Car 1 D3).
        """
        stats = self._consolidation_cycle()
        try:
            sleep_stats = self._sleep_engine.run_sleep_cycle()
            stats["sleep_cycle"] = sleep_stats
            self._last_sleep_cycle = datetime.now(UTC)
        except Exception:
            logger.exception("Sleep cycle failed during run_full_consolidation")
        return stats

    @observe(tier="boundary", metric="consolidation.run_nightly")
    def run_nightly_consolidation(self) -> dict:
        """Nightly compute: a consolidation cycle followed by a GATED sleep cycle.

        This is the cron path (forwarded from the core nightly orchestrator). It
        runs the regular cycle, then invokes ``_maybe_sleep_cycle()`` so the
        dream/community/cluster/reembed/compress/narrate phases run at most once
        every 6 hours (v5.7.0 PR-1 wiring; #37). The gate lives on this backend
        scheduler singleton, so nightly + consolidate_now(full) share it.

        R3 Car 1 D1: the graph-layout precompute moved to the CORE orchestrator
        (viz, core-owned). Core runs it after this forwarded compute returns.
        """
        stats = self._consolidation_cycle()
        sleep_stats = self._maybe_sleep_cycle()
        # v5.86 (OT-C4): mandatory full similarity-link reconcile after sleep when
        # re-embedding mutated old↔old similarity, or on the weekly cadence. Inert
        # unless SIMILARITY_LINKING_INCREMENTAL_ENABLED (default OFF).
        self._maybe_full_reconcile(sleep_stats)
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

    @observe(tier="stage", metric="consolidation.run_domain_consolidation")
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

    # R3 Car 1 D1: _maybe_auto_vacuum moved to
    # yadgar.core.consolidation.orchestrator — it fires core.ops._fire_vacuum_service
    # (host lifecycle, core-owned). Core runs it after the forwarded compute
    # returns, holding the cooldown timestamp as a module-level global.
