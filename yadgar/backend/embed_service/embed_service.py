"""Embedding microservice — runs in the backend container.

Serves POST /embed for the core container to call.
Serves POST /rerank for ML scoring (cross-encoder, NLI, pair) via LocalMLClient.
GET /health returns 200 only when SurrealDB is also reachable (true readiness signal).
GET /metrics exposes Prometheus metrics (unauthenticated — V1a, v5.5.0).
"""

# I13 note (ADR-0130): this module is an ACCEPTED single-file case over the ≤500
# soft cap — do NOT split it in future audits. The FastAPI `app` + reload-hit route
# handlers must co-reside with the module-level singletons
# (_engine / _reranker / _ce_cache / _embed_cache / _queue_drainer), which MUST
# survive `importlib.reload(embed_service)` for test isolation (route siblings are
# force-reloaded via the _YADGAR_ES_LOADED sentinel). Splitting breaks the
# reload/monkeypatch reach. Kept in the soft baseline ratchet intentionally.

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import numpy as np
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

import yadgar._shared.paths as _paths
from yadgar._shared.config import resolve_knob
from yadgar._shared.observability.observe import observe
from yadgar.backend.embed_service.embed_service_metrics import (
    cache_snapshot_age_seconds as _cache_snapshot_age_seconds,
)
from yadgar.backend.embed_service.embed_service_metrics import (
    ce_cache_evictions_total as _ce_cache_evictions_total,
)
from yadgar.backend.embed_service.embed_service_metrics import (
    ce_cache_hits_total as _ce_cache_hits_total,
)
from yadgar.backend.embed_service.embed_service_metrics import (
    ce_cache_misses_total as _ce_cache_misses_total,
)
from yadgar.backend.embed_service.embed_service_metrics import (
    ce_cache_size_bytes as _ce_cache_size_bytes,
)
from yadgar.backend.embed_service.embed_service_metrics import (
    ce_cache_size_entries as _ce_cache_size_entries,
)
from yadgar.backend.embed_service.embed_service_metrics import (
    embed_cache_evictions_total as _embed_cache_evictions_total,
)
from yadgar.backend.embed_service.embed_service_metrics import (
    embed_cache_hits_total as _embed_cache_hits_total,
)
from yadgar.backend.embed_service.embed_service_metrics import (
    embed_cache_misses_total as _embed_cache_misses_total,
)
from yadgar.backend.embed_service.embed_service_metrics import (
    embed_cache_size_bytes as _embed_cache_size_bytes,
)
from yadgar.backend.embed_service.embed_service_metrics import (
    embed_cache_size_entries as _embed_cache_size_entries,
)
from yadgar.backend.embed_service.embed_service_metrics import (
    embed_dbsize_cache_hits_total as _dbsize_cache_hits,
)
from yadgar.backend.embed_service.embed_service_metrics import (
    embed_dbsize_cache_misses_total as _dbsize_cache_misses,
)
from yadgar.backend.embed_service.embed_service_metrics import (
    embed_restart_reason_total as _restart_reason_total,
)
from yadgar.backend.embed_service.embed_service_metrics import (
    metrics_handler as _metrics_handler,
)
from yadgar.backend.embed_service.embed_service_metrics import (
    model_loaded as _model_loaded,
)
from yadgar.backend.embed_service.embed_service_metrics import (
    rerank_503_total as _rerank_503_total,
)
from yadgar.backend.embed_service.embed_service_metrics import (
    rerank_duration_seconds as _rerank_duration_seconds,
)
from yadgar.backend.embed_service.embed_service_metrics import (
    rerank_requests_total as _rerank_requests_total,
)
from yadgar.backend.embed_service.embed_service_metrics import (
    rerank_semaphore_held as _rerank_semaphore_held,
)

# C1 split (#18): pydantic request/response models live in a sibling module.
# Re-exported here (F401) so ``embed_service.embed_service.<Model>`` keeps
# resolving for every importer + test; embed/rerank routes below use the Embed*/
# Rerank* pair directly, the rest are re-export-only (routes moved to
# embed_service_routes).
from yadgar.backend.embed_service.embed_service_models import (  # noqa: F401
    AdminRequest,
    AdminResponse,
    ConsolidateRequest,
    ConsolidateResponse,
    EmbedRequest,
    EmbedResponse,
    RecallRequest,
    RecallResponse,
    RerankRequest,
    RerankResponse,
    RestoreRequest,
    RestoreResponse,
    VizRequest,
    VizResponse,
)

if TYPE_CHECKING:
    from yadgar._shared.embeddings import EmbeddingEngine
    from yadgar.backend.ml_client import LocalMLClient

