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


def _is_metrics_enabled() -> bool:
    """Return True when YADGAR_METRICS_ENABLED is truthy (default: True)."""
    val = os.environ.get("YADGAR_METRICS_ENABLED", "1")
    return val.lower() in ("1", "true", "yes", "on")


def _collect_queue_depths() -> None:
    """Update queue depth gauges from filesystem."""
    try:
        from yadgar.config import get_settings

        settings = get_settings()
        from pathlib import Path

        data_dir = Path(settings.DATA_DIR).expanduser()
        for queue_name in ("queue", "archive", "dlq"):
            q_dir = data_dir / queue_name
            if q_dir.is_dir():
                depth = sum(1 for _ in q_dir.iterdir() if _.suffix == ".json")
                yadgar_queue_depth.labels(queue=queue_name).set(depth)
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

    output = generate_latest(_registry)
    return Response(
        content=output,
        status_code=200,
        media_type=CONTENT_TYPE_LATEST,
    )
