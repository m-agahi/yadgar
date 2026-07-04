"""Orchestrator mixin — consolidation cycle and sleep cycle.

v5.7.0 PR-0: _daemon_loop (idle-triggered + daily 18:30 auto-consolidation)
removed. Consolidation now runs only when explicitly invoked via force_consolidate()
(MCP consolidate_now), or by the nightly cron. The _maybe_sleep_cycle helper is
wired into the nightly cron via ConsolidationScheduler.run_nightly_consolidation()
(#37 — PR-1 wiring; the sleep cycle had been dead from v5.7.0 until this).

v5.15.0 D1: per-phase duration alerting.  When any phase exceeds
PHASE_DURATION_WARN_MS (config default 60 000 ms = 1 min), a CRITICAL log is
emitted with the phase name and actual duration so bursts are immediately
visible in journalctl.  Override via YADGAR_PHASE_DURATION_WARN_MS env var or
config YAML.  Set to 0 to disable.
"""

import logging
import time
from datetime import UTC, datetime, timedelta

from yadgar.config import get_settings
from yadgar.observability.observe import observe

logger = logging.getLogger("yadgar.consolidation")

# v5.15.0 D1: loaded once at import time; tests may monkeypatch this module attr.
PHASE_DURATION_WARN_MS: int = get_settings().PHASE_DURATION_WARN_MS


def _warn_slow_phase(phase: str, duration_ms: int) -> None:
    """Emit CRITICAL log when a consolidation phase exceeds the warn threshold.

    Args:
        phase:       Name of the phase (e.g. "apply_decay").
        duration_ms: Actual elapsed time in milliseconds.
    """
    threshold = PHASE_DURATION_WARN_MS
    if threshold <= 0:
        return
    if duration_ms > threshold:
        logger.critical(
            "SLOW_PHASE phase=%s duration_ms=%d threshold_ms=%d — "
            "consolidation phase exceeded warn threshold; check CPU/embed-service load",
            phase,
            duration_ms,
            threshold,
        )