logger = logging.getLogger(__name__)

_http_bearer = HTTPBearer(auto_error=False)

# ---------------------------------------------------------------------------
# C1 split (#18): pure config/knob/ckpt/cache-factory helpers live in a sibling.
# Re-exported here so ``embed_service.embed_service.<name>`` keeps resolving for
# tests + callers; the INSTANCES below stay module-level so importlib.reload()
# re-creates them with fresh env values (several tests depend on that).
# ---------------------------------------------------------------------------
from yadgar.backend.embed_service.embed_service_config import (  # noqa: E402, F401
    CE_SCORING_VERSION,  # re-export: tests import from the service module
    _backend_cache_ram_pct,  # re-export
    _cache_snapshot_dir,
    _cache_snapshot_interval_sec,
    _ce_cache_enabled,  # re-export
    _ce_cache_max_entries,  # re-export: tests import from the service module
    _configure_torch_threads,
    _dbsize_cache_ttl,
    _embed_cache_enabled,  # re-export
    _embed_cache_max_entries,  # re-export: tests import from the service module
    _get_ce_checkpoint_hash,
    _get_embed_checkpoint_hash,
    _make_ce_cache,
    _make_embed_cache,
    _make_rerank_semaphores,
    _rerank_acquire_timeout,
    _shutdown_marker_path,
)

# F5-A per-mode inference semaphore INSTANCES (reload() recreates with fresh env).
_rerank_semaphores: dict[str, asyncio.Semaphore] = _make_rerank_semaphores()

# v5.3.0 — /admin/dbsize in-memory cache. Reassigned globals: stay here with
# their writer (admin_dbsize) so importlib.reload() resets both fields.
_dbsize_cache: dict | None = None  # last computed payload (without cache_age_seconds)
_dbsize_cache_ts: float = 0.0  # time.time() when last computed

# backend v5.4.0 — LRU cache INSTANCES (reset on importlib.reload).
_ce_cache = _make_ce_cache()
_embed_cache = _make_embed_cache()


@observe(tier="hot")
def _update_cache_metrics() -> None:
    """Sync cache counters to Prometheus gauges (called periodically + after ops)."""
    try:
        _ce_cache_size_entries.set(_ce_cache.size_entries)
        _ce_cache_size_bytes.set(_ce_cache.size_bytes)
        _embed_cache_size_entries.set(_embed_cache.size_entries)
        _embed_cache_size_bytes.set(_embed_cache.size_bytes)
    except Exception:
        pass


async def _run_cache_snapshot_task() -> None:
    """Background task: save CE + embed caches to disk every snapshot interval."""
    while True:
        interval = _cache_snapshot_interval_sec()
        await asyncio.sleep(interval)
        snap_dir = _cache_snapshot_dir()
        try:
            _ce_cache.save_snapshot(snap_dir, "ce")
            _embed_cache.save_snapshot(snap_dir, "embed")
            _update_cache_metrics()
            # Update snapshot age gauges
            _cache_snapshot_age_seconds.labels(cache="ce").set(
                _ce_cache.snapshot_age_seconds(snap_dir, "ce")
            )
            _cache_snapshot_age_seconds.labels(cache="embed").set(
                _embed_cache.snapshot_age_seconds(snap_dir, "embed")
            )
            logger.info(
                "cache_snapshot_written",
                extra={
                    "event": "cache_snapshot_written",
                    "ce_entries": _ce_cache.size_entries,
                    "embed_entries": _embed_cache.size_entries,
                    "snap_dir": snap_dir,
                },
            )
        except Exception as exc:
            logger.warning("cache_snapshot_task error: %s", exc)


@observe(tier="stage")
async def _run_model_warmup() -> None:
    """Background task: preload rerank models (ce, nli, pair) after startup delay.

    backend v5.5.0 — triggered once at startup if YADGAR_MODEL_PRELOAD=true.
    Models load sequentially, each in a thread-pool executor so the event loop
    is not blocked.  Per-model errors are caught so one failure doesn't abort
    the others.  CancelledError propagates cleanly on lifespan exit.
    """
    from yadgar._shared.config import get_settings  # noqa: PLC0415

    settings = get_settings()
    if not settings.MODEL_PRELOAD:
        return

    delay = settings.MODEL_PRELOAD_DELAY_SEC
    await asyncio.sleep(delay)

    loop = asyncio.get_running_loop()
    reranker = _get_reranker()

    for mode in ("ce", "nli", "pair"):
        t0 = time.monotonic()
        try:
            if mode == "ce":
                await loop.run_in_executor(None, reranker.score_cross_encoder, "warmup", ["warmup"])
            elif mode == "nli":
                await loop.run_in_executor(None, reranker.score_nli, "warmup", ["warmup"])
            else:  # pair
                await loop.run_in_executor(None, reranker.score_pair, "warmup", "warmup")
            duration = time.monotonic() - t0
            logger.info(
                "model_warmup",
                extra={
                    "event": "model_warmup",
                    "model": mode,
                    "outcome": "ok",
                    "duration_s": round(duration, 3),
                },
            )
        except Exception as exc:
            duration = time.monotonic() - t0
            logger.warning(
                "model_warmup",
                extra={
                    "event": "model_warmup",
                    "model": mode,
                    "outcome": "error",
                    "duration_s": round(duration, 3),
                    "error": str(exc),
                },
            )


