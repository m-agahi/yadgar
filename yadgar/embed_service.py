"""Embedding microservice — runs in the backend container.

Serves POST /embed for the core container to call.
Serves POST /rerank for ML scoring (cross-encoder, NLI, pair) via LocalMLClient.
GET /health returns 200 only when SurrealDB is also reachable (true readiness signal).
GET /metrics exposes Prometheus metrics (unauthenticated — V1a, v5.5.0).
"""

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
from pydantic import BaseModel, field_validator

from yadgar.embed_service_metrics import (
    embed_dbsize_cache_hits_total as _dbsize_cache_hits,
)
from yadgar.embed_service_metrics import (
    embed_dbsize_cache_misses_total as _dbsize_cache_misses,
)
from yadgar.embed_service_metrics import (
    embed_restart_reason_total as _restart_reason_total,
)
from yadgar.embed_service_metrics import (
    metrics_handler as _metrics_handler,
)
from yadgar.embed_service_metrics import (
    model_loaded as _model_loaded,
)
from yadgar.embed_service_metrics import (
    rerank_503_total as _rerank_503_total,
)
from yadgar.embed_service_metrics import (
    rerank_duration_seconds as _rerank_duration_seconds,
)
from yadgar.embed_service_metrics import (
    rerank_requests_total as _rerank_requests_total,
)
from yadgar.embed_service_metrics import (
    rerank_semaphore_held as _rerank_semaphore_held,
)

if TYPE_CHECKING:
    from yadgar.embeddings import EmbeddingEngine
    from yadgar.ml_client import LocalMLClient

logger = logging.getLogger(__name__)

_http_bearer = HTTPBearer(auto_error=False)

# ---------------------------------------------------------------------------
# F5-A — Per-mode concurrent-inference semaphore (v5.4.2)
# ---------------------------------------------------------------------------
# Bounds concurrent /rerank inferences so HALF_OPEN probes fast-fail (503)
# instead of queueing behind a saturated model thread.
# Module-level so reload() recreates them with fresh env values in tests.


def _make_rerank_semaphores() -> dict[str, asyncio.Semaphore]:
    from yadgar.config import get_settings

    _n = int(get_settings().RERANK_MAX_CONCURRENCY)
    return {mode: asyncio.Semaphore(_n) for mode in ("ce", "nli", "pair")}


_rerank_semaphores: dict[str, asyncio.Semaphore] = _make_rerank_semaphores()


def _rerank_acquire_timeout() -> float:
    from yadgar.config import get_settings

    return float(get_settings().RERANK_SEMAPHORE_ACQUIRE_TIMEOUT_SEC)


# ---------------------------------------------------------------------------
# v5.3.0 — /admin/dbsize in-memory cache
# ---------------------------------------------------------------------------
# Module-level so importlib.reload() resets both fields, keeping tests isolated.

_dbsize_cache: dict | None = None  # last computed payload (without cache_age_seconds)
_dbsize_cache_ts: float = 0.0  # time.time() when last computed


def _dbsize_cache_ttl() -> int:
    """Return YADGAR_DBSIZE_CACHE_TTL_SEC (default 60). 0 = disabled."""
    return int(os.environ.get("YADGAR_DBSIZE_CACHE_TTL_SEC", "60"))


def _shutdown_marker_path() -> str:
    """Return path for clean-shutdown marker file."""
    return os.environ.get("YADGAR_SHUTDOWN_MARKER_PATH", "/data/.shutdown_clean")


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


def _get_engine():
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                from yadgar.embeddings import EmbeddingEngine

                model = os.environ.get("YADGAR_EMBEDDING_MODEL", "all-MiniLM-L6-v2")
                _engine = EmbeddingEngine(model)
                _engine._ensure_model()
    return _engine


def _get_reranker() -> LocalMLClient:
    global _reranker
    if _reranker is None:
        with _reranker_lock:
            if _reranker is None:
                from yadgar.config import get_settings
                from yadgar.ml_client import LocalMLClient

                _reranker = LocalMLClient(get_settings())
                # Mark all reranker model variants as loaded (lazy-load on first use)
                for _mode in ("ce", "nli", "pair"):
                    _model_loaded.labels(model=_mode).set(1)
    return _reranker