class _OrchestratorMixin:
    """Main consolidation cycle orchestrator."""

    @observe(tier="stage", name="consolidation.maybe_sleep_cycle")
    def _maybe_sleep_cycle(self) -> dict | None:
        """Run a full sleep cycle if at least 6 hours since the last one.

        Returns the run_sleep_cycle() stats (incl. `reembedded`/`compressed`) when
        a cycle ran, else None. v5.86 (OT-C4): the caller uses these to decide
        whether a full similarity-link reconcile is needed (re-embedding mutates
        old↔old similarity).
        """
        now = datetime.now(UTC)
        if self._last_sleep_cycle is not None:
            hours_since = (now - self._last_sleep_cycle).total_seconds() / 3600.0
            if hours_since < 6.0:
                return None
        try:
            stats = self._sleep_engine.run_sleep_cycle()
            self._last_sleep_cycle = now
            logger.info("Sleep cycle complete: %s", stats)
            return stats
        except Exception as _exc:
            from yadgar.exception_telemetry import record_exception  # noqa: PLC0415

            record_exception("consolidation.sleep_cycle", _exc)
            logger.exception("Sleep cycle failed")
            return None

    # ── v5.86 (OT-C4) incremental similarity-linking ───────────────────────────

    _SIMILARITY_WATERMARK_KEY = "similarity_linking"
    _FULL_RECONCILE_WATERMARK_KEY = "full_reconcile"

    @observe(tier="stage", name="consolidation.run_similarity_linking")
    def _run_similarity_linking(self, stats: dict) -> None:
        """In-cycle similarity linking — incremental fast-path when enabled, else full.

        DEFAULT OFF: with SIMILARITY_LINKING_INCREMENTAL_ENABLED False this calls
        the unchanged full N×N pass. When True it links only memories created
        since the persisted watermark (probe×corpus), then bumps the watermark to
        the cycle-start timestamp (captured BEFORE the fetch so a memory created
        mid-run is re-processed next cycle, never skipped). The mandatory full
        reconcile (post-sleep / weekly) remains the safety net for re-embedding.
        """
        if not getattr(self._settings, "SIMILARITY_LINKING_INCREMENTAL_ENABLED", False):
            self._link_similar_memories(stats)
            return

        run_start = datetime.now(UTC).isoformat()
        since = self._storage.get_consolidation_watermark(self._SIMILARITY_WATERMARK_KEY)
        if since is None:
            # First incremental run with no watermark → do a full pass to seed the
            # graph, then record the watermark so later cycles go incremental.
            self._link_similar_memories(stats)
        else:
            self._link_similar_memories_incremental(stats, since=since)
        self._storage.set_consolidation_watermark(self._SIMILARITY_WATERMARK_KEY, run_start)

    @observe(tier="hot", name="consolidation.full_reconcile_due")
    def _full_reconcile_due(self, embeddings_changed: bool) -> bool:
        """True when a full reconcile must run: embeddings changed OR weekly cadence."""
        if embeddings_changed:
            return True
        last = self._storage.get_consolidation_watermark(self._FULL_RECONCILE_WATERMARK_KEY)
        if last is None:
            return True
        interval_days = getattr(self._settings, "SIMILARITY_LINKING_RECONCILE_INTERVAL_DAYS", 7)
        try:
            last_dt = datetime.fromisoformat(last)
        except ValueError:
            return True
        return datetime.now(UTC) - last_dt >= timedelta(days=interval_days)

    @observe(tier="stage", name="consolidation.maybe_full_reconcile")
    def _maybe_full_reconcile(self, sleep_stats: dict | None) -> None:
        """Post-sleep safety net: re-run the FULL pass when embeddings mutated or weekly.

        Only fires when SIMILARITY_LINKING_INCREMENTAL_ENABLED is True — with the
        flag OFF the in-cycle full pass already covers everything, so this is inert
        (production behavior unchanged). `sleep_stats` is the run_sleep_cycle()
        result; re-embedding/compression there changes old↔old similarity that an
        incremental-by-created_at pass cannot see.
        """
        if not getattr(self._settings, "SIMILARITY_LINKING_INCREMENTAL_ENABLED", False):
            return
        s = sleep_stats or {}
        embeddings_changed = bool(s.get("reembedded", 0)) or bool(s.get("compressed", 0))
        if not self._full_reconcile_due(embeddings_changed):
            return
        try:
            _t = time.monotonic()
            logger.info(
                "phase_start: full_reconcile_links embeddings_changed=%s", embeddings_changed
            )
            self._link_similar_memories({})
            now_iso = datetime.now(UTC).isoformat()
            self._storage.set_consolidation_watermark(self._FULL_RECONCILE_WATERMARK_KEY, now_iso)
            # A full reconcile also re-establishes the incremental baseline.
            self._storage.set_consolidation_watermark(self._SIMILARITY_WATERMARK_KEY, now_iso)
            _dur_ms = int((time.monotonic() - _t) * 1000)
            logger.info("phase_end: full_reconcile_links duration_ms=%d", _dur_ms)
            _warn_slow_phase("full_reconcile_links", _dur_ms)
        except Exception as _exc:
            from yadgar.exception_telemetry import record_exception  # noqa: PLC0415

            record_exception("consolidation.phase_full_reconcile_links", _exc)
            logger.exception("Full similarity-link reconcile failed")

    @observe(tier="stage", name="consolidation.maybe_precompute_graph_layout")
    def _maybe_precompute_graph_layout(self) -> None:
        """v5.88: precompute + cache the 3D graph layout (nightly path only).

        Gated three ways so it never blocks the daemon: (1) the
        VIZ_PRECOMPUTED_LAYOUT_ENABLED flag (default OFF), (2) a graph-signature
        no-op — when the live graph shape matches the cached signature nothing is
        recomputed, and (3) it is only called from run_nightly_consolidation, so
        the light consolidate_now budget is never charged the spring_layout cost.
        Non-fatal: any failure is logged + recorded, never raised.
        """
        if not getattr(self._settings, "VIZ_PRECOMPUTED_LAYOUT_ENABLED", False):
            return
        try:
            from yadgar.graph_api import GraphAPI  # noqa: PLC0415
            from yadgar.graph_layout import compute_graph_layout, graph_signature  # noqa: PLC0415

            # Lay out the FULL uncapped graph (caps=0) so positions stay stable
            # when the per-request /api/graph node caps change.
            data = GraphAPI(self._storage).get_full_graph(0, 8, False, None, 0, 0)
            nodes, edges = data.get("nodes", []), data.get("edges", [])
            sig = graph_signature(nodes, edges)
            cached = self._storage.get_graph_layout_cache()
            if cached and cached.get("signature") == sig:
                return  # graph shape unchanged — keep the cached layout

            _t = time.monotonic()
            iterations = getattr(self._settings, "VIZ_LAYOUT_ITERATIONS", 50)
            logger.info("phase_start: precompute_graph_layout nodes=%d", len(nodes))
            positions = compute_graph_layout(nodes, edges, dim=3, iterations=iterations)
            self._storage.set_graph_layout_cache(sig, positions, datetime.now(UTC).isoformat())
            _dur_ms = int((time.monotonic() - _t) * 1000)
            logger.info("phase_end: precompute_graph_layout duration_ms=%d", _dur_ms)
            _warn_slow_phase("precompute_graph_layout", _dur_ms)
        except Exception as _exc:
            from yadgar.exception_telemetry import record_exception  # noqa: PLC0415

            record_exception("consolidation.phase_precompute_graph_layout", _exc)
            logger.exception("Precompute graph layout failed")

    @observe(tier="stage", name="consolidation.episodic")
    def _run_episodic_phases(self, stats: dict) -> None:
        """Phase group 1: decay, episode processing, pruning, duplicate merge."""
        _t = time.monotonic()
        logger.info("phase_start: apply_decay")
        self._apply_decay(stats)
        _dur_ms = int((time.monotonic() - _t) * 1000)
        logger.info("phase_end: apply_decay duration_ms=%d", _dur_ms)
        _warn_slow_phase("apply_decay", _dur_ms)

        _t = time.monotonic()
        logger.info("phase_start: process_episodes")
        self._process_new_episodes(stats)
        _dur_ms = int((time.monotonic() - _t) * 1000)
        logger.info("phase_end: process_episodes duration_ms=%d", _dur_ms)
        _warn_slow_phase("process_episodes", _dur_ms)

        # Prune old episodes to keep the table bounded and _check_temporal_order fast
        self._prune_old_episodes_safe()

        _t = time.monotonic()
        logger.info("phase_start: merge_duplicates")
        self._merge_duplicates(stats)
        _dur_ms = int((time.monotonic() - _t) * 1000)
        logger.info("phase_end: merge_duplicates duration_ms=%d", _dur_ms)
        _warn_slow_phase("merge_duplicates", _dur_ms)

    @observe(tier="stage", name="consolidation.graph")
    def _run_graph_phases(self, stats: dict) -> None:
        """Phase group 2: similarity linking, causality, graph priors, cofire priors."""
        # Semantic similarity linking — create relationships between similar memories
        try:
            _t = time.monotonic()
            logger.info("phase_start: link_similar")
            self._run_similarity_linking(stats)
            _dur_ms = int((time.monotonic() - _t) * 1000)
            logger.info("phase_end: link_similar duration_ms=%d", _dur_ms)
            _warn_slow_phase("link_similar", _dur_ms)
        except Exception as _exc:
            from yadgar.exception_telemetry import record_exception  # noqa: PLC0415

            record_exception("consolidation.phase_link_similar", _exc)
            logger.exception("Similarity linking failed")

        try:
            _t = time.monotonic()
            logger.info("phase_start: detect_causality")
            self._graph.detect_causality()
            _dur_ms = int((time.monotonic() - _t) * 1000)
            logger.info("phase_end: detect_causality duration_ms=%d", _dur_ms)
            _warn_slow_phase("detect_causality", _dur_ms)
        except Exception as _exc:
            from yadgar.exception_telemetry import record_exception  # noqa: PLC0415

            record_exception("consolidation.phase_detect_causality", _exc)
            logger.exception("Causal detection failed")

        # v5.54.1: Precompute per-memory graph_prior scalars for fast-profile recall.
        # Runs after detect_causality so the entity graph is maximally fresh.
        # Non-fatal — prior is additive; a missing cycle just keeps old values.
        try:
            _t = time.monotonic()
            logger.info("phase_start: compute_graph_priors")
            self._compute_graph_priors(stats)
            _dur_ms = int((time.monotonic() - _t) * 1000)
            logger.info("phase_end: compute_graph_priors duration_ms=%d", _dur_ms)
            _warn_slow_phase("compute_graph_priors", _dur_ms)
        except Exception as _exc:
            from yadgar.exception_telemetry import record_exception  # noqa: PLC0415

            record_exception("consolidation.phase_compute_graph_priors", _exc)
            logger.exception("Graph prior computation failed (non-fatal)")

        # v5.54.2: Precompute per-memory cofire_prior scalars from co-recall transitions.
        # Runs after compute_graph_priors. Reads memory_transition once (bulk), stores
        # a normalized co-recall frequency on each memory row. Non-fatal — additive.
        try:
            _t = time.monotonic()
            logger.info("phase_start: compute_cofire_priors")
            self._compute_cofire_priors(stats)
            _dur_ms = int((time.monotonic() - _t) * 1000)
            logger.info("phase_end: compute_cofire_priors duration_ms=%d", _dur_ms)
            _warn_slow_phase("compute_cofire_priors", _dur_ms)
        except Exception as _exc:
            from yadgar.exception_telemetry import record_exception  # noqa: PLC0415

            record_exception("consolidation.phase_compute_cofire_priors", _exc)
            logger.exception("Co-fire prior computation failed (non-fatal)")

    @observe(tier="stage", name="consolidation.curation")
    def _run_curation_phases(self, stats: dict) -> None:
        """Phase group 3: memify, CLS consolidation, action log processing."""
        # Run memify self-improvement cycle
        try:
            _t = time.monotonic()
            logger.info("phase_start: memify")
            memify_stats = self._curator.memify_cycle()
            stats["memify_pruned"] = memify_stats.get("pruned", 0)
            stats["memify_strengthened"] = memify_stats.get("strengthened", 0)
            stats["memify_reweighted"] = memify_stats.get("reweighted", 0)
            stats["memify_derived"] = memify_stats.get("derived", 0)
            _dur_ms = int((time.monotonic() - _t) * 1000)
            logger.info("phase_end: memify duration_ms=%d", _dur_ms)
            _warn_slow_phase("memify", _dur_ms)
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
            _dur_ms = int((time.monotonic() - _t) * 1000)
            logger.info("phase_end: cls_consolidation duration_ms=%d", _dur_ms)
            _warn_slow_phase("cls_consolidation", _dur_ms)
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
            _dur_ms = int((time.monotonic() - _t) * 1000)
            logger.info("phase_end: action_log duration_ms=%d", _dur_ms)
            _warn_slow_phase("action_log", _dur_ms)
        except Exception as _exc:
            from yadgar.exception_telemetry import record_exception  # noqa: PLC0415

            record_exception("consolidation.phase_action_log", _exc)
            logger.exception("Action log processing failed")

    @observe(tier="boundary", name="consolidation.cycle")
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
            self._run_episodic_phases(stats)
            self._run_graph_phases(stats)
            self._run_curation_phases(stats)

            # Domain-aware consolidation (entity extraction per domain).
            # Heat decay is owned solely by _apply_decay above — re-wiring here
            # cannot cause double-decay because consolidate_domain no longer writes heat.
            _pool = getattr(self, "_pool", None)
            _astro_settings = getattr(self, "_settings", None)
            if _pool is not None and getattr(_astro_settings, "ASTROCYTE_POOL_ENABLED", True):
                try:
                    _t = time.monotonic()
                    logger.info("phase_start: domain_consolidation")
                    self._run_domain_consolidation()
                    _dur_ms = int((time.monotonic() - _t) * 1000)
                    logger.info("phase_end: domain_consolidation duration_ms=%d", _dur_ms)
                    _warn_slow_phase("domain_consolidation", _dur_ms)
                except Exception as _exc:
                    from yadgar.exception_telemetry import record_exception  # noqa: PLC0415

                    record_exception("consolidation.phase_domain_consolidation", _exc)
                    logger.exception("Domain consolidation failed")

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

    @observe(tier="stage", name="consolidation.run_post_cycle_tasks")
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