# ---------------------------------------------------------------------------
# backend 5.30.1 — queue drainer lifecycle (P0 fix). The live handle is a
# reassigned global; it stays HERE (read by /health + drainer tests as
# es._queue_drainer, reset to None on importlib.reload). The construct/start/stop
# functions live in embed_service_lifecycle and write this attribute through the
# module object (imported + re-exported at the bottom of this file).
# ---------------------------------------------------------------------------

_queue_drainer = None  # live QueueDrainer | None — module-level for /health + shutdown


async def _require_admin_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_http_bearer),
) -> None:
    """Dependency: verify bearer token for /admin/* routes.

    Token read from YADGAR_MCP_AUTH_TOKEN. If the env var is unset,
    /admin routes are locked out entirely (fail-secure).
    """
    expected = os.environ.get("YADGAR_MCP_AUTH_TOKEN", "")
    allow_root = os.environ.get("YADGAR_ALLOW_ROOT", "0").lower() in ("1", "true", "yes")
    if allow_root:
        return  # test escape hatch
    if not expected:
        raise HTTPException(status_code=503, detail="Admin token not configured")
    if credentials is None or credentials.credentials != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


_engine: EmbeddingEngine | None = None
_engine_lock = threading.Lock()

_reranker: LocalMLClient | None = None
_reranker_lock = threading.Lock()


@observe(tier="stage")
def _get_engine():
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                from yadgar._shared.embeddings import EmbeddingEngine

                model = resolve_knob(
                    "YADGAR_EMBEDDING_MODEL", "EMBEDDING_MODEL", str, "all-MiniLM-L6-v2"
                )
                _engine = EmbeddingEngine(model)
                _engine._ensure_model()
    return _engine


@observe(tier="stage")
def _get_reranker() -> LocalMLClient:
    global _reranker
    if _reranker is None:
        with _reranker_lock:
            if _reranker is None:
                from yadgar._shared.config import get_settings
                from yadgar.backend.ml_client import LocalMLClient

                _reranker = LocalMLClient(get_settings())
                # Mark all reranker model variants as loaded (lazy-load on first use)
                for _mode in ("ce", "nli", "pair"):
                    _model_loaded.labels(model=_mode).set(1)
    return _reranker


