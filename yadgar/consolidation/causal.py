"""Causal discovery dispatch mixin for ConsolidationScheduler.

v5.1 C1 fix: _events_since_last_discovery accumulates action_memories_created +
cls_promoted + memify_derived AFTER the memory-producing phases complete.
"""

import logging
import time

from yadgar.tracing import trace_span

logger = logging.getLogger("yadgar.consolidation")


class _CausalMixin:
    """Periodic PC-algorithm causal discovery dispatch."""

    @trace_span("consolidation.causal")
    def _run_causal_discovery_phase(self, stats: dict) -> None:
        """Run formal causal discovery (PC algorithm) periodically.

        Placed after all memory-producing phases so counters are fully populated.
        build_event_matrix reads the episodes table (not action_log rows), so moving
        the dispatch after action_log processing does not deprive it of data.
        """
        if self._causal_discovery is not None:
            # Sum memories created this cycle across all phases that produce new memories.
            cycle_memories_added = (
                stats.get("action_memories_created", 0)
                + stats.get("cls_promoted", 0)
                + stats.get("memify_derived", 0)
            )
            stats["memories_added"] = cycle_memories_added
            self._events_since_last_discovery += cycle_memories_added
            if self._events_since_last_discovery >= 50:
                try:
                    _t = time.monotonic()
                    logger.info("phase_start: causal_discovery")
                    dag = self._causal_discovery.discover_dag()
                    stats["causal_dag_edges"] = dag.get("metadata", {}).get("directed_count", 0)
                    self._events_since_last_discovery = 0
                    logger.info(
                        "phase_end: causal_discovery duration_ms=%d",
                        int((time.monotonic() - _t) * 1000),
                    )
                except Exception:
                    logger.exception("Causal discovery failed")
