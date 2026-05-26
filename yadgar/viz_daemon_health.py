"""Daemon health scraper + aggregated JSON endpoint (V1c, v5.6.0).

Exposes GET /api/daemon-health — returns aggregated metrics from both daemons.
A background scraper (started by server lifespan) refreshes every 5 s.

Architecture decision (2026-05-22, v5.6.0 V1c):
  Server-side scraping (Option A) — viz_server is a thin reverse proxy;
  all logic belongs in server/http.py or its own module. CORS avoided.
  Polling (not SSE push) for the new endpoint — the existing SSE channel
  already emits `daemon_health` events; /api/daemon-health is a REST
  fallback for debug tab parity and initial page load.

TODO(V1d): replace hardcoded 5-second cadence with
    YADGAR_VIZ_HEALTH_REFRESH_SEC env var.

I13 compliance: functions ≤15 cyclomatic, ≤150 LOC hard, ≤80 LOC soft.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import UTC, datetime
from typing import Any

import httpx
from prometheus_client.parser import text_string_to_metric_families
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

# Hardcoded refresh cadence — V1d will make this configurable.
_SCRAPE_INTERVAL_S: float = 5.0
_BACKEND_TIMEOUT_S: float = 3.0
_BACKEND_DEFAULT_URL: str = "http://yadgar-backend:8001"


def _get_backend_metrics_url() -> str:
    """Resolve backend /metrics URL from env vars.

    Priority:
      1. YADGAR_BACKEND_METRICS_URL — explicit override (local dev, testing)
      2. YADGAR_EMBED_URL — already set in container; append /metrics
      3. Hardcoded default: http://yadgar-backend:8001/metrics
    """
    override = os.environ.get("YADGAR_BACKEND_METRICS_URL")
    if override:
        return override
    embed_url = os.environ.get("YADGAR_EMBED_URL")
    if embed_url:
        return embed_url.rstrip("/") + "/metrics"
    return _BACKEND_DEFAULT_URL + "/metrics"


# Module-level cache — written by background scraper, read by endpoint handler.
_health_cache: dict[str, Any] | None = None

# Background scraper task handle — guards against double-start.
_scraper_task: asyncio.Task | None = None  # type: ignore[type-arg]

# Previous CPU samples for per-daemon rate computation.
_core_prev_cpu_s: float | None = None
_core_prev_cpu_t: float | None = None
_backend_prev_cpu_s: float | None = None
_backend_prev_cpu_t: float | None = None


# ---------------------------------------------------------------------------
# Metric text scraping
# ---------------------------------------------------------------------------


async def scrape_backend_metrics_text(url: str) -> tuple[str | None, str | None]:
    """Fetch /metrics text from the backend. Returns (text, error_msg)."""
    try:
        async with httpx.AsyncClient(timeout=_BACKEND_TIMEOUT_S) as client:
            resp = await client.get(url)
        if resp.status_code != 200:
            return None, f"HTTP {resp.status_code}"
        return resp.text, None
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


def _sample_value(families: dict, family_name: str, labels: dict | None = None) -> float | None:
    """Extract first matching sample value from parsed metric families.

    Args:
        families: dict of {name: MetricFamily} from text_string_to_metric_families.
        family_name: prometheus metric family name.
        labels: if given, filter samples to those matching all label key/values.

    Returns:
        float value or None if metric/sample not found.
    """
    fam = families.get(family_name)
    if fam is None:
        return None
    for s in fam.samples:
        if labels is None or all(s.labels.get(k) == v for k, v in labels.items()):
            return s.value
    return None


def _labeled_values(families: dict, family_name: str, label_key: str) -> dict[str, float]:
    """Return {label_value: sample_value} for all samples of a metric family."""
    fam = families.get(family_name)
    if fam is None:
        return {}
    result: dict[str, float] = {}
    for s in fam.samples:
        lv = s.labels.get(label_key)
        if lv is not None:
            result[lv] = s.value
    return result


def _cpu_pct(families: dict, prev_cpu_s: float | None, prev_cpu_t: float | None) -> float | None:
    """Compute CPU % from process_cpu_seconds_total rate over last interval."""
    fam = families.get("process_cpu_seconds")
    if fam is None:
        return None
    cpu_s: float | None = None
    for s in fam.samples:
        if s.name == "process_cpu_seconds_total":
            cpu_s = s.value
            break
    if cpu_s is None or prev_cpu_s is None or prev_cpu_t is None:
        return None
    dt = time.time() - prev_cpu_t
    if dt <= 0:
        return None
    return round((cpu_s - prev_cpu_s) / dt * 100.0, 1)


def _histogram_p95(
    families: dict, family_name: str, extra_labels: dict | None = None
) -> float | None:
    """Estimate p95 from histogram buckets (linear interpolation at 95th count).

    Returns None if metric not found or insufficient data.
    """
    fam = families.get(family_name)
    if fam is None:
        return None
    # Collect bucket samples (le != +Inf) for matching extra_labels.
    buckets: list[tuple[float, float]] = []
    total_count: float | None = None
    for s in fam.samples:
        if extra_labels and not all(s.labels.get(k) == v for k, v in extra_labels.items()):
            continue
        if s.name.endswith("_count"):
            total_count = s.value
        elif s.name.endswith("_bucket"):
            le = s.labels.get("le", "+Inf")
            if le != "+Inf":
                buckets.append((float(le), s.value))
    if not buckets or total_count is None or total_count == 0:
        return None
    buckets.sort(key=lambda x: x[0])
    target = total_count * 0.95
    for le_val, count in buckets:
        if count >= target:
            return round(le_val, 3)
    return buckets[-1][0] if buckets else None


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def _parse_process(families: dict, prev_cpu_s: float | None, prev_cpu_t: float | None) -> dict:
    """Extract process-level fields from standard prometheus process_* metrics.

    Used for backend (prometheus_client default ProcessCollector).
    """
    rss = _sample_value(families, "process_resident_memory_bytes")
    fds = _sample_value(families, "process_open_fds")
    start = _sample_value(families, "process_start_time_seconds")
    uptime = round(time.time() - start, 1) if start is not None else None
    cpu = _cpu_pct(families, prev_cpu_s, prev_cpu_t)
    return {
        "rss_bytes": int(rss) if rss is not None else None,
        "open_fds": int(fds) if fds is not None else None,
        "uptime_s": uptime,
        "cpu_pct": cpu,
    }


def _parse_core_process(families: dict) -> dict:
    """Extract process-level fields from core's yadgar_process_* gauges.

    Core uses an isolated CollectorRegistry with custom gauges (not the default
    prometheus_client ProcessCollector), so metric names differ from backend.
    cpu_pct is read directly from yadgar_process_cpu_percent gauge.
    """
    rss = _sample_value(families, "yadgar_process_rss_bytes")
    fds = _sample_value(families, "yadgar_process_open_fds")
    cpu = _sample_value(families, "yadgar_process_cpu_percent")
    return {
        "rss_bytes": int(rss) if rss is not None else None,
        "open_fds": int(fds) if fds is not None else None,
        "uptime_s": None,  # no process_start_time in core registry
        "cpu_pct": round(float(cpu), 1) if cpu is not None else None,
    }


def _parse_log(families: dict) -> dict:
    """Extract log-metric fields from parsed metric families."""
    size_bytes: float | None = None
    rotations: float | None = None
    dropped_total: float = 0.0
    fam_size = families.get("yadgar_log_file_size_bytes")
    if fam_size:
        for s in fam_size.samples:
            size_bytes = s.value
            break
    # Counter family name strips _total suffix → yadgar_log_file_rotations
    fam_rot = families.get("yadgar_log_file_rotations")
    if fam_rot:
        for s in fam_rot.samples:
            if s.name.endswith("_total"):
                rotations = s.value
                break
    fam_drop = families.get("yadgar_log_dropped")
    if fam_drop:
        for s in fam_drop.samples:
            if s.name.endswith("_total"):
                dropped_total += s.value
    return {
        "file_size_bytes": int(size_bytes) if size_bytes is not None else None,
        "rotations_total": int(rotations) if rotations is not None else None,
        "dropped_total": int(dropped_total),
    }


def parse_core_metrics(
    text: str,
    prev_cpu_s: float | None,
    prev_cpu_t: float | None,
) -> dict:
    """Parse core /metrics text into structured core health dict.

    Returns dict with process, log, circuit_breakers, queue keys.
    """
    if not text:
        return {
            "process": {"rss_bytes": None, "open_fds": None, "uptime_s": None, "cpu_pct": None},
            "log": {"file_size_bytes": None, "rotations_total": None, "dropped_total": 0},
            "circuit_breakers": {},
            "queue": {"depth": None, "dlq_size": None, "drainer_lag_p95_ms": None},
        }
    families = {f.name: f for f in text_string_to_metric_families(text)}
    cb_raw = _labeled_values(families, "yadgar_circuit_breaker_state", "endpoint")
    cb = {ep: int(v) for ep, v in cb_raw.items()}
    fam_q = families.get("yadgar_queue_depth")
    depth: float | None = None
    if fam_q:
        for s in fam_q.samples:
            depth = (depth or 0) + s.value
    dlq = _sample_value(families, "yadgar_dlq_size")
    lag_p95 = _histogram_p95(families, "yadgar_drainer_lag_ms")
    return {
        "process": _parse_core_process(families),
        "log": _parse_log(families),
        "circuit_breakers": cb,
        "queue": {
            "depth": int(depth) if depth is not None else None,
            "dlq_size": int(dlq) if dlq is not None else None,
            "drainer_lag_p95_ms": lag_p95,
        },
    }


def parse_backend_metrics(
    text: str,
    prev_cpu_s: float | None,
    prev_cpu_t: float | None,
) -> dict:
    """Parse backend /metrics text into structured backend health dict.

    Returns dict with process, log, rerank, models keys.
    """
    if not text:
        return {
            "process": {"rss_bytes": None, "open_fds": None, "uptime_s": None, "cpu_pct": None},
            "log": {"file_size_bytes": None, "rotations_total": None, "dropped_total": 0},
            "rerank": {"requests_total": {}, "errors_503": {}, "semaphore_held": {}},
            "models": {},
        }
    families = {f.name: f for f in text_string_to_metric_families(text)}
    # Counter family names: strip _total → yadgar_embed_rerank_requests
    req_raw = _labeled_values(families, "yadgar_embed_rerank_requests", "mode")
    err_raw = _labeled_values(families, "yadgar_embed_rerank_503", "mode")
    sem_raw = _labeled_values(families, "yadgar_embed_rerank_semaphore_held", "mode")
    models_raw = _labeled_values(families, "yadgar_embed_model_loaded", "model")
    return {
        "process": _parse_process(families, prev_cpu_s, prev_cpu_t),
        "log": _parse_log(families),
        "rerank": {
            "requests_total": {k: int(v) for k, v in req_raw.items()},
            "errors_503": {k: int(v) for k, v in err_raw.items()},
            "semaphore_held": {k: int(v) for k, v in sem_raw.items()},
        },
        "models": {k: int(v) for k, v in models_raw.items()},
    }


# ---------------------------------------------------------------------------
# Background scraper
# ---------------------------------------------------------------------------


def _scrape_core_text() -> str:
    """Synchronous: generate core /metrics text from local registry."""
    from prometheus_client import generate_latest

    from yadgar.metrics import _registry  # type: ignore[attr-defined]

    return generate_latest(_registry).decode("utf-8")


async def _scrape_once() -> None:
    """One scrape cycle: fetch both daemons, parse, update cache."""
    global _health_cache, _core_prev_cpu_s, _core_prev_cpu_t
    global _backend_prev_cpu_s, _backend_prev_cpu_t

    now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Core: generate locally.
    try:
        core_text = await asyncio.to_thread(_scrape_core_text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("viz_daemon_health: core scrape error: %s", exc)
        core_text = ""

    core_data = parse_core_metrics(core_text, _core_prev_cpu_s, _core_prev_cpu_t)

    # Update core CPU prev values.
    fams_core = {f.name: f for f in text_string_to_metric_families(core_text)} if core_text else {}
    fam_cpu = fams_core.get("process_cpu_seconds")
    if fam_cpu:
        for s in fam_cpu.samples:
            if s.name == "process_cpu_seconds_total":
                _core_prev_cpu_s = s.value
                _core_prev_cpu_t = time.time()
                break

    # Backend: HTTP scrape.
    backend_text, backend_err = await scrape_backend_metrics_text(_get_backend_metrics_url())
    if backend_err is not None:
        logger.debug("viz_daemon_health: backend scrape error: %s", backend_err)
        backend_data: dict = {"unavailable": True, "error": backend_err}
    else:
        backend_data = parse_backend_metrics(
            backend_text or "", _backend_prev_cpu_s, _backend_prev_cpu_t
        )
        fams_back = (
            {f.name: f for f in text_string_to_metric_families(backend_text)}
            if backend_text
            else {}
        )
        fam_bcpu = fams_back.get("process_cpu_seconds")
        if fam_bcpu:
            for s in fam_bcpu.samples:
                if s.name == "process_cpu_seconds_total":
                    _backend_prev_cpu_s = s.value
                    _backend_prev_cpu_t = time.time()
                    break

    _health_cache = {
        "core": core_data,
        "backend": backend_data,
        "scraped_at": now_iso,
    }


def _scraper_heartbeat() -> None:
    """PR-I: heartbeat helper — no nesting added to run_health_scraper."""
    try:
        from yadgar.metrics import loop_heartbeat  # noqa: PLC0415

        loop_heartbeat("viz_health_scraper")
    except Exception:  # noqa: BLE001
        pass


def _scraper_record_exc(exc: BaseException) -> None:
    """PR-I: error counter helper — no nesting added to run_health_scraper."""
    try:
        from yadgar.metrics import loop_record_exception  # noqa: PLC0415

        loop_record_exception("viz_health_scraper", exc)
    except Exception:  # noqa: BLE001
        pass


async def run_health_scraper() -> None:
    """Background loop — scrapes every _SCRAPE_INTERVAL_S seconds.

    Call once at server lifespan startup. Runs until task cancelled.
    """
    while True:
        _scraper_heartbeat()  # PR-I: heartbeat at top of every iteration
        try:
            await _scrape_once()
        except Exception as exc:  # noqa: BLE001
            logger.warning("viz_daemon_health: scrape cycle error: %s", exc)
            _scraper_record_exc(exc)  # PR-I: loop error counter
        await asyncio.sleep(_SCRAPE_INTERVAL_S)


# ---------------------------------------------------------------------------
# HTTP endpoint
# ---------------------------------------------------------------------------

_PLACEHOLDER: dict = {
    "core": {
        "process": {"rss_bytes": None, "open_fds": None, "uptime_s": None, "cpu_pct": None},
        "log": {"file_size_bytes": None, "rotations_total": None, "dropped_total": 0},
        "circuit_breakers": {},
        "queue": {"depth": None, "dlq_size": None, "drainer_lag_p95_ms": None},
    },
    "backend": {
        "process": {"rss_bytes": None, "open_fds": None, "uptime_s": None, "cpu_pct": None},
        "log": {"file_size_bytes": None, "rotations_total": None, "dropped_total": 0},
        "rerank": {"requests_total": {}, "errors_503": {}, "semaphore_held": {}},
        "models": {},
    },
    "scraped_at": None,
}


def _ensure_scraper_running() -> None:
    """Start the background scraper task if not already running."""
    global _scraper_task
    if _scraper_task is not None and not _scraper_task.done():
        return
    try:
        loop = asyncio.get_event_loop()
        _scraper_task = loop.create_task(run_health_scraper(), name="viz-daemon-health-scraper")
        logger.info("viz_daemon_health: background scraper started")
    except RuntimeError:
        # No event loop — test or non-async context. Skip.
        pass


async def api_daemon_health(request: Request) -> JSONResponse:
    """GET /api/daemon-health — returns latest aggregated daemon health JSON.

    Returns cached snapshot from background scraper (refreshed every 5s).
    Returns placeholder with nulls when cache is empty (first tick not yet done).
    Always 200 — presence of backend.unavailable=True signals degraded backend.
    Lazily starts the background scraper on first call.
    """
    _ensure_scraper_running()
    payload = _health_cache if _health_cache is not None else _PLACEHOLDER
    return JSONResponse(payload, headers={"Cache-Control": "no-cache"})


# ---------------------------------------------------------------------------
# Route registration (V1c) — registered here so http.py stays within I13 LOC cap.
# server/__init__.py imports this module after http.py to register the route.
# ---------------------------------------------------------------------------


def _register_routes() -> None:
    """Register /api/daemon-health on the FastMCP server.

    Called once at module import via server/__init__.py. Lazy import of
    mcp_server avoids circular imports (http.py → _app.py → this module).
    """
    from yadgar.server._app import mcp_server  # noqa: PLC0415

    @mcp_server.custom_route("/api/daemon-health", methods=["GET"])
    async def _api_daemon_health_route(request: Request) -> JSONResponse:
        """GET /api/daemon-health — V1c viz daemon sidebar aggregated health."""
        return await api_daemon_health(request)


_register_routes()