@asynccontextmanager
async def lifespan(app: FastAPI):
    # I14: configure structured logging at backend boot.
    # Format reads from YADGAR_LOG_FORMAT (default 'json' for production).
    _level = resolve_knob("YADGAR_BACKEND_LOG_LEVEL", "BACKEND_LOG_LEVEL", str, "warn").upper()
    from yadgar._shared.observability.log_config import (
        configure_logging as _configure_logging,  # noqa: PLC0415
    )

    _configure_logging(
        log_format=resolve_knob("YADGAR_LOG_FORMAT", "LOG_FORMAT", str, "json"),
        level=_level,
        process="backend",
    )

    # v5.6.3: distributed tracing for backend.
    # setup_tracing initialises LogSpanProcessor + sets global TracerProvider, and
    # (v5.101 R2) activates HTTPXClientInstrumentor itself — backend calls SurrealDB
    # via httpx, so this ensures outbound httpx calls auto-inject W3C traceparent.
    from yadgar._shared.observability.tracing import (
        setup_tracing as _setup_tracing,  # noqa: PLC0415
    )

    _setup_tracing("yadgar-backend")

    # v5.3.0: restart attribution — inspect previous shutdown state before model load.
    _marker = _shutdown_marker_path()
    _db_path = os.environ.get("YADGAR_DB_PATH", str(_paths.DB_PATH))
    _marker_path = Path(_marker)
    _db_dir = Path(_db_path).expanduser()
    if _marker_path.exists():
        _restart_reason = "clean"
        try:
            _marker_path.unlink()
        except OSError:
            pass
    elif _db_dir.exists():
        _restart_reason = "crash"
    else:
        _restart_reason = "first_boot"

    _restart_reason_total.labels(reason=_restart_reason).inc()
    logger.info(
        "backend_started",
        extra={"event": "backend_started", "reason": _restart_reason},
    )

    # T3 Car 3: set torch intra-op threads to the CPU-aware budget BEFORE the
    # first model load, so the batched CE / embedding inference honors the core
    # budget (1 at --cpus 2 = today's behavior; scales above). Process-global.
    _configure_torch_threads()

    # Load model eagerly so /health reflects true readiness
    try:
        await asyncio.to_thread(_get_engine)
        _model_loaded.labels(model="embedding").set(1)
        logger.info("Embedding model loaded")
    except Exception as exc:
        _model_loaded.labels(model="embedding").set(0)
        logger.error("Failed to load embedding model: %s", exc)

    # backend v5.4.0: restore caches from snapshot BEFORE serving first request.
    _snap_dir = _cache_snapshot_dir()
    try:
        await asyncio.to_thread(_ce_cache.load_snapshot, _snap_dir, "ce")
        await asyncio.to_thread(_embed_cache.load_snapshot, _snap_dir, "embed")
        _update_cache_metrics()
        logger.info(
            "cache_restored",
            extra={
                "event": "cache_restored",
                "ce_entries": _ce_cache.size_entries,
                "embed_entries": _embed_cache.size_entries,
            },
        )
    except Exception as _exc:
        logger.warning("cache restore failed: %s", _exc)

    # backend 5.30.1 (P0): start the queue drainer — the R3 Car 1 write-half.
    # Blocking construction (engine init + thread start) runs in a worker
    # thread so the event loop stays free; awaited so the drainer is running
    # (or fail-loud logged) before the app reports ready.
    await asyncio.to_thread(_start_queue_drainer)

    # viz-render-perf (Car A): warm the graph-layout cache on boot when empty so a
    # fresh deploy renders the viz pre-laid-out instead of the slow client cold
    # layout on the first load. Non-blocking (daemon thread) + non-fatal. Storage
    # is up now (_start_queue_drainer ran _ensure_recall_engines).
    try:
        from yadgar._shared.runtime.lifecycle import _get_storage as _get_storage_boot

        _bootstrap_graph_layout_if_empty(_get_storage_boot())
    except Exception as _exc:  # noqa: BLE001 — bootstrap kick must not block boot
        logger.warning("graph_layout_bootstrap kick failed (non-fatal): %s", _exc)

    # Start periodic snapshot background task (ExceptionGroup-safe: task is
    # cancelled on lifespan exit).
    _snap_task = asyncio.create_task(_run_cache_snapshot_task())
    # backend v5.5.0: preload rerank models in background (not awaited — must not block readiness).
    _warmup_task = asyncio.create_task(_run_model_warmup())

    yield

    # T3 Car 2: drain forked recall DB-write side-effects BEFORE stopping the
    # queue drainer / surreal (the #181 writers-stop seam) so no forked heat
    # boost is lost on shutdown. Bounded; overruns cancel (idempotent writes).
    from yadgar._shared.runtime.recall_side_effects_fork import (  # noqa: PLC0415
        drain_db_tasks as _drain_db_tasks,
    )

    try:
        await _drain_db_tasks(timeout=10.0)
    except Exception as _exc:  # noqa: BLE001 — shutdown must proceed
        logger.warning("recall side-effect drain error: %s", _exc)

    # backend 5.30.1: stop the queue drainer first (join capped at 5s).
    await asyncio.to_thread(_stop_queue_drainer)

    # Cancel snapshot task on shutdown
    _snap_task.cancel()
    try:
        await _snap_task
    except (asyncio.CancelledError, Exception):  # fmt: skip
        pass

    # Cancel warmup task on shutdown (no-op if already finished)
    _warmup_task.cancel()
    try:
        await _warmup_task
    except (asyncio.CancelledError, Exception):  # fmt: skip
        pass

    # Final cache snapshot on shutdown
    try:
        _ce_cache.save_snapshot(_snap_dir, "ce")
        _embed_cache.save_snapshot(_snap_dir, "embed")
    except Exception as _exc:
        logger.warning("cache final snapshot failed: %s", _exc)

    # v5.3.0: write clean-shutdown marker so next start knows we exited cleanly.
    _marker_path_shutdown = Path(_shutdown_marker_path())
    try:
        _marker_path_shutdown.parent.mkdir(parents=True, exist_ok=True)
        _marker_path_shutdown.write_text("1")
    except OSError as _exc:
        logger.warning("Failed to write shutdown marker: %s", _exc)


