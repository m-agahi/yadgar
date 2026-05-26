"""Orchestrator mixin — consolidation cycle and sleep cycle.

v5.7.0 PR-0: _daemon_loop (idle-triggered + daily 18:30 auto-consolidation)
removed. Consolidation now runs only when explicitly invoked via force_consolidate()
(MCP consolidate_now), or by the nightly cron (PR-1). The _maybe_sleep_cycle
helper is preserved for PR-1 to wire into the cron.
"""

import logging
import time
from datetime import UTC, datetime

from yadgar.tracing import trace_span

logger = logging.getLogger("yadgar.consolidation")


class _OrchestratorMixin:
    """Main consolidation cycle orchestrator."""

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
        except Exception as _exc:
            from yadgar.exception_telemetry import record_exception  # noqa: PLC0415

            record_exception("consolidation.sleep_cycle", _exc)
            logger.exception("Sleep cycle failed")

    @trace_span("consolidation.cycle")
    def _consolidation_cycle(self) -> dict:
        _cycle_wall_t0 = time.monotonic()
        start = time.monotonic()
        stats = {
            "memories_added": 0,
            "memories_updated": 0,
            "memories_archived": 0,
            "memories_deleted": 0,
        }

        try:
            _t = time.monotonic()
            logger.info("phase_start: apply_decay")
            self._apply_decay(stats)
            logger.info(
                "phase_end: apply_decay duration_ms=%d", int((time.monotonic() - _t) * 1000)
            )

            _t = time.monotonic()
            logger.info("phase_start: process_episodes")
            self._process_new_episodes(stats)
            logger.info(
                "phase_end: process_episodes duration_ms=%d", int((time.monotonic() - _t) * 1000)
            )

            # Prune old episodes to keep the table bounded and _check_temporal_order fast
            self._prune_old_episodes_safe()

            _t = time.monotonic()
            logger.info("phase_start: merge_duplicates")
            self._merge_duplicates(stats)
            logger.info(
                "phase_end: merge_duplicates duration_ms=%d", int((time.monotonic() - _t) * 1000)
            )

            # Semantic similarity linking — create relationships between similar memories
            try:
                _t = time.monotonic()
                logger.info("phase_start: link_similar")
                self._link_similar_memories(stats)
                logger.info(
                    "phase_end: link_similar duration_ms=%d", int((time.monotonic() - _t) * 1000)
                )
            except Exception as _exc:
                from yadgar.exception_telemetry import record_exception  # noqa: PLC0415

                record_exception("consolidation.phase_link_similar", _exc)
                logger.exception("Similarity linking failed")

            try:
                _t = time.monotonic()
                logger.info("phase_start: detect_causality")
                self._graph.detect_causality()
                logger.info(
                    "phase_end: detect_causality duration_ms=%d",
                    int((time.monotonic() - _t) * 1000),
                )
            except Exception as _exc:
                from yadgar.exception_telemetry import record_exception  # noqa: PLC0415

                record_exception("consolidation.phase_detect_causality", _exc)
                logger.exception("Causal detection failed")

            # Run memify self-improvement cycle
            try:
                _t = time.monotonic()
                logger.info("phase_start: memify")
                memify_stats = self._curator.memify_cycle()
                stats["memify_pruned"] = memify_stats.get("pruned", 0)
                stats["memify_strengthened"] = memify_stats.get("strengthened", 0)
                stats["memify_reweighted"] = memify_stats.get("reweighted", 0)
                stats["memify_derived"] = memify_stats.get("derived", 0)
                logger.info("phase_end: memify duration_ms=%d", int((time.monotonic() - _t) * 1000))
            except Exception as _exc:
                from yadgar.exception_telemetry import record_exception  # noqa: PLC0415

                record_exception("consolidation.phase_memify", _exc)
                logger.exception("Memify cycle failed")

            # Run CLS dual-store consolidation (Go-CLS: episodic → semantic)
            try:
                _t = time.monotonic()
                logger.info("phase_start: cls_consolidation")
                cls_stats = self._cls.consolidation_cycle()
                stats["cls_patterns_found"] = cls_stats.get("patterns_found", 0)
                stats["cls_promoted"] = cls_stats.get("promoted", 0)
                stats["cls_skipped_inconsistent"] = cls_stats.get("skipped_inconsistent", 0)
                logger.info(
                    "phase_end: cls_consolidation duration_ms=%d",
                    int((time.monotonic() - _t) * 1000),
                )
            except Exception as _exc:
                from yadgar.exception_telemetry import record_exception  # noqa: PLC0415

                record_exception("consolidation.phase_cls_consolidation", _exc)
                logger.exception("CLS consolidation cycle failed")

            # Compression disabled: memory content must stay intact for LLM usage.

            # Process action_log entries into real memories
            try:
                _t = time.monotonic()
                logger.info("phase_start: action_log")
                action_stats = self._process_action_log()
                stats["actions_processed"] = action_stats.get("processed", 0)
                stats["action_memories_created"] = action_stats.get("memories_created", 0)
                logger.info(
                    "phase_end: action_log duration_ms=%d", int((time.monotonic() - _t) * 1000)
                )
            except Exception as _exc:
                from yadgar.exception_telemetry import record_exception  # noqa: PLC0415

                record_exception("consolidation.phase_action_log", _exc)
                logger.exception("Action log processing failed")

            # Run formal causal discovery (PC algorithm) periodically.
            # v5.1 C1: placed after all memory-producing phases so counters are fully populated.
            self._run_causal_discovery_phase(stats)

            # Table retention prunes — each is non-fatal; logged at debug on error.
            self._run_retention_tasks()

            self._run_post_cycle_tasks(stats, start)
            return stats
        finally:
            # PR-E: observe full cycle wall-clock even if a non-guarded phase raises
            try:
                from yadgar.metrics import yadgar_consolidation_duration_seconds  # noqa: PLC0415

                yadgar_consolidation_duration_seconds.labels(phase="full_cycle").observe(
                    time.monotonic() - _cycle_wall_t0
                )
            except Exception:
                pass

    def _run_post_cycle_tasks(self, stats: dict, start: float) -> None:
        """Non-fatal post-consolidation tasks: invariant checks, vacuum, log, MTREE probe."""
        # Run invariant checks — violations are logged CRITICAL
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
        logger.info("phase_start: insert_consolidation_log")
        duration_ms = int((time.monotonic() - start) * 1000)
        self._storage.insert_consolidation_log({**stats, "duration_ms": duration_ms})
        logger.info(
            "phase_end: insert_consolidation_log duration_ms=%d",
            int((time.monotonic() - _t) * 1000),
        )
        logger.info("Consolidation complete in %dms: %s", duration_ms, stats)

        # Post-consolidation MTREE health probe: bulk embedding writes during
        # consolidation are the primary trigger for SurrealDB 2.6.x index
        # corruption. Detect and auto-recover while the damage is still fresh.
        logger.info("phase_start: mtree_probe")
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
        logger.info("phase_end: mtree_probe duration_ms=%d", int((time.monotonic() - _t) * 1000))
