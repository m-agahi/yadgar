"""Prometheus metrics endpoint for Yadgar.

Gated by YADGAR_METRICS_ENABLED (default True).
Exposed at /metrics — unauthenticated on loopback per §2 design.

Collectors:
- yadgar_queue_depth{queue}       Gauge   — items in queue/, archive/, dlq/
- yadgar_requests_total{route}    Counter — requests by route
- yadgar_consolidation_duration_seconds{phase}  Histogram — cycle phase timing
- yadgar_db_query_duration_seconds  Histogram   — DB query latency (p50/p95)
- yadgar_embedding_cache_hits_total Counter     — embedding cache hits
- yadgar_embedding_cache_misses_total Counter   — embedding cache misses
- yadgar_action_batch_size         Histogram    — action-batch size
- yadgar_tool_token_estimate_total{tool}  Counter — estimated tokens returned per tool call
- yadgar_cache_hit_total{cache}    Counter — cache hits by cache name
- yadgar_cache_miss_total{cache}   Counter — cache misses by cache name
"""

from __future__ import annotations

import os
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

# DB query latency histogram (p50/p95 readable via quantile buckets)
yadgar_db_query_duration_seconds = Histogram(
    "yadgar_db_query_duration_seconds",
    "SurrealDB query round-trip latency in seconds",
    buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0),
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

# ── P11 — Backend liveness + circuit breaker (N3) ────────────────────────────

yadgar_backend_reachable = Gauge(
    "yadgar_backend_reachable",
    "Backend endpoint reachability (1=reachable, 0=unreachable)",
    ["endpoint"],
    registry=_registry,
)

yadgar_circuit_breaker_state = Gauge(
    "yadgar_circuit_breaker_state",
    "Circuit breaker state per endpoint (0=closed, 1=half_open, 2=open)",
    ["endpoint"],
    registry=_registry,
)


def _collect_process_metrics() -> None:
    """Sample process RSS, CPU, and FD count into gauges. Non-fatal."""
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF)
        # ru_maxrss: bytes on Linux, kilobytes on macOS — normalise to bytes
        import platform

        rss = usage.ru_maxrss
        if platform.system() != "Linux":
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


def _collect_circuit_breaker_states() -> None:
    """Read circuit breaker states from RemoteMLClient and update gauge. Non-fatal."""
    try:
        import yadgar.server._state as _st  # noqa: PLC0415
        from yadgar.ml_client import RemoteMLClient  # noqa: PLC0415

        ml = getattr(_st, "_ml_client", None)
        if not isinstance(ml, RemoteMLClient):
            return
        _state_map = {"closed": 0, "half_open": 1, "open": 2}
        for ep_name in ("ce", "nli", "pair"):
            cb = getattr(ml, f"_cb_{ep_name}", None)
            if cb is not None:
                val = _state_map.get(cb._state, 0)
                yadgar_circuit_breaker_state.labels(endpoint=ep_name).set(val)
    except Exception:
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
    _collect_circuit_breaker_states()

    output = generate_latest(_registry)
    return Response(
        content=output,
        status_code=200,
        media_type=CONTENT_TYPE_LATEST,
    )