app = FastAPI(title="yadgar-embed", version="1.0", lifespan=lifespan)

# v5.6.3: instrument FastAPI so all /rerank, /embed, /health routes get server spans.
# Applied after app creation. InstrumentedRoutes get span names from route paths.
try:
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor as _FAI  # noqa: PLC0415

    _FAI.instrument_app(app)
except Exception:
    pass  # OTel not available — no-op


@app.get("/metrics")
@observe(tier="boundary", metric="backend.metrics")
async def metrics(request: Request):
    """Prometheus metrics endpoint (V1a, v5.5.0).

    Unauthenticated — Prometheus scrapers operate on loopback without bearer
    tokens.  Matches core /metrics pattern (yadgar/server/http.py §15).
    Always on: overhead is negligible (<1µs per observe); no sensitive data.
    """
    return await _metrics_handler(request)


@app.post("/embed", response_model=EmbedResponse)
@observe(tier="boundary", metric="backend.embed")
async def embed(req: EmbedRequest, _: None = Depends(_require_admin_token)):
    import time as _time

    t0 = _time.monotonic()
    engine = _get_engine()
    if engine is None:
        raise HTTPException(status_code=503, detail="Embedding engine not ready")

    @observe(tier="hot")
    def _encode_all() -> list[list[float] | None]:
        import hashlib as _hl  # noqa: PLC0415

        ckpt = getattr(_embed_cache, "_ckpt", _get_embed_checkpoint_hash())
        results: list[list[float] | None] = []
        prev_embed_evictions = _embed_cache.evictions
        for text in req.texts:
            # Check embed cache first (backend v5.4.0)
            text_sha = _hl.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]
            cache_key = f"{text_sha}:{req.mode}:{ckpt}"
            cached = _embed_cache.get(cache_key)
            if cached is not None:
                _embed_cache_hits_total.inc()
                results.append(cached)
                continue

            _embed_cache_misses_total.inc()
            if req.mode == "query":
                raw = engine.encode_query(text)
            elif req.mode == "raw":
                raw = engine.encode(text)
            else:
                raw = engine.encode_document(text)
            if raw is None:
                results.append(None)
            else:
                arr = np.frombuffer(raw, dtype=np.float32)
                vec = arr.tolist()
                _embed_cache.put(cache_key, vec)
                results.append(vec)
        # Sync eviction delta + size to Prometheus
        new_embed_evictions = _embed_cache.evictions - prev_embed_evictions
        if new_embed_evictions > 0:
            _embed_cache_evictions_total.inc(new_embed_evictions)
        try:
            _embed_cache_size_entries.set(_embed_cache.size_entries)
        except Exception:
            pass
        return results

    results = await asyncio.to_thread(_encode_all)
    logger.info(
        "embed: %d texts, mode=%s, latency=%dms",
        len(req.texts),
        req.mode,
        int((_time.monotonic() - t0) * 1000),
    )
    return EmbedResponse(
        embeddings=results,
        model=engine.model_name,
        dim=engine.get_dimensions(),
    )


@observe(tier="stage")
def _score_ce_with_cache(ml, query: str, texts: list[str]) -> list[float]:
    """CE scoring with per-text LRU cache hit-path (backend v5.4.0).

    Key: sha256(query)[:16] + ":" + sha256(text)[:16] + ":" + ckpt_hash.
    Partial hits: cached texts skip ML; only misses go to CE batch.
    Results merged back to original order, then back-filled into cache.
    """
    import hashlib  # noqa: PLC0415

    if not texts:
        return []

    ckpt = getattr(_ce_cache, "_ckpt", _get_ce_checkpoint_hash())
    query_sha = hashlib.sha256(query.encode("utf-8", errors="replace")).hexdigest()[:16]

    # Build keys and check cache
    keys = []
    for text in texts:
        text_sha = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]
        keys.append(f"{query_sha}:{text_sha}:{ckpt}")

    cached_scores: dict[int, float] = {}
    miss_indices: list[int] = []
    miss_texts: list[str] = []

    for i, key in enumerate(keys):
        hit = _ce_cache.get(key)
        if hit is not None:
            cached_scores[i] = hit
            _ce_cache_hits_total.inc()
        else:
            miss_indices.append(i)
            miss_texts.append(texts[i])
            _ce_cache_misses_total.inc()

    if miss_texts:
        new_scores = ml.score_cross_encoder(query, miss_texts)
        # Back-fill cache + merge
        prev_evictions = _ce_cache.evictions
        for j, idx in enumerate(miss_indices):
            score = float(new_scores[j]) if new_scores and j < len(new_scores) else 0.0
            _ce_cache.put(keys[idx], score)
            cached_scores[idx] = score
        # Sync new evictions to Prometheus counter
        new_evictions = _ce_cache.evictions - prev_evictions
        if new_evictions > 0:
            _ce_cache_evictions_total.inc(new_evictions)
        try:
            _ce_cache_size_entries.set(_ce_cache.size_entries)
        except Exception:
            pass

    return [cached_scores.get(i, 0.0) for i in range(len(texts))]