class EmbedRequest(BaseModel):
    texts: list[str]
    mode: str = "document"  # "document" | "query" | "raw"

    @field_validator("texts")
    @classmethod
    def validate_texts(cls, v: list[str]) -> list[str]:
        if len(v) > 128:
            raise ValueError("Maximum 128 texts per request")
        for text in v:
            if len(text) > 32768:
                raise ValueError("Text exceeds maximum length of 32768 characters")
        return v


class EmbedResponse(BaseModel):
    embeddings: list[list[float] | None]
    model: str
    dim: int


class RerankRequest(BaseModel):
    query: str
    texts: list[str]
    mode: str = "ce"  # "ce" | "nli" | "pair"

    model_config = {"extra": "forbid"}

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        if v not in ("ce", "nli", "pair"):
            raise ValueError(f"mode must be 'ce', 'nli', or 'pair'; got {v!r}")
        return v


class RerankResponse(BaseModel):
    scores: list[float]
    mode: str


@asynccontextmanager
async def lifespan(app: FastAPI):
    # I14: configure structured logging at backend boot.
    # Format reads from YADGAR_LOG_FORMAT (default 'json' for production).
    _level = os.environ.get("YADGAR_BACKEND_LOG_LEVEL", "warn").upper()
    from yadgar.log_config import configure_logging as _configure_logging  # noqa: PLC0415

    _configure_logging(
        log_format=os.environ.get("YADGAR_LOG_FORMAT", "json"),
        level=_level,
        process="backend",
    )

    # v5.6.3: distributed tracing for backend.
    # setup_tracing initialises LogSpanProcessor + sets global TracerProvider.
    # v5.6.4 Bug 3: HTTPXClientInstrumentor — backend calls SurrealDB via httpx;
    # this ensures outbound httpx calls auto-inject W3C traceparent headers.
    from yadgar.tracing import setup_tracing as _setup_tracing  # noqa: PLC0415

    _setup_tracing("yadgar-backend")
    try:
        from opentelemetry.instrumentation.httpx import (
            HTTPXClientInstrumentor as _HCI,  # noqa: PLC0415
        )

        _HCI().instrument()
    except Exception as _otel_exc:
        from yadgar.exception_telemetry import record_exception  # noqa: PLC0415

        record_exception("embed_service.otel_setup", _otel_exc)
        pass  # OTel not available — no-op

    # v5.3.0: restart attribution — inspect previous shutdown state before model load.
    _marker = _shutdown_marker_path()
    _db_path = os.environ.get("YADGAR_DB_PATH", "~/.yadgar/surreal_db")
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

    # Load model eagerly so /health reflects true readiness
    try:
        await asyncio.to_thread(_get_engine)
        _model_loaded.labels(model="embedding").set(1)
        logger.info("Embedding model loaded")
    except Exception as exc:
        _model_loaded.labels(model="embedding").set(0)
        logger.error("Failed to load embedding model: %s", exc)
    yield

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
async def metrics(request: Request):
    """Prometheus metrics endpoint (V1a, v5.5.0).

    Unauthenticated — Prometheus scrapers operate on loopback without bearer
    tokens.  Matches core /metrics pattern (yadgar/server/http.py §15).
    Always on: overhead is negligible (<1µs per observe); no sensitive data.
    """
    return await _metrics_handler(request)


@app.post("/embed", response_model=EmbedResponse)
async def embed(req: EmbedRequest, _: None = Depends(_require_admin_token)):
    import time as _time

    t0 = _time.monotonic()
    engine = _get_engine()
    if engine is None:
        raise HTTPException(status_code=503, detail="Embedding engine not ready")

    def _encode_all() -> list[list[float] | None]:
        results: list[list[float] | None] = []
        for text in req.texts:
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
                results.append(arr.tolist())
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
        else:  # "ce" default
            return ml.score_cross_encoder(req.query, req.texts)

    t0 = _time.monotonic()

    # v5.6.3: child span showing pure inference time (excludes FastAPI overhead).
    # Semaphore release is always in the finally block regardless of OTel availability.
    def _make_inference_span():
        """Return an OTel span context manager, or nullcontext if unavailable."""
        try:
            from opentelemetry import trace as _ot  # noqa: PLC0415

            _model_name = os.environ.get("YADGAR_EMBEDDING_MODEL", "all-MiniLM-L6-v2")
            _tracer = _ot.get_tracer("yadgar.embed_service")
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
    }
    if not db_ok or not engine_loaded:
        response.status_code = 503
    return payload


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
        else Path(os.environ.get("YADGAR_DB_PATH", "~/.yadgar/surreal_db")).expanduser()
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
