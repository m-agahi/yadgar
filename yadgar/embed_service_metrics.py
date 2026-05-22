"""Prometheus metrics for yadgar-embed backend service (V1a, v5.5.0).

Exposed at GET /metrics — unauthenticated (Prometheus scrapers can't easily
carry bearer tokens; matches core /metrics pattern in yadgar/server/http.py §15).
Always on — no YADGAR_METRICS_ENABLED gate needed; overhead is negligible
(<1µs per observe) and the endpoint provides no sensitive data. I3 opt-in
short-circuit considered and rejected: no reason to gate a lightweight,
always-safe endpoint.

Metric families (F5-A semaphore observability + model state + process):

  yadgar_embed_rerank_requests_total{mode}         Counter  — requests received
  yadgar_embed_rerank_503_total{mode}              Counter  — semaphore-busy 503s
  yadgar_embed_rerank_duration_seconds{mode}       Histogram — inference latency
  yadgar_embed_rerank_semaphore_held{mode}         Gauge    — current held slots
  yadgar_embed_model_loaded{model}                 Gauge    — 1=loaded, 0=not

Process metrics (process_*, python_info) come from ProcessCollector and
PlatformCollector registered on the same isolated registry.

Registry is isolated (CollectorRegistry()) so tests importing this module
do not cross-contaminate each other's counters. Tests assert on deltas,
not absolute values.
"""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    PlatformCollector,
    ProcessCollector,
    generate_latest,
)
from starlette.requests import Request
from starlette.responses import Response

# ---------------------------------------------------------------------------
# Isolated registry — prevents cross-test contamination and avoids polluting
# prometheus_client's default REGISTRY (which the core process may also use).
# ---------------------------------------------------------------------------
_registry = CollectorRegistry()

# Register process + platform collectors on the same registry
ProcessCollector(registry=_registry)
PlatformCollector(registry=_registry)

# ---------------------------------------------------------------------------
# F5-A semaphore observability
# ---------------------------------------------------------------------------

rerank_requests_total = Counter(
    "yadgar_embed_rerank_requests_total",
    "Total rerank requests received per mode (ce/nli/pair)",
    ["mode"],
    registry=_registry,
)
# Pre-initialise all mode label-sets so metric families appear from first scrape
for _mode in ("ce", "nli", "pair"):
    rerank_requests_total.labels(mode=_mode)

rerank_503_total = Counter(
    "yadgar_embed_rerank_503_total",
    "Total semaphore-busy 503 responses per mode (ce/nli/pair)",
    ["mode"],
    registry=_registry,
)
for _mode in ("ce", "nli", "pair"):
    rerank_503_total.labels(mode=_mode)

rerank_duration_seconds = Histogram(
    "yadgar_embed_rerank_duration_seconds",
    "Rerank inference latency in seconds (post-semaphore-acquire to release)",
    ["mode"],
    # Buckets sized for ML inference: 1ms to 30s
    buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
    registry=_registry,
)
for _mode in ("ce", "nli", "pair"):
    rerank_duration_seconds.labels(mode=_mode)

rerank_semaphore_held = Gauge(
    "yadgar_embed_rerank_semaphore_held",
    "Number of currently held semaphore slots per mode (informational)",
    ["mode"],
    registry=_registry,
)
for _mode in ("ce", "nli", "pair"):
    rerank_semaphore_held.labels(mode=_mode).set(0)

# ---------------------------------------------------------------------------
# Model state
# ---------------------------------------------------------------------------

model_loaded = Gauge(
    "yadgar_embed_model_loaded",
    "1 if model is loaded and ready, 0 if not (model label: ce/nli/pair/embedding)",
    ["model"],
    registry=_registry,
)

# Initialise all model gauges to 0 so they appear in /metrics from first scrape
for _m in ("ce", "nli", "pair", "embedding"):
    model_loaded.labels(model=_m).set(0)


# ---------------------------------------------------------------------------
# v5.5.1 — Log system observability (backend)
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# ASGI handler
# ---------------------------------------------------------------------------


async def metrics_handler(request: Request) -> Response:
    """ASGI handler for GET /metrics.

    Unauthenticated — Prometheus scrapers operate on loopback without bearer
    tokens. Exposes only operational metrics; no PII or sensitive data.
    """
    output = generate_latest(_registry)
    return Response(
        content=output,
        status_code=200,
        media_type=CONTENT_TYPE_LATEST,
    )