@app.post("/rerank", response_model=RerankResponse)
async def rerank(req: RerankRequest, _: None = Depends(_require_admin_token)) -> RerankResponse:
    """Score texts using the local ML client (cross-encoder, NLI, or pair mode).

    F5-A: acquire per-mode semaphore before dispatching to inference thread.
    If semaphore is unavailable within RERANK_SEMAPHORE_ACQUIRE_TIMEOUT_SEC,
    return 503 immediately so circuit-breaker probes fast-fail without burning CPU.

    Metrics instrumented (V1a, v5.5.0):
    - rerank_requests_total incremented on every call
    - rerank_503_total incremented on semaphore-busy 503
    - rerank_duration_seconds observed on successful inference (post-acquire)
    - rerank_semaphore_held tracks acquired slot count
    """
    import time as _time

    _rerank_requests_total.labels(mode=req.mode).inc()

    sem = _rerank_semaphores[req.mode]
    timeout = _rerank_acquire_timeout()
    try:
        await asyncio.wait_for(sem.acquire(), timeout=timeout)
    except TimeoutError:
        _rerank_503_total.labels(mode=req.mode).inc()
        logger.warning(
            "semaphore_busy",
            extra={
                "component": "embed_service",
                "action": "rerank_acquire",
                "outcome": "error",
                "rerank_mode": req.mode,
                "timeout_s": timeout,
                "http_status": 503,
            },
        )
        raise HTTPException(status_code=503, detail="inference slot unavailable") from None

    _rerank_semaphore_held.labels(mode=req.mode).inc()
    ml = _get_reranker()

    def _score() -> list[float]:
        if req.mode == "nli":
            return ml.score_nli(req.query, req.texts)
        elif req.mode == "pair":
            if not req.texts:
                return []
            return [ml.score_pair(req.query, req.texts[0])]
        else:  # "ce" default — with LRU cache hit-path (backend v5.4.0)
            return _score_ce_with_cache(ml, req.query, req.texts)

    t0 = _time.monotonic()

    # v5.6.3: child span showing pure inference time (excludes FastAPI overhead).
    # Semaphore release is always in the finally block regardless of OTel availability.
    def _make_inference_span():
        """Return an OTel span context manager, or nullcontext if unavailable."""
        try:
            from opentelemetry import trace as _ot  # noqa: PLC0415

            _model_name = resolve_knob(
                "YADGAR_EMBEDDING_MODEL", "EMBEDDING_MODEL", str, "all-MiniLM-L6-v2"
            )
            _tracer = _ot.get_tracer("yadgar.backend.embed_service")
            _ctx = _tracer.start_as_current_span(f"backend.rerank.{req.mode}")
            return _ctx, _model_name
        except Exception:
            import contextlib  # noqa: PLC0415

            return contextlib.nullcontext(), None

    def _annotate_span(_span, _model_name, _mode: str, _n: int) -> None:
        """Set OTel span attributes; silently ignores any attribute-set failure."""
        if _span is None or _model_name is None:
            return
        try:
            _span.set_attribute("rerank.mode", _mode)
            _span.set_attribute("rerank.n_passages", _n)
            _span.set_attribute("model.name", _model_name)
        except Exception:
            pass

    _span_ctx, _model_name = _make_inference_span()
    try:
        with _span_ctx as _span:
            _annotate_span(_span, _model_name, req.mode, len(req.texts))
            scores = await asyncio.to_thread(_score)
    finally:
        elapsed = _time.monotonic() - t0
        _rerank_duration_seconds.labels(mode=req.mode).observe(elapsed)
        _rerank_semaphore_held.labels(mode=req.mode).dec()
        sem.release()

    return RerankResponse(scores=scores, mode=req.mode)


