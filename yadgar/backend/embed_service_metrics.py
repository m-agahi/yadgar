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
# backend 5.30.1 — queue drainer lifecycle (P0 fix: R3 Car 1 left the drainer
# unconstructed in production; this gauge makes "drainer not running" alertable)
# ---------------------------------------------------------------------------

embed_drainer_running = Gauge(
    "yadgar_embed_queue_drainer_running",
    "1 if the backend QueueDrainer thread is running, 0 if not started / failed / stopped",
    registry=_registry,
)
embed_drainer_running.set(0)

# ---------------------------------------------------------------------------
# v5.6.7 PR-G — idle eviction telemetry
# ---------------------------------------------------------------------------

model_unload_total = Counter(
    "yadgar_embed_model_unload_total",
    "Total idle-eviction unloads per model (ce/nli). Emitted once per .set(0) on model_loaded.",
    ["model"],
    registry=_registry,
)
# Pre-initialise label-sets so they appear in /metrics from first scrape
for _m in ("ce", "nli"):
    model_unload_total.labels(model=_m)

model_load_duration_seconds = Histogram(
    "yadgar_embed_model_load_duration_seconds",
    "Wall-clock duration of each model cold-load (first construction of the handle).",
    ["model"],
    # Buckets: 1ms to 30s — cold model loads on CPU range from ~50ms (CE) to ~3s (NLI deberta)
    buckets=(0.001, 0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
    registry=_registry,
)
for _m in ("ce", "nli"):
    model_load_duration_seconds.labels(model=_m)


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
# v5.3.0 — /admin/dbsize cache hit/miss observability
# ---------------------------------------------------------------------------

embed_dbsize_cache_hits_total = Counter(
    "yadgar_embed_dbsize_cache_hits_total",
    "Total /admin/dbsize responses served from in-memory cache (no os.walk).",
    registry=_registry,
)

embed_dbsize_cache_misses_total = Counter(
    "yadgar_embed_dbsize_cache_misses_total",
    "Total /admin/dbsize responses that triggered a fresh os.walk recompute.",
    registry=_registry,
)

# ---------------------------------------------------------------------------
# v5.3.0 — Backend restart cause attribution
# ---------------------------------------------------------------------------

embed_restart_reason_total = Counter(
    "yadgar_embed_restart_reason_total",
    "Total backend start events bucketed by shutdown reason detected at startup.",
    ["reason"],
    registry=_registry,
)
# Pre-initialise all label-sets so the metric appears from first scrape
for _reason in ("clean", "crash", "first_boot"):
    embed_restart_reason_total.labels(reason=_reason)

# ---------------------------------------------------------------------------
# backend v5.4.0 — CE + embed LRU cache observability
# ---------------------------------------------------------------------------
# Per-cache counters: hits, misses, evictions, size_entries, size_bytes.
# Shared: snapshot_age_seconds{cache} gauge.
#
# Naming: yadgar_embed_<cache>_cache_<metric> — consistent with I23 naming
# convention (embed-service prefix + cache domain + metric name).

ce_cache_hits_total = Counter(
    "yadgar_embed_ce_cache_hits_total",
    "Total CE score cache hits (key found in LRU cache, ML inference skipped).",
    registry=_registry,
)

ce_cache_misses_total = Counter(
    "yadgar_embed_ce_cache_misses_total",
    "Total CE score cache misses (key absent, ML inference required).",
    registry=_registry,
)

ce_cache_evictions_total = Counter(
    "yadgar_embed_ce_cache_evictions_total",
    "Total CE cache LRU evictions (oldest entry removed to make room).",
    registry=_registry,
)

ce_cache_size_entries = Gauge(
    "yadgar_embed_ce_cache_size_entries",
    "Current number of entries in the CE score LRU cache.",
    registry=_registry,
)

ce_cache_size_bytes = Gauge(
    "yadgar_embed_ce_cache_size_bytes",
    "Approximate memory footprint of the CE score LRU cache (sys.getsizeof of internal dict).",
    registry=_registry,
)

embed_cache_hits_total = Counter(
    "yadgar_embed_embed_cache_hits_total",
    "Total embedding vector cache hits (text already encoded, re-encode skipped).",
    registry=_registry,
)

embed_cache_misses_total = Counter(
    "yadgar_embed_embed_cache_misses_total",
    "Total embedding vector cache misses (text not in cache, encode required).",
    registry=_registry,
)

embed_cache_evictions_total = Counter(
    "yadgar_embed_embed_cache_evictions_total",
    "Total embed cache LRU evictions (oldest entry removed to make room).",
    registry=_registry,
)

embed_cache_size_entries = Gauge(
    "yadgar_embed_embed_cache_size_entries",
    "Current number of entries in the embedding vector LRU cache.",
    registry=_registry,
)

embed_cache_size_bytes = Gauge(
    "yadgar_embed_embed_cache_size_bytes",
    "Approximate memory footprint of the embedding vector LRU cache.",
    registry=_registry,
)

cache_snapshot_age_seconds = Gauge(
    "yadgar_embed_cache_snapshot_age_seconds",
    "Seconds since the named cache snapshot was last written (-1 = no snapshot).",
    ["cache"],
    registry=_registry,
)
# Pre-initialise label-sets so gauges appear from first scrape
for _cache_name in ("ce", "embed"):
    cache_snapshot_age_seconds.labels(cache=_cache_name).set(-1)

# ---------------------------------------------------------------------------
# Car 0 (backend v5.14) — dual-emit CE/embed onto the generic {cache=} family
# ---------------------------------------------------------------------------
# The old bespoke `yadgar_embed_ce_cache_*` / `yadgar_embed_embed_cache_*`
# counters above stay STATICALLY declared and inline-incremented in
# embed_service.py, UNTOUCHED (behavior-neutral). Car 0 ADDS the generic
# `yadgar_cache_{hit,miss,evictions}_total{cache=<name>}` + `_size_entries`
# series via a scrape-time collector that reads the LRUCache internal ints
# (hits/misses/evictions/size_entries). No per-get label-inc → zero added
# latency on the per-passage CE loop.
#
# ⚠ Collision guard (dual-emit resolution A): this collector emits ONLY the NEW
# generic series. It must NOT re-yield the old bespoke names — re-yielding an
# already-statically-declared counter in the same process = duplicate `# TYPE`
# at scrape → Prometheus rejects the whole scrape. The generic
# `yadgar_cache_hit_total` is declared statically only CORE-side
# (yadgar/metrics.py), never here, so this collector is its sole backend emitter.


def _default_backend_cache_instances() -> dict:
    """Return {name: Cache} for EVERY registered backend cache namespace.

    Enumerates the shared ``yadgar.backend.cache._REGISTRY`` — the single source of
    truth every ``Cache`` self-registers into at construction (``ce``, ``embed``
    from ``embed_service`` import; ``memory_doc``, ``engram_slot``, ``graph``
    lazily on the first recall that hits their seam). Reading the registry (rather
    than the two hard-coded module globals) is what surfaces the recall-path DATA
    caches at backend ``/metrics``: they are ``obs_tier="cold"`` so their
    ``record_cache_*`` calls land in the CORE-scraped ``yadgar.metrics`` registry,
    invisible here — this scrape-time collector is their only backend emitter.

    The three data caches are eagerly ensured (idempotent factories) so their
    series appear with 0 values even before the first recall registers them —
    deterministic, always-present metrics instead of appear-on-first-hit.

    Read at SCRAPE time only. A degraded import must never break ``/metrics``.
    """
    from yadgar.backend.cache import _REGISTRY  # noqa: PLC0415

    # Importing embed_service registers the ce/embed namespaces at its module
    # import (``_ce_cache = _make_ce_cache()``) — force it so both are present in
    # ``_REGISTRY`` regardless of import order (preserves the pre-fix guarantee).
    try:
        import yadgar.backend.embed_service  # noqa: F401,PLC0415
    except Exception:  # noqa: BLE001 — a degraded backend must not break /metrics
        pass

    # Eagerly ensure the recall-path data caches are registered so their series
    # are always present (0-valued until first hit), not appear-on-first-recall.
    for _factory_name in (
        "get_memory_doc_cache",
        "get_engram_slot_cache",
        "get_graph_cache",
    ):
        try:
            import yadgar.backend.cache as _cache_mod  # noqa: PLC0415

            getattr(_cache_mod, _factory_name)()
        except Exception:  # noqa: BLE001 — a degraded cache must not break /metrics
            pass

    return dict(_REGISTRY)


class CacheStatsCollector:
    """Scrape-time collector: emits the generic {cache=} cache series off the
    backend LRUCache internal ints. Registered on the backend `_registry`.

    Emits ONLY the new generic names (never the old bespoke ones) — see the
    collision guard above.
    """

    def __init__(self, instances_fn=_default_backend_cache_instances) -> None:
        self._instances_fn = instances_fn

    def collect(self):  # noqa: D401 — prometheus_client Collector protocol
        from prometheus_client.core import (  # noqa: PLC0415
            CounterMetricFamily,
            GaugeMetricFamily,
        )

        hit = CounterMetricFamily(
            "yadgar_cache_hit_total", "Total cache hits by cache name", labels=["cache"]
        )
        miss = CounterMetricFamily(
            "yadgar_cache_miss_total", "Total cache misses by cache name", labels=["cache"]
        )
        evict = CounterMetricFamily(
            "yadgar_cache_evictions_total",
            "Total cache evictions by cache name",
            labels=["cache"],
        )
        size = GaugeMetricFamily(
            "yadgar_cache_size_entries",
            "Current number of entries in each cache, by cache name",
            labels=["cache"],
        )
        try:
            instances = self._instances_fn() or {}
        except Exception:  # noqa: BLE001
            instances = {}
        for name, cache in instances.items():
            if cache is None:
                continue
            hit.add_metric([name], getattr(cache, "hits", 0))
            miss.add_metric([name], getattr(cache, "misses", 0))
            evict.add_metric([name], getattr(cache, "evictions", 0))
            size.add_metric([name], getattr(cache, "size_entries", 0))
        yield hit
        yield miss
        yield evict
        yield size


# Register the dual-emit collector on the backend registry (idempotent per-import).
_registry.register(CacheStatsCollector())


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
