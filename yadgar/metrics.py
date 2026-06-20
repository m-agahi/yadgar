"""Prometheus metrics endpoint for Yadgar.

Gated by YADGAR_METRICS_ENABLED (default True).
Exposed at /metrics — unauthenticated on loopback per §2 design.

Collectors:
- yadgar_queue_depth{queue}       Gauge   — items in queue/, archive/, dlq/
- yadgar_requests_total{route}    Counter — requests by route
- yadgar_consolidation_duration_seconds{phase}  Histogram — cycle phase timing
- yadgar_embedding_cache_hits_total Counter     — embedding cache hits
- yadgar_embedding_cache_misses_total Counter   — embedding cache misses
- yadgar_action_batch_size         Histogram    — action-batch size
- yadgar_tool_token_estimate_total{tool}  Counter — estimated tokens returned per tool call
- yadgar_cache_hit_total{cache}    Counter — cache hits by cache name
- yadgar_cache_miss_total{cache}   Counter — cache misses by cache name
- yadgar_loop_last_run_unix_timestamp{loop}  Gauge   — unix timestamp of last loop iteration start
- yadgar_loop_errors_total{loop,error_type}  Counter — exceptions caught in each background loop
- yadgar_signals_payload_oversized_total     Counter — signals mode payload exceeded SIGNALS_TOKEN_BUDGET_SOFT
- yadgar_archive_purged_total               Counter — memory_archive rows deleted by nightly retention purge
- yadgar_archive_retention_skipped_total{reason} Counter — rows skipped by retention purge (protected|anchor|recent)
- yadgar_hook_recall_timeout_total{handler} Counter — hook recall() calls exceeding HOOK_RECALL_TIMEOUT_S latency budget
- yadgar_cold_purge_candidates              Gauge   — cold immortal user-memory retention candidates (visibility gate #29)
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.requests import Request
from starlette.responses import Response

if TYPE_CHECKING:
    pass

# Module-level registry — isolated so tests can run without cross-contamination
_registry = CollectorRegistry()

# ── Collectors ─────────────────────────────────────────────────────────

# Queue depth by queue type: queue, archive, dlq
yadgar_queue_depth = Gauge(
    "yadgar_queue_depth",
    "Number of items in each queue directory",
    ["queue"],
    registry=_registry,
)

# Request counter by route
yadgar_requests_total = Counter(
    "yadgar_requests_total",
    "Total MCP tool requests by route/tool name",
    ["route"],
    registry=_registry,
)

# Consolidation phase duration histogram
yadgar_consolidation_duration_seconds = Histogram(
    "yadgar_consolidation_duration_seconds",
    "Duration of each consolidation cycle phase in seconds",
    ["phase"],
    buckets=(0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 10.0, 30.0, 60.0),
    registry=_registry,
)

# Embedding cache hit/miss counters
yadgar_embedding_cache_hits_total = Counter(
    "yadgar_embedding_cache_hits_total",
    "Total embedding cache hits",
    registry=_registry,
)

yadgar_embedding_cache_misses_total = Counter(
    "yadgar_embedding_cache_misses_total",
    "Total embedding cache misses",
    registry=_registry,
)

# Action-batch size histogram
yadgar_action_batch_size = Histogram(
    "yadgar_action_batch_size",
    "Number of actions processed per consolidation action-log batch",
    buckets=(1, 5, 10, 25, 50, 100, 200, 500),
    registry=_registry,
)

# ── v5.3.5 Q1 — Token-budget + cache-hit metrics ─────────────────────

# Estimated tokens returned per tool call (len(result_text) / 4 approximation)
yadgar_tool_token_estimate_total = Counter(
    "yadgar_tool_token_estimate_total",
    "Estimated tokens returned per MCP tool call (len/4 approximation)",
    ["tool"],
    registry=_registry,
)

# Generic cache hit/miss counters keyed by cache name
yadgar_cache_hit_total = Counter(
    "yadgar_cache_hit_total",
    "Total cache hits by cache name",
    ["cache"],
    registry=_registry,
)

yadgar_cache_miss_total = Counter(
    "yadgar_cache_miss_total",
    "Total cache misses by cache name",
    ["cache"],
    registry=_registry,
)

# ── P11 Observability v1 — write path ───────────────────────────────────────

yadgar_dlq_size = Gauge(
    "yadgar_dlq_size",
    "Number of items in the dead-letter queue",
    registry=_registry,
)

yadgar_drainer_lag_ms = Histogram(
    "yadgar_drainer_lag_ms",
    "Lag from enqueue to drain-start in milliseconds",
    buckets=(1, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000),
    registry=_registry,
)

yadgar_drain_cycle_duration_ms = Histogram(
    "yadgar_drain_cycle_duration_ms",
    "Duration of one full drain cycle in milliseconds",
    buckets=(1, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000),
    registry=_registry,
)

yadgar_drain_stage_ms = Histogram(
    "yadgar_drain_stage_ms",
    "Duration of a drain stage in milliseconds",
    ["stage"],
    buckets=(1, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000),
    registry=_registry,
)

yadgar_writegate_outcome = Counter(
    "yadgar_writegate_outcome",
    "WriteGate decisions by outcome",
    ["outcome"],
    registry=_registry,
)

# ── v5.41.5 — wiki_add rejection counter (I23) ───────────────────────────────
# Emitted by drainer similarity gate (moved from request path, I9 fix).
# reason labels: "duplicate_detected"

yadgar_wiki_add_rejected_total = Counter(
    "yadgar_wiki_add_rejected_total",
    "wiki_add calls rejected by the similarity gate (drainer stage)",
    ["reason"],
    registry=_registry,
)

# ── v5.42.0 — DLQ rejection count gauge (I23, plan §7) ───────────────────────
# Gauge: current count of DLQ entries with failure_reason in rejection taxonomy.
# Ops visibility without per-directory cardinality (plan §13 Q1: total count only).

yadgar_dlq_rejection_count = Gauge(
    "yadgar_dlq_rejection_count",
    "Current count of DLQ entries with failure_reason in the rejection taxonomy "
    "(duplicate_detected, policy_rejected)",
    registry=_registry,
)

# ── v5.42.6 — enforcement-relaxed writes counter (I23) ───────────────────────
# Emitted by dlq.py::_validate_wiki_add / _validate_branch_context when
# YADGAR_DIRECTORY_ENFORCEMENT or YADGAR_BRANCH_ENFORCEMENT is false.
# enforcement labels: "directory" | "branch"

yadgar_writes_with_enforcement_relaxed = Counter(
    "yadgar_writes_with_enforcement_relaxed",
    "Writes that bypassed enforcement because the relevant knob is off (v5.42.6)",
    ["enforcement"],
    registry=_registry,
)

# ── v5.42.1 — wiki embedding compute failure counter (I23) ───────────────────
# Emitted by wiki.py::_compute_embedding on exception.
# reason labels: "exception" (encode_document raised), "returned_none" (None returned)

yadgar_wiki_embedding_compute_failed_total = Counter(
    "yadgar_wiki_embedding_compute_failed_total",
    "wiki_page embedding computation failures (silent previously; surfaced v5.42.1)",
    ["reason"],
    registry=_registry,
)

# ── P11 — read path ─────────────────────────────────────────────────────────

yadgar_recall_duration_ms = Histogram(
    "yadgar_recall_duration_ms",
    "Total recall() duration in milliseconds",
    buckets=(1, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000),
    registry=_registry,
)

yadgar_recall_result_count = Histogram(
    "yadgar_recall_result_count",
    "Number of results returned by recall()",
    buckets=(0, 1, 2, 3, 5, 10, 15, 20),
    registry=_registry,
)

yadgar_recall_stage_ms = Histogram(
    "yadgar_recall_stage_ms",
    "Duration of a recall stage in milliseconds",
    ["stage"],
    buckets=(1, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000),
    registry=_registry,
)

# ── v5.31.0 — Plugin pipeline per-stage metrics ──────────────────────────────

yadgar_recall_stage_duration_seconds = Histogram(
    "yadgar_recall_stage_duration_seconds",
    "Duration of a recall pipeline stage in seconds (v5.31.0 plugin arch)",
    ["stage", "profile"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    registry=_registry,
)

yadgar_recall_stage_candidates_in = Gauge(
    "yadgar_recall_stage_candidates_in",
    "Candidate count entering a recall pipeline stage",
    ["stage", "profile"],
    registry=_registry,
)

yadgar_recall_stage_candidates_out = Gauge(
    "yadgar_recall_stage_candidates_out",
    "Candidate count exiting a recall pipeline stage",
    ["stage", "profile"],
    registry=_registry,
)

yadgar_recall_profile_invocations_total = Counter(
    "yadgar_recall_profile_invocations_total",
    "Total recall() calls by profile name",
    ["profile"],
    registry=_registry,
)

yadgar_wiki_query_duration_ms = Histogram(
    "yadgar_wiki_query_duration_ms",
    "Total wiki_query() duration in milliseconds",
    buckets=(1, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000),
    registry=_registry,
)

yadgar_wiki_query_stage_ms = Histogram(
    "yadgar_wiki_query_stage_ms",
    "Duration of a wiki_query stage in milliseconds",
    ["stage"],
    buckets=(1, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000),
    registry=_registry,
)

# ── P11 — embedding ──────────────────────────────────────────────────────────

yadgar_encode_duration_ms = Histogram(
    "yadgar_encode_duration_ms",
    "Embedding encode duration in milliseconds",
    ["model"],
    buckets=(1, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000),
    registry=_registry,
)

yadgar_encode_queue_depth = Gauge(
    "yadgar_encode_queue_depth",
    "Pending asyncio.to_thread encode tasks (0 until queue added)",
    registry=_registry,
)

# ── P11 — KG / curator / engram ──────────────────────────────────────────────

yadgar_entity_extract_duration_ms = Histogram(
    "yadgar_entity_extract_duration_ms",
    "Entity extraction duration in milliseconds",
    buckets=(1, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000),
    registry=_registry,
)

yadgar_curator_duration_ms = Histogram(
    "yadgar_curator_duration_ms",
    "Curator merge/dedup duration in milliseconds",
    buckets=(1, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000),
    registry=_registry,
)

yadgar_curator_merge_outcome = Counter(
    "yadgar_curator_merge_outcome",
    "Curator merge decision by outcome",
    ["outcome"],
    registry=_registry,
)

# v5.17.0 — write-time contradiction detection (Adopt-2)
yadgar_write_time_contradiction_total = Counter(
    "yadgar_write_time_contradiction_total",
    "Contradictions detected at write time, by detector reason "
    "(negation_mismatch | action_divergence). Gated by YADGAR_WRITE_TIME_CONTRADICTION "
    "(default on).",
    ["reason"],
    registry=_registry,
)

yadgar_engram_allocate_duration_ms = Histogram(
    "yadgar_engram_allocate_duration_ms",
    "Engram slot allocation duration in milliseconds",
    buckets=(1, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000),
    registry=_registry,
)

yadgar_astrocyte_assign_duration_ms = Histogram(
    "yadgar_astrocyte_assign_duration_ms",
    "Astrocyte assignment duration in milliseconds",
    buckets=(1, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000),
    registry=_registry,
)

# ── P11 — LLM (C4 conflict resolver) ─────────────────────────────────────────

yadgar_llm_call_duration_ms = Histogram(
    "yadgar_llm_call_duration_ms",
    "LLM call duration in milliseconds",
    ["provider", "model", "purpose"],
    buckets=(10, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000),
    registry=_registry,
)

yadgar_llm_decision = Counter(
    "yadgar_llm_decision",
    "LLM conflict resolver decision by outcome",
    ["outcome"],
    registry=_registry,
)

# ── P11 — MCP transport + auth ────────────────────────────────────────────────

yadgar_mcp_request_duration_ms = Histogram(
    "yadgar_mcp_request_duration_ms",
    "MCP tool request duration in milliseconds",
    ["tool"],
    buckets=(1, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000),
    registry=_registry,
)

yadgar_mcp_auth_check_duration_ms = Histogram(
    "yadgar_mcp_auth_check_duration_ms",
    "MCP auth check duration in milliseconds",
    buckets=(0.1, 0.5, 1, 5, 10, 25, 50, 100),
    registry=_registry,
)

yadgar_mcp_request_count = Counter(
    "yadgar_mcp_request_count",
    "MCP tool request count by tool and status",
    ["tool", "status"],
    registry=_registry,
)

# ── P11 — Database ────────────────────────────────────────────────────────────

yadgar_surrealdb_query_duration_ms = Histogram(
    "yadgar_surrealdb_query_duration_ms",
    "SurrealDB query duration in milliseconds",
    ["op"],
    buckets=(0.5, 1, 5, 10, 25, 50, 100, 250, 500, 1000, 2500),
    registry=_registry,
)

yadgar_surrealdb_connection_pool_wait_ms = Histogram(
    "yadgar_surrealdb_connection_pool_wait_ms",
    "SurrealDB connection pool wait time in milliseconds",
    buckets=(0.1, 0.5, 1, 5, 10, 25, 50, 100, 250, 500),
    registry=_registry,
)

yadgar_surrealdb_pool_active = Gauge(
    "yadgar_surrealdb_pool_active",
    "Active SurrealDB connections",
    registry=_registry,
)

# ── P11 — Process ─────────────────────────────────────────────────────────────

yadgar_process_rss_bytes = Gauge(
    "yadgar_process_rss_bytes",
    "Process resident set size in bytes",
    registry=_registry,
)

yadgar_process_cpu_percent = Gauge(
    "yadgar_process_cpu_percent",
    "Process CPU usage percent",
    registry=_registry,
)

yadgar_process_open_fds = Gauge(
    "yadgar_process_open_fds",
    "Number of open file descriptors",
    registry=_registry,
)

yadgar_python_gc_duration_ms = Histogram(
    "yadgar_python_gc_duration_ms",
    "Python GC collection duration in milliseconds",
    ["generation"],
    buckets=(0.1, 0.5, 1, 5, 10, 25, 50, 100, 250, 500),
    registry=_registry,
)

# ── P11 — Subagents ───────────────────────────────────────────────────────────

yadgar_subagent_dispatch_count = Counter(
    "yadgar_subagent_dispatch_count",
    "Subagent dispatch count by agent type",
    ["agent_type"],
    registry=_registry,
)

yadgar_subagent_capture_rate = Gauge(
    "yadgar_subagent_capture_rate",
    "Subagent findings capture rate (captured / dispatched)",
    registry=_registry,
)
# Placeholder: no in-process capture tracking yet — set 0 so dashboard panels
# have a sample rather than rendering "no data".
yadgar_subagent_capture_rate.set(0)

# ── P11 — Viz ─────────────────────────────────────────────────────────────────

yadgar_viz_api_graph_duration_ms = Histogram(
    "yadgar_viz_api_graph_duration_ms",
    "Viz /api/graph response duration in milliseconds",
    buckets=(1, 5, 10, 25, 50, 100, 250, 500, 1000, 2500),
    registry=_registry,
)

yadgar_viz_sse_clients = Gauge(
    "yadgar_viz_sse_clients",
    "Active SSE clients connected to viz",
    registry=_registry,
)

yadgar_viz_dbsize_sample_duration_ms = Histogram(
    "yadgar_viz_dbsize_sample_duration_ms",
    "Viz dbsize sample duration in milliseconds",
    buckets=(1, 5, 10, 25, 50, 100, 250, 500, 1000),
    registry=_registry,
)

# ── v5.10.9 — Orphan-edge filter observability ───────────────────────────────

yadgar_graph_api_orphan_edges_dropped_total = Counter(
    "yadgar_graph_api_orphan_edges_dropped_total",
    "Total edges dropped by get_full_graph() because one or both endpoints were absent "
    "from the returned node set. A non-zero value indicates backend payload drift "
    "(e.g. causal edges referencing entity nodes not included in the graph response).",
    registry=_registry,
)

# ── P11 — Backend liveness + circuit breaker (N3) ────────────────────────────

yadgar_backend_reachable = Gauge(
    "yadgar_backend_reachable",
    "Backend endpoint reachability (1=reachable, 0=unreachable)",
    ["endpoint"],
    registry=_registry,
)

yadgar_circuit_breaker_state = Gauge(
    "yadgar_circuit_breaker_state",
    "Circuit breaker state per endpoint (0=CLOSED, 1=HALF_OPEN, 2=OPEN)",
    ["endpoint"],
    registry=_registry,
)

# Mapping used by _CircuitBreaker to convert state strings to gauge values.
_CB_STATE_VALUES: dict[str, int] = {"closed": 0, "half_open": 1, "open": 2}


def _collect_process_metrics() -> None:
    """Sample process RSS, CPU, and FD count into gauges. Non-fatal."""
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF)
        # ru_maxrss: KiB on Linux, bytes on macOS — normalise to bytes
        import platform

        rss = usage.ru_maxrss
        if platform.system() == "Linux":
            rss = rss * 1024
        yadgar_process_rss_bytes.set(rss)
    except Exception:
        pass

    try:
        import os

        fds = len(os.listdir("/proc/self/fd"))
        yadgar_process_open_fds.set(fds)
    except Exception:
        pass


# ── v5.5.1 — Log system observability ────────────────────────────────────────

yadgar_log_file_rotations_total = Counter(
    "yadgar_log_file_rotations_total",
    "Total rotating file handler doRollover() calls",
    ["logger"],
    registry=_registry,
)

yadgar_log_file_size_bytes = Gauge(
    "yadgar_log_file_size_bytes",
    "Current active log file size in bytes",
    ["logger"],
    registry=_registry,
)

yadgar_log_dropped_total = Counter(
    "yadgar_log_dropped_total",
    "Total log records dropped by rate limiter",
    ["logger", "level", "reason"],
    registry=_registry,
)

# ── v5.6.7 PR-J — Runtime config knob gauges ─────────────────────────────────

yadgar_config_value = Gauge(
    "yadgar_config_value",
    "Numeric env-driven config values at runtime",
    ["name"],
    registry=_registry,
)

# ── v5.6.7 PR-K — Hook handler execution metrics ─────────────────────────────

yadgar_hook_execution_duration_ms = Histogram(
    "yadgar_hook_execution_duration_ms",
    "Wall-clock execution duration of HTTP hook handlers in milliseconds",
    ["hook"],
    buckets=(1, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000),
    registry=_registry,
)

yadgar_hook_failure_total = Counter(
    "yadgar_hook_failure_total",
    "Total hook handler failures by hook name and failure reason "
    "(reason = exception class name or HTTP status code string)",
    ["hook", "reason"],
    registry=_registry,
)

# ── v5.6.7 PR-H — Exception telemetry ────────────────────────────────────────

yadgar_exception_total = Counter(
    "yadgar_exception_total",
    "Total swallowed or near-silent exceptions by handler location and exception type",
    ["location", "error_type"],
    registry=_registry,
)

# ── v5.6.7 PR-I — Background loop heartbeat gauges + error counters ──────────

yadgar_loop_last_run_unix_timestamp = Gauge(
    "yadgar_loop_last_run_unix_timestamp",
    "Unix timestamp of the most-recent iteration start for each background loop. "
    "Stale value (older than expected interval) indicates a hung or dead loop.",
    ["loop"],
    registry=_registry,
)

yadgar_loop_errors_total = Counter(
    "yadgar_loop_errors_total",
    "Total exceptions caught in each background loop body",
    ["loop", "error_type"],
    registry=_registry,
)

# ── v5.10.1 — signals mode token-budget observability ────────────────────────

yadgar_signals_payload_oversized_total = Counter(
    "yadgar_signals_payload_oversized_total",
    "Total project_brief(mode='signals') calls where payload exceeded SIGNALS_TOKEN_BUDGET_SOFT",
    registry=_registry,
)

# ── v5.51.0 — hook recall latency budget timeout counter (I23) ───────────────
# Emitted by yadgar/server/http.py _recall_with_timeout() when asyncio.wait_for
# exceeds HOOK_RECALL_TIMEOUT_S.
# handler labels: "prompt-recall" | "instructions-loaded" | "subagent-start"

yadgar_hook_recall_timeout_total = Counter(
    "yadgar_hook_recall_timeout_total",
    "Total hook recall() calls that exceeded the HOOK_RECALL_TIMEOUT_S latency budget "
    "and returned an empty result. Monitor this counter to tune the timeout default.",
    ["handler"],
    registry=_registry,
)

# ── v5.49.0 — archive retention telemetry ────────────────────────────────────

# ── cold-memory retention DRY-RUN visibility (#29) ───────────────────────────
# Gauge: count of cold immortal user memories that WOULD be purged (candidates).
# Set on every nightly retention pass regardless of COLD_MEMORY_PURGE_ENABLED.
# A non-zero value is an ops signal; rising trend warrants reviewing the config.

yadgar_cold_purge_candidates = Gauge(
    "yadgar_cold_purge_candidates",
    "Count of cold immortal user memories that are retention candidates "
    "(heat<COLD_THRESHOLD, age>COLD_MEMORY_RETENTION_DAYS, access_count=0, "
    "not protected/anchored). Non-zero = candidates exist; real delete gated by "
    "COLD_MEMORY_PURGE_ENABLED=true AND COLD_MEMORY_PURGE_DRY_RUN=false.",
    registry=_registry,
)

yadgar_archive_purged_total = Counter(
    "yadgar_archive_purged_total",
    "Total memory_archive rows deleted by nightly retention purge",
    registry=_registry,
)

yadgar_archive_retention_skipped_total = Counter(
    "yadgar_archive_retention_skipped_total",
    "Total memory_archive rows skipped by nightly retention purge, by skip reason "
    "(reason = protected | anchor | recent)",
    ["reason"],
    registry=_registry,
)

# ── v6 Phase 0.2 — Data-quality gauges (I23 writers in _collect_data_quality) ──
# These gauges are refreshed on every /metrics scrape (alongside queue depths).
# They measure corpus health and are the Prometheus half of the Phase-0.2 spec:
# "Export as Prometheus metrics + a yadgar stats section."

yadgar_data_quality_embedding_valid_ratio = Gauge(
    "yadgar_data_quality_embedding_valid_ratio",
    "Fraction of non-stale memory rows whose embedding IS NOT NULL (0.0–1.0). "
    "A value < 1.0 indicates null-embedding corruption — Phase-1.2 hard invariant target.",
    registry=_registry,
)

yadgar_data_quality_duplicate_rate = Gauge(
    "yadgar_data_quality_duplicate_rate",
    "Near-duplicate density: count of memory_similarity_link edges divided by "
    "total active memories. High values suggest low write-gate threshold or "
    "aggressive ingestion without curation.",
    registry=_registry,
)

yadgar_data_quality_zombie_rate = Gauge(
    "yadgar_data_quality_zombie_rate",
    "Fraction of memories that are stale (is_stale=true). High values indicate "
    "consolidation is not archiving / vacuum is not running.",
    registry=_registry,
)

yadgar_data_quality_domain_coverage = Gauge(
    "yadgar_data_quality_domain_coverage",
    "Fraction of non-stale memories with a non-null domain assignment "
    "(from astrocyte / consolidation). Low values mean domain-consolidation is not firing.",
    registry=_registry,
)

yadgar_data_quality_surprise_p50 = Gauge(
    "yadgar_data_quality_surprise_p50",
    "Median surprise_score across non-stale memories with a non-null surprise_score. "
    "Part of the surprise-distribution summary for Phase-0.2 dashboard.",
    registry=_registry,
)

yadgar_data_quality_surprise_p95 = Gauge(
    "yadgar_data_quality_surprise_p95",
    "95th-percentile surprise_score across non-stale memories with a non-null "
    "surprise_score. Used to detect surprise-score distribution drift.",
    registry=_registry,
)

yadgar_data_quality_null_embedding_count = Gauge(
    "yadgar_data_quality_null_embedding_count",
    "Absolute count of non-stale memory rows with embedding IS NULL. "
    "This is the Phase-0.2 hard-invariant visibility metric: must be 0 in a healthy corpus.",
    registry=_registry,
)

# Stable, bounded set of loop label values (8 loops audited; 2 are not standalone while-True).
# Active labels:
#   metrics_sampler          — graph_api.py sample_system_metrics (lifecycle.py thread)
#   consolidation_daemon     — consolidation/orchestrator.py _daemon_loop
#   queue_drainer            — file_queue QueueDrainer.run
#   viz_health_scraper       — viz_daemon_health.py run_health_scraper
#   sse_event_stream         — server/http.py _make_event_stream (Option A: shared gauge)
#   model_unload             — server/lifecycle.py _reranker_idle_thread
# Skipped (not standalone while-True loops):
#   consolidation_idle_check — branch inside _daemon_loop, not its own loop
#   consolidation_sleep_cycle — one-shot _maybe_sleep_cycle(), not a while-True


def loop_heartbeat(loop: str) -> None:
    """Set the last-run gauge for a background loop.

    Call at the TOP of each loop iteration.
    Never raises — telemetry failures must not compound caller failures.
    """
    try:
        yadgar_loop_last_run_unix_timestamp.labels(loop=loop).set(time.time())
    except Exception:  # noqa: BLE001
        pass


def hook_record_failure(
    hook: str,
    *,
    exc: BaseException | None = None,
    reason: str | None = None,
    status_code: int | None = None,
) -> None:
    """Increment yadgar_hook_failure_total{hook, reason} and delegate to PR-H global counter.

    Priority for reason label:
      1. `reason` kwarg (explicit string)
      2. exc.__class__.__name__ if exc given
      3. str(status_code) if status_code >= 500
    Never raises — telemetry must not compound caller failures.
    """
    try:
        if reason is None:
            if exc is not None:
                reason = exc.__class__.__name__
            elif status_code is not None and status_code >= 500:
                reason = str(status_code)
            else:
                reason = "unknown"
        yadgar_hook_failure_total.labels(hook=hook, reason=reason).inc()
    except Exception:  # noqa: BLE001
        pass
    if exc is not None:
        try:
            from yadgar.exception_telemetry import record_exception  # noqa: PLC0415

            record_exception(f"hook.{hook}", exc)
        except Exception:  # noqa: BLE001
            pass


def loop_record_exception(loop: str, exc: BaseException) -> None:
    """Increment loop-scoped error counter + delegate to PR-H global counter.

    Call in the catch-all except of each background loop.
    Never raises — telemetry failures must not compound caller failures.
    """
    try:
        yadgar_loop_errors_total.labels(loop=loop, error_type=exc.__class__.__name__).inc()
    except Exception:  # noqa: BLE001
        pass
    try:
        from yadgar.exception_telemetry import record_exception  # noqa: PLC0415

        record_exception(f"loop.{loop}", exc)
    except Exception:  # noqa: BLE001
        pass


def _is_metrics_enabled() -> bool:
    """Return True when YADGAR_METRICS_ENABLED is truthy (default: True)."""
    val = os.environ.get("YADGAR_METRICS_ENABLED", "1")
    return val.lower() in ("1", "true", "yes", "on")


def _collect_queue_depths() -> None:
    """Update queue depth and DLQ size gauges from filesystem."""
    try:
        from pathlib import Path

        from yadgar.config import get_settings

        settings = get_settings()
        data_dir = Path(settings.DATA_DIR).expanduser()
        for queue_name in ("queue", "archive", "dlq"):
            q_dir = data_dir / queue_name
            if q_dir.is_dir():
                depth = sum(1 for _ in q_dir.iterdir() if _.suffix == ".json")
                yadgar_queue_depth.labels(queue=queue_name).set(depth)
                if queue_name == "dlq":
                    yadgar_dlq_size.set(depth)
    except Exception:
        pass  # non-fatal


def _dq_count(storage, sql: str) -> int:
    """Execute a COUNT(*) GROUP ALL query and return the integer result."""
    try:
        res = storage._q(sql)
        return res[0][0].get("count", 0) if res and res[0] else 0
    except Exception:  # noqa: BLE001
        return 0


def _dq_set_embedding_gauges(storage, total: int) -> None:
    """Set null-embedding count and valid-ratio gauges."""
    null_emb = _dq_count(
        storage,
        "SELECT count() FROM memory WHERE is_stale = false AND embedding IS NONE GROUP ALL",
    )
    yadgar_data_quality_null_embedding_count.set(null_emb)
    yadgar_data_quality_embedding_valid_ratio.set((total - null_emb) / total if total > 0 else 0.0)


def _parse_surprise_scores(rows) -> list[float]:
    """Extract finite surprise_score floats from a SELECT result (best-effort)."""
    scores: list[float] = []
    if not (rows and rows[0]):
        return scores
    for row in rows[0]:
        sv = row.get("surprise_score")
        if sv is None:
            continue
        try:
            scores.append(float(sv))
        except Exception:  # noqa: BLE001 - skip non-numeric
            pass
    return scores


def _dq_set_surprise_gauges(storage) -> None:
    """Set surprise p50 and p95 gauges."""
    import statistics as _stats  # noqa: PLC0415

    try:
        surp_rows = storage._q(
            "SELECT surprise_score FROM memory "
            "WHERE is_stale = false AND surprise_score IS NOT NONE "
            "AND surprise_score > 0 "
            "LIMIT 5000"
        )
    except Exception:  # noqa: BLE001
        return
    scores = _parse_surprise_scores(surp_rows)
    if not scores:
        return
    yadgar_data_quality_surprise_p50.set(_stats.median(scores))
    p95 = _stats.quantiles(scores, n=20)[18] if len(scores) >= 20 else max(scores)
    yadgar_data_quality_surprise_p95.set(p95)


def _collect_data_quality() -> None:
    """Refresh Phase-0.2 data-quality gauges from the live DB.

    Called on every /metrics scrape.  All DB errors are silently swallowed
    so that a degraded DB doesn't break the metrics endpoint.

    I23 compliance: this is the declared writer for all seven
    yadgar_data_quality_* gauges declared above.
    """
    try:
        import yadgar.server._state as _st  # noqa: PLC0415

        if _st._storage is None:
            return
        storage = _st._storage

        total = _dq_count(storage, "SELECT count() FROM memory WHERE is_stale = false GROUP ALL")
        if total == 0:
            return

        _dq_set_embedding_gauges(storage, total)

        sim_links = _dq_count(storage, "SELECT count() FROM memory_similarity_link GROUP ALL")
        yadgar_data_quality_duplicate_rate.set(sim_links / total)

        stale = _dq_count(storage, "SELECT count() FROM memory WHERE is_stale = true GROUP ALL")
        total_all = total + stale
        yadgar_data_quality_zombie_rate.set(stale / total_all if total_all > 0 else 0.0)

        domain_count = _dq_count(
            storage,
            "SELECT count() FROM memory WHERE is_stale = false AND domain IS NOT NONE GROUP ALL",
        )
        yadgar_data_quality_domain_coverage.set(domain_count / total)

        _dq_set_surprise_gauges(storage)

    except Exception:  # noqa: BLE001
        pass  # non-fatal — data-quality metrics are best-effort


async def metrics_handler(request: Request) -> Response:
    """ASGI handler for the /metrics Prometheus endpoint.

    Returns 404 when YADGAR_METRICS_ENABLED=False.
    Otherwise returns Prometheus text exposition format.
    """
    if not _is_metrics_enabled():
        return Response(
            content="metrics disabled",
            status_code=404,
            media_type="text/plain",
        )

    # Refresh dynamic gauges before scrape
    _collect_queue_depths()
    _collect_process_metrics()
    _collect_data_quality()

    output = generate_latest(_registry)
    return Response(
        content=output,
        status_code=200,
        media_type=CONTENT_TYPE_LATEST,
    )