@app.get("/health")
@observe(tier="boundary", metric="backend.health")
async def health(response: Response):
    """Returns 200 only when DB is up AND embedding model is loaded."""
    db_url = os.environ.get("YADGAR_DB_URL", "http://127.0.0.1:8000")
    engine_loaded = _engine is not None and not _engine._unavailable and _engine._model is not None

    db_ok = False
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{db_url}/health", timeout=2.0)
            db_ok = resp.status_code == 200
    except Exception:
        pass

    payload = {
        "status": "ok" if (db_ok and engine_loaded) else "degraded",
        "db": db_ok,
        "model": engine_loaded,
        # backend 5.30.1: drainer state is informational (alerting via the
        # yadgar_embed_queue_drainer_running gauge) — does NOT gate 503, so a
        # queue-mount misconfig cannot restart-loop the embed/rerank service.
        "drainer": _queue_drainer is not None and _queue_drainer.is_alive(),
    }
    if not db_ok or not engine_loaded:
        response.status_code = 503
    return payload


@observe(tier="hot")
def _walk_db_sizes(
    db_path: Path,
    known_subdirs: set[str],
) -> tuple[dict[str, int], int]:
    """Walk db_path and return (size_by_dir, other_size).

    Extracted to keep admin_dbsize nesting ≤ 4 (I13 HARD cap).
    """
    size_by_dir: dict[str, int] = {k: 0 for k in known_subdirs}
    other_size = 0
    for dirpath, _dirs, filenames in os.walk(db_path):
        rel = os.path.relpath(dirpath, db_path)
        top = rel.split(os.sep)[0] if rel != "." else ""
        for fname in filenames:
            try:
                fsize = os.stat(os.path.join(dirpath, fname)).st_size
            except OSError:
                continue
            if top in known_subdirs:
                size_by_dir[top] += fsize
            else:
                other_size += fsize
    return size_by_dir, other_size


@app.get("/admin/dbsize")
@observe(tier="boundary", metric="backend.admin_dbsize")
async def admin_dbsize(_: None = Depends(_require_admin_token)):
    """Return a filesystem size breakdown of the SurrealDB data directory.

    Walks /data/surreal_db (configurable via YADGAR_DB_PATH) using os.walk()
    and buckets files by subdirectory (vlog/, sstables/, wal/).  Returns the
    same field structure as StorageEngine.get_db_size() so the core container
    can use the response directly without field remapping.

    v5.3.0: response is cached in memory for YADGAR_DBSIZE_CACHE_TTL_SEC (default 60).
    Set TTL=0 to disable caching.  cache_age_seconds in the response indicates
    how old the cached payload is; 0 = freshly computed.
    """
    global _dbsize_cache, _dbsize_cache_ts

    now = time.time()
    ttl = _dbsize_cache_ttl()

    if ttl > 0 and _dbsize_cache is not None and (now - _dbsize_cache_ts) < ttl:
        _dbsize_cache_hits.inc()
        return {**_dbsize_cache, "cache_age_seconds": now - _dbsize_cache_ts}

    _dbsize_cache_misses.inc()

    # Resolve DB path: container default is /data/surreal_db; elsewhere use YADGAR_DB_PATH.
    _container_db = "/data/surreal_db"
    db_path = (
        Path(_container_db)
        if Path(_container_db).exists()
        else Path(os.environ.get("YADGAR_DB_PATH", str(_paths.DB_PATH))).expanduser()
    )

    known_subdirs = {"vlog", "sstables", "wal"}
    if not db_path.exists():
        size_by_dir: dict[str, int] = {k: 0 for k in known_subdirs}
        other_size = 0
        total = 0
    else:
        size_by_dir, other_size = _walk_db_sizes(db_path, known_subdirs)
        total = sum(size_by_dir.values()) + other_size

    vlog = size_by_dir["vlog"]
    vlog_pct = int(vlog * 100 / total) if total > 0 else 0

    payload = {
        "db_size_bytes": total,
        "vlog_size_bytes": vlog,
        "sstables_size_bytes": size_by_dir["sstables"],
        "wal_size_bytes": size_by_dir["wal"],
        "other_size_bytes": other_size,
        "vlog_pct_of_total": vlog_pct,
    }
    _dbsize_cache = payload
    _dbsize_cache_ts = now
    return {**payload, "cache_age_seconds": 0.0}


# ---------------------------------------------------------------------------
# Train 1: backend /recall route — runs the fan-out pipeline backend-side
# ---------------------------------------------------------------------------

# Module-level guard: init_engines called at most once in the backend process.
_recall_engines_ready: bool = False
_recall_engines_lock = threading.Lock()


@observe(tier="stage")
def _ensure_recall_engines() -> None:
    """Lazily initialise the engines needed for the backend /recall pipeline.

    Calls init_engines() from lifecycle.py (verified _app-clean in the init path)
    exactly once, guarded by a lock so concurrent requests don't race.  The
    backend uses LocalMLClient (no YADGAR_EMBED_URL in backend container) and
    the local SurrealDB (YADGAR_DB_URL defaults to localhost:8000).

    #44 fix: passes local_engines=True so init selects LOCAL in-process engines
    and does NOT trip the CORE offload guard.  The prod backend container carries
    the shared YADGAR_OFFLOAD_TOOLS flag but has no YADGAR_EMBED_URL (it IS the
    embed service); the guard is a core tool-body-pool concern that does not
    apply to this single-purpose ML service, where local engines are GIL-safe.

    Idempotent: subsequent calls return immediately when engines are ready.
    """
    global _recall_engines_ready
    if _recall_engines_ready:
        return
    with _recall_engines_lock:
        if _recall_engines_ready:
            return
        from yadgar._shared.runtime.lifecycle import init_engines as _init_engines  # noqa: PLC0415
        from yadgar.backend.restoration import ensure_restoration_engines  # noqa: PLC0415

        # Car 3 (folder-split #17): slim engine set — build only the 14 engines
        # the /recall path needs, skip the 10 CORE-ONLY engines. Behavior-neutral
        # for recall (byte-identical output; a missing engine = immediate crash).
        _init_engines(local_engines=True, engine_set="slim")
        # T2 Car E2: compose the backend Retriever (the shared root no longer
        # builds it — retrieval sank to yadgar.backend.retrieval). MUST run
        # before ensure_restoration_engines, which wires _st._retriever into
        # CheckpointRestore.
        from yadgar.backend.retrieval.compose import ensure_retrieval_engine  # noqa: PLC0415

        ensure_retrieval_engine()
        # T2 Car B: compose the backend restoration engines (CognitiveMap +
        # CheckpointRestore) next to the DB — the shared root no longer builds
        # them. Needed by POST /restore, /admin (anchor ops), and the drainer.
        ensure_restoration_engines()
        _recall_engines_ready = True


# ---------------------------------------------------------------------------
# C1 split (#18): the DB-forward routes (/recall, /restore, /consolidate, /admin,
# /viz) + the recall helpers live in embed_service_routes; the queue-drainer
# lifecycle (_queue_base_path/_start/_stop_queue_drainer) lives in
# embed_service_lifecycle. Imported HERE, at the bottom, AFTER `app` +
# `_ensure_recall_engines` + `_recall_engines_ready` + `_queue_drainer` exist, so
# the @app.post decorators register on the live app and the helpers reach the
# guard/writer/handle through the module object.
#
# RELOAD-AWARE (recipe crux): importlib.reload(embed_service) re-runs THIS body,
# creating a fresh `app`, but does NOT re-execute an already-imported sibling —
# so a plain import would leave the 5 DB-forward routes registered on the OLD
# app and absent from the reloaded one (test-ordering pollution: any prior
# reload silently drops /recall,/restore,/consolidate,/admin,/viz). Force a
# reload of the siblings when they are already loaded so `app = _es.app` at their
# top rebinds to the new app and the @app.post decorators re-register on it. The
# lifecycle sibling re-reads its module refs on reload too (harmless, cheap).
import importlib as _importlib  # noqa: E402

_sib_lifecycle = _importlib.import_module("yadgar.backend.embed_service.embed_service_lifecycle")
_sib_routes = _importlib.import_module("yadgar.backend.embed_service.embed_service_routes")
if getattr(_sib_lifecycle, "_YADGAR_ES_LOADED", False):
    _sib_lifecycle = _importlib.reload(_sib_lifecycle)
if getattr(_sib_routes, "_YADGAR_ES_LOADED", False):
    _sib_routes = _importlib.reload(_sib_routes)

# Re-export so embed_service.embed_service.<name> keeps resolving for callers +
# tests. lifespan (above) references _start/_stop_queue_drainer via these module
# globals, so patch.object(es, "_start_queue_drainer") still intercepts.
_queue_base_path = _sib_lifecycle._queue_base_path
_start_queue_drainer = _sib_lifecycle._start_queue_drainer
_stop_queue_drainer = _sib_lifecycle._stop_queue_drainer
_bootstrap_graph_layout_if_empty = _sib_lifecycle._bootstrap_graph_layout_if_empty
_forked_boost_write = _sib_routes._forked_boost_write
_run_landscape_backend = _sib_routes._run_landscape_backend
admin_route = _sib_routes.admin_route
consolidate_route = _sib_routes.consolidate_route
recall_route = _sib_routes.recall_route
restore_route = _sib_routes.restore_route
viz_route = _sib_routes.viz_route
