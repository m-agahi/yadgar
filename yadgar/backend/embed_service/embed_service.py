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
    embed_drainer_running as _drainer_running,
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

if TYPE_CHECKING:
    from yadgar._shared.embeddings import EmbeddingEngine
    from yadgar.backend.ml_client import LocalMLClient

logger = logging.getLogger(__name__)

_http_bearer = HTTPBearer(auto_error=False)

# ---------------------------------------------------------------------------
# F5-A — Per-mode concurrent-inference semaphore (v5.4.2)
# ---------------------------------------------------------------------------
# Bounds concurrent /rerank inferences so HALF_OPEN probes fast-fail (503)
# instead of queueing behind a saturated model thread.
# Module-level so reload() recreates them with fresh env values in tests.


def _make_rerank_semaphores() -> dict[str, asyncio.Semaphore]:
    from yadgar._shared.config import get_settings

    _n = int(get_settings().RERANK_MAX_CONCURRENCY)
    return {mode: asyncio.Semaphore(_n) for mode in ("ce", "nli", "pair")}


_rerank_semaphores: dict[str, asyncio.Semaphore] = _make_rerank_semaphores()


def _rerank_acquire_timeout() -> float:
    from yadgar._shared.config import get_settings

    return float(get_settings().RERANK_SEMAPHORE_ACQUIRE_TIMEOUT_SEC)


# ---------------------------------------------------------------------------
# v5.3.0 — /admin/dbsize in-memory cache
# ---------------------------------------------------------------------------
# Module-level so importlib.reload() resets both fields, keeping tests isolated.

_dbsize_cache: dict | None = None  # last computed payload (without cache_age_seconds)
_dbsize_cache_ts: float = 0.0  # time.time() when last computed


def _dbsize_cache_ttl() -> int:
    """Return DBSIZE_CACHE_TTL_SEC from Settings (yaml/env/default 60). 0 = disabled."""
    from yadgar._shared.config import get_settings  # noqa: PLC0415

    return int(get_settings().DBSIZE_CACHE_TTL_SEC)


@observe(tier="hot")
def _shutdown_marker_path() -> str:
    """Return path for clean-shutdown marker file."""
    return os.environ.get("YADGAR_SHUTDOWN_MARKER_PATH", "/data/.shutdown_clean")


# ---------------------------------------------------------------------------
# backend v5.4.0 — LRU caches for CE scores and embedding vectors
# ---------------------------------------------------------------------------
# Module-level so importlib.reload() resets both caches in tests.
# Cache instances are created lazily on first access so env knobs are resolved
# after any monkeypatch in tests.


def _ce_cache_enabled() -> bool:
    return resolve_knob(
        "YADGAR_CE_CACHE_ENABLED",
        "CE_CACHE_ENABLED",
        lambda v: v.lower() not in ("0", "false", "no"),
        True,
    )


def _embed_cache_enabled() -> bool:
    return resolve_knob(
        "YADGAR_EMBED_CACHE_ENABLED",
        "EMBED_CACHE_ENABLED",
        lambda v: v.lower() not in ("0", "false", "no"),
        True,
    )


def _ce_cache_max_entries() -> int:
    return resolve_knob("YADGAR_CE_CACHE_MAX_ENTRIES", "CE_CACHE_MAX_ENTRIES", int, 100000)


def _embed_cache_max_entries() -> int:
    return resolve_knob("YADGAR_EMBED_CACHE_MAX_ENTRIES", "EMBED_CACHE_MAX_ENTRIES", int, 100000)


def _backend_cache_ram_pct() -> float:
    """% of the backend container RAM budgeted for the unified backend cache.

    Byte-bounded eviction sizes each namespace from this (Car 0, backend 5.17.0).
    The legacy YADGAR_*_CACHE_MAX_ENTRIES knobs no longer cap entry count; the
    byte budget is authoritative. The *_CACHE_ENABLED kill switches still disable.
    """
    return resolve_knob("YADGAR_BACKEND_CACHE_RAM_PCT", "BACKEND_CACHE_RAM_PCT", float, 10.0)


def _cache_snapshot_dir() -> str:
    return resolve_knob("YADGAR_CACHE_SNAPSHOT_DIR", "CACHE_SNAPSHOT_DIR", str, "/data/cache")


def _cache_snapshot_interval_sec() -> int:
    return resolve_knob(
        "YADGAR_CACHE_SNAPSHOT_INTERVAL_SEC", "CACHE_SNAPSHOT_INTERVAL_SEC", int, 600
    )


@observe(tier="hot")
def _get_ce_checkpoint_hash() -> str:
    """Return a short hash identifying the current CE model checkpoint."""
    import hashlib  # noqa: PLC0415

    model = os.environ.get(
        "YADGAR_CE_MODEL",
        resolve_knob("YADGAR_EMBEDDING_MODEL", "EMBEDDING_MODEL", str, "default"),
    )
    return hashlib.sha256(model.encode()).hexdigest()[:16]


@observe(tier="hot")
def _get_embed_checkpoint_hash() -> str:
    """Return a short hash identifying the current embedding model."""
    import hashlib  # noqa: PLC0415

    model = resolve_knob("YADGAR_EMBEDDING_MODEL", "EMBEDDING_MODEL", str, "all-MiniLM-L6-v2")
    return hashlib.sha256(model.encode()).hexdigest()[:16]


@observe(tier="stage")
def _make_ce_cache():
    """Build the unified `ce` namespace (Car 0). Byte-budget from RAM-%.

    Behaviour-neutral fold-in: same keys (query_sha:text_sha:ckpt), same float
    values, same ModelCkpt-in-key invalidation, same snapshot format. Only the
    eviction discipline changed (count-cap → byte-cap). DI note: still a module
    global for now; consumer constructor-DI deferred to a later car.
    """
    from yadgar.backend.cache import (  # noqa: PLC0415
        Cache,
        ModelCkpt,
        _backend_cache_total_budget_bytes,
        _namespace_budget_bytes,
    )

    if not _ce_cache_enabled():
        budget = 0
    else:
        total = _backend_cache_total_budget_bytes(_backend_cache_ram_pct())
        budget = _namespace_budget_bytes("ce", total)
    return Cache(
        name="ce",
        max_bytes=budget,
        invalidation=ModelCkpt(),
        checkpoint_hash=_get_ce_checkpoint_hash(),
        obs_tier="hot",
    )


@observe(tier="stage")
def _make_embed_cache():
    """Build the unified `embed` namespace (Car 0). See `_make_ce_cache`."""
    from yadgar.backend.cache import (  # noqa: PLC0415
        Cache,
        ModelCkpt,
        _backend_cache_total_budget_bytes,
        _namespace_budget_bytes,
    )

    if not _embed_cache_enabled():
        budget = 0
    else:
        total = _backend_cache_total_budget_bytes(_backend_cache_ram_pct())
        budget = _namespace_budget_bytes("embed", total)
    return Cache(
        name="embed",
        max_bytes=budget,
        invalidation=ModelCkpt(),
        checkpoint_hash=_get_embed_checkpoint_hash(),
        obs_tier="hot",
    )


# Module-level cache instances (reset on importlib.reload)
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
# backend 5.30.1 — queue drainer lifecycle (P0 fix)
#
# R3 Car 1 (87143dd0) moved QueueDrainer core→backend and removed the
# construction from core _get_file_queue with the note "started by the backend
# lifecycle half" — but no backend startup code ever built it, so production
# writes sat in queue/ forever. This is the missing wiring; the FileQueue +
# DrainerConfig construction mirrors what Car 1 removed from core exactly.
# ---------------------------------------------------------------------------

_queue_drainer = None  # live QueueDrainer | None — module-level for /health + shutdown


@observe(tier="stage")
def _queue_base_path() -> Path | None:
    """Resolve the shared file-queue root from YADGAR_QUEUE_BASE (R3 Car 0).

    The backend container mounts the shared queue volume rw at /queue-data and
    sets YADGAR_QUEUE_BASE; core mounts the SAME volume at /data. No fallback
    to YADGAR_DATA_DIR here: on the backend /data is the read-only DB mount,
    and unit tests always set YADGAR_DATA_DIR — falling back would silently
    start a drainer where none belongs. Unset → drainer disabled (gauge 0).
    """
    base = os.environ.get("YADGAR_QUEUE_BASE", "").strip()
    return Path(base) if base else None


@observe(tier="stage")
def _start_queue_drainer():
    """Construct + start the backend QueueDrainer (the R3 Car 1 write-half).

    Wiring mirrors the pre-R3 core _get_file_queue construction: FileQueue on
    the queue root + QueueDrainer(storage_factory=_get_storage) with
    drain_interval and DrainerConfig from settings. Ensures the recall engine
    stack (incl. _st._storage / _st._embeddings) is up BEFORE the first drain
    pass — ensure_write_engines and the write_exec replay impls read _st.*.

    Fail-loud: queue root missing/unwritable, or any construction error →
    ERROR log + yadgar_embed_queue_drainer_running=0 + /health drainer=false.
    Never raises — the embed/rerank service must still come up.

    Returns the started QueueDrainer, or None when disabled/failed.
    """
    global _queue_drainer
    base = _queue_base_path()
    if base is None:
        _drainer_running.set(0)
        logger.warning(
            "queue_drainer_disabled",
            extra={
                "event": "queue_drainer_disabled",
                "reason": "YADGAR_QUEUE_BASE unset (production backend must set it — R3 Car 0)",
            },
        )
        return None

    try:
        base.mkdir(parents=True, exist_ok=True)
        _probe = base / ".drainer-write-probe"
        _probe.write_text("1")
        _probe.unlink()
    except OSError as exc:
        _drainer_running.set(0)
        logger.error(
            "queue_drainer_start_failed",
            extra={
                "event": "queue_drainer_start_failed",
                "queue_base": str(base),
                "error": str(exc),
            },
        )
        return None

    try:
        import yadgar._shared.runtime.state as _st  # noqa: PLC0415
        from yadgar._shared.config import get_settings  # noqa: PLC0415
        from yadgar._shared.file_queue.queue import FileQueue  # noqa: PLC0415
        from yadgar._shared.runtime.lifecycle import _get_storage  # noqa: PLC0415
        from yadgar.backend.queue_drainer import DrainerConfig, QueueDrainer  # noqa: PLC0415

        # Engines first: replay impls + ensure_write_engines read _st._storage.
        _ensure_recall_engines()

        settings = get_settings()
        fq = FileQueue(base, wiki_prefix=settings.WIKI_SLUG_PREFIX)
        drainer = QueueDrainer(
            fq,
            _get_storage,
            drain_interval=float(settings.QUEUE_DRAIN_INTERVAL),
            config=DrainerConfig(
                max_permanent_attempts=settings.QUEUE_MAX_PERMANENT_ATTEMPTS,
                max_transient_attempts=settings.QUEUE_MAX_TRANSIENT_ATTEMPTS,
                backoff_base_s=float(settings.QUEUE_BACKOFF_BASE_S),
                backoff_max_s=float(settings.QUEUE_BACKOFF_MAX_S),
                dlq_retention_days=settings.QUEUE_DLQ_RETENTION_DAYS,
            ),
        )
        drainer.start()
    except Exception as exc:  # noqa: BLE001 — embed/rerank must still serve
        _drainer_running.set(0)
        logger.error(
            "queue_drainer_start_failed",
            extra={
                "event": "queue_drainer_start_failed",
                "queue_base": str(base),
                "error": str(exc),
            },
        )
        return None

    _st._file_queue = fq
    _st._queue_drainer = drainer
    _queue_drainer = drainer
    _drainer_running.set(1)
    logger.info(
        "queue_drainer_started",
        extra={
            "event": "queue_drainer_started",
            "queue_base": str(base),
            "drain_interval_s": drainer._drain_interval,
        },
    )
    return drainer


@observe(tier="stage")
def _stop_queue_drainer() -> None:
    """Stop the QueueDrainer on shutdown (no-op when never started)."""
    global _queue_drainer
    if _queue_drainer is None:
        return
    try:
        _queue_drainer.stop()  # sets stop event + joins (5s cap)
        logger.info("queue_drainer_stopped", extra={"event": "queue_drainer_stopped"})
    except Exception as exc:  # noqa: BLE001 — shutdown must proceed
        logger.warning("queue_drainer stop error: %s", exc)
    _queue_drainer = None
    _drainer_running.set(0)


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


class RecallRequest(BaseModel):
    """Request body for POST /recall."""

    query: str
    directory: str
    current_branch: str | None = None
    default_branch: str | None = None
    max_results: int = 5
    min_heat: float = 0.0
    type: str = "all"  # noqa: A003 — matches MCP schema convention
    profile: str | None = None
    mode: str | None = None
    stage_overrides: dict | None = None
    tags: list[str] | None = None
    knobs: dict = {}  # noqa: B006 — Pydantic default_factory not needed here
    # ADR-0077: client compute budget in ms. When set, the route converts it to
    # a monotonic deadline and the pipeline aborts remaining stages once it is
    # exceeded (partial results) — a hook client that already timed out at 2.0s
    # must not keep the backend computing. None = no deadline (MCP recall path).
    deadline_ms: int | None = None

    model_config = {"extra": "forbid"}

    @field_validator("type")
    @classmethod
    def validate_type(cls, v: str) -> str:
        valid = {"all", "memory", "wiki"}
        if v not in valid:
            raise ValueError(f"type must be one of {sorted(valid)}; got {v!r}")
        return v


class RecallResponse(BaseModel):
    """Response body for POST /recall."""

    results: list[dict]


@observe(tier="stage", metric="backend.recall.landscape")
def _run_landscape_backend(query: str, max_results: int, directory: str, storage) -> list[dict]:
    """Backend-side landscape recall via AstrocytePool.consensus_retrieve.

    Phase 1 §5.1/§3.2: mirrors core _landscape_recall (recall.py:45-91) but runs
    inside the backend process where the AstrocytePool is available after
    init_engines(local_engines=True). The 400 guard at the route level is removed;
    this function is called when req.mode=="landscape".

    Returns [] gracefully when _pool is None (pool unavailable / disabled).
    Directory post-filter via is_directory_eligible (same predicate as fanout path).
    """
    import yadgar._shared.runtime.state as _st  # noqa: PLC0415
    from yadgar._shared.storage.directory import is_directory_eligible  # noqa: PLC0415

    if _st._pool is None:
        logger.debug("landscape_backend: pool unavailable — returning []")
        return []

    raw = _st._pool.consensus_retrieve(query, top_k=max_results)
    scoped = [r for r in raw if is_directory_eligible(r.get("directory_context"), directory)]
    return scoped[:max_results]


@observe(tier="stage")
async def _forked_boost_write(storage, boosted_ids: list[int], now: str) -> None:
    """T3 Car 2: the forked backend heat DB write (the ~407ms recall tail).

    Runs the batched ``storage.boost_memories_access`` off the recall response
    critical path (as a tracked task via ``schedule_db_write``, or awaited inline
    under backpressure). The ``recall.side_effects.db`` span nests under the
    recall request trace because the task is created while that span is current
    (contextvars carry the OTEL parent across ``create_task``).
    """
    from yadgar._shared.observability.tracing import span as _span  # noqa: PLC0415

    with _span("recall.side_effects.db", results=len(boosted_ids)):
        await asyncio.to_thread(storage.boost_memories_access, boosted_ids, now)


@app.post("/recall", response_model=RecallResponse)
@observe(tier="boundary", metric="backend.recall")
async def recall_route(
    req: RecallRequest, _: None = Depends(_require_admin_token)
) -> RecallResponse:
    """Run the fan-out recall pipeline backend-side and return ranked results.

    Phase 1 (backend contract widening, §5.1):
      - mode=None: _fanout_recall with optional profile/rerank_level threading.
      - mode="landscape": _landscape_recall via backend-local AstrocytePool.

    The two 400 guards for mode=landscape and profile= are removed — the backend
    now serves every recall variant. Existing callers (mode=None, profile=None)
    are unaffected (additive change, not a breaking change).

    Called by the core thin forwarder when RECALL_BACKEND_ENABLED=True.
    Applies the DB-side bookkeeping half (_apply_recall_db_side_effects) for
    the fanout path. Landscape side-effects use _apply_recall_db_side_effects too
    (heat boost + thermo), mirroring the core landscape path.

    Session-side bookkeeping (SR transitions, action buffer, replay counter)
    runs in the core process on the returned results — NOT here.

    Returns:
        RecallResponse with the ranked result list.
    """
    # Bootstrap engines (idempotent, guarded by lock).
    await asyncio.to_thread(_ensure_recall_engines)

    from yadgar._shared.runtime.lifecycle import (
        _get_storage as _backend_get_storage,  # noqa: PLC0415
    )
    from yadgar._shared.runtime.recall_side_effects_fork import (  # noqa: PLC0415
        schedule_db_write,
    )
    from yadgar.backend.retrieval.recall_pipeline import (  # noqa: PLC0415
        _compute_db_boost,
        _fanout_recall,
    )

    # ADR-0077: convert the client's compute budget to a monotonic deadline ONCE,
    # at route entry — the pipeline checks it between stages and aborts remaining
    # work (partial results) when exceeded. None = no deadline.
    deadline: float | None = (
        time.monotonic() + req.deadline_ms / 1000.0 if req.deadline_ms else None
    )

    # Run the RETRIEVAL + the response-feeding heat mutations in a thread
    # (CPU-bound + IO-bound mix; don't block the event loop). T3 Car 2: the
    # in-place heat/last_accessed mutations stay INLINE here (they feed the
    # response payload — must be byte-identical), but the batched DB WRITE
    # (~407ms tail) is forked off the response path below.
    def _run_pipeline() -> tuple[list[dict], list[int], str]:
        storage = _backend_get_storage()

        if req.mode == "landscape":
            # §5.1 landscape dispatch: backend-hosted consensus_retrieve via AstrocytePool.
            # Mirrors core _landscape_recall (recall.py:45-91): consensus_retrieve →
            # directory post-filter → apply DB side-effects.
            merged = _run_landscape_backend(
                query=req.query,
                max_results=req.max_results,
                directory=req.directory,
                storage=storage,
            )
        else:
            # Default fanout path — thread profile/rerank_level.
            merged = _fanout_recall(
                query=req.query,
                max_results=req.max_results,
                min_heat=req.min_heat,
                directory=req.directory,
                current_branch=req.current_branch,
                default_branch=req.default_branch,
                type_filter=req.type,
                tags=req.tags,
                profile=req.profile,
                deadline=deadline,
            )

        # Inline, latency-safe: mutate heat/last_accessed in place + thermo record.
        boosted_ids, now = _compute_db_boost(merged, storage)
        return merged, boosted_ids, now

    results, boosted_ids, boost_now = await asyncio.to_thread(_run_pipeline)

    # T3 Car 2: fork the batched heat DB write off the response critical path.
    # create_task runs while THIS request span is current → contextvars carry the
    # OTEL parent so recall.side_effects.db nests under the recall trace. If the
    # fork is disabled OR the in-flight cap is hit, await the SAME coroutine
    # inline (backpressure — the side-effect always executes, never dropped).
    if boosted_ids:
        storage = _backend_get_storage()
        _coro = _forked_boost_write(storage, boosted_ids, boost_now)
        if not schedule_db_write(_coro):
            await _coro

    return RecallResponse(results=results)


# ---------------------------------------------------------------------------
# T2 Car B: backend /restore route — runs the restore COMPUTE backend-side
# (CheckpointRestore + CognitiveMap SR navigation, census verdict #7). The
# core restore MCP tool, the post-compact hook, and the CLI restore subcommand
# forward here via the core _forward_restore helper. Live-proven motivation:
# restore() on core's 1 CPU exceeded the 95s tool-offload ceiling; the SR
# matrix compute now runs next to the DB on the backend's CPUs.
# ---------------------------------------------------------------------------


class RestoreRequest(BaseModel):
    """Request body for POST /restore."""

    directory: str = ""

    model_config = {"extra": "forbid"}


class RestoreResponse(BaseModel):
    """Response body for POST /restore.

    ``result`` is the exact payload CheckpointRestore.restore returns (the dict
    the core restore tool returned pre-Car-B): checkpoint, anchored_memories,
    recent_memories, hot_memories, predicted_memories, gaps_detected,
    memory_blocks, epoch, formatted.
    """

    result: dict


@app.post("/restore", response_model=RestoreResponse)
@observe(tier="boundary", metric="backend.restore")
async def restore_route(
    req: RestoreRequest, _: None = Depends(_require_admin_token)
) -> RestoreResponse:
    """Run the restore compute backend-side and return the restore payload.

    Mirrors the /recall route: lazily builds the slim engine set (plus the
    restoration engines) via _ensure_recall_engines, then runs the compute in a
    worker thread so the event loop is not blocked (SR matrix build + inversion
    is CPU-bound). Called by the core thin forwarder (_forward_restore).
    """
    from yadgar.backend.restoration import run_restore  # noqa: PLC0415

    # Bootstrap engines (idempotent, guarded by lock).
    await asyncio.to_thread(_ensure_recall_engines)

    result = await asyncio.to_thread(run_restore, req.directory)
    return RestoreResponse(result=result)


# ---------------------------------------------------------------------------
# R3 Car 1 D2: backend /consolidate route — runs the consolidation COMPUTE
# backend-side (it uses the backend curator + phase engines). The core
# orchestrator forwards here and layers its viz/admin tail on the result.
# ---------------------------------------------------------------------------


class ConsolidateRequest(BaseModel):
    """Request body for POST /consolidate."""

    mode: str = "light"

    model_config = {"extra": "forbid"}

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        valid = {"light", "full", "nightly"}
        if v not in valid:
            raise ValueError(f"mode must be one of {sorted(valid)}; got {v!r}")
        return v


class ConsolidateResponse(BaseModel):
    """Response body for POST /consolidate."""

    stats: dict


@app.post("/consolidate", response_model=ConsolidateResponse)
@observe(tier="boundary", metric="backend.consolidate")
async def consolidate_route(
    req: ConsolidateRequest, _: None = Depends(_require_admin_token)
) -> ConsolidateResponse:
    """Run the consolidation compute cycle backend-side and return the stats.

    Mirrors the /recall route: lazily builds the backend engine set (the
    consolidation service reuses the slim /recall engines + builds its own
    scheduler singleton), then runs one cycle in a worker thread so the event
    loop is not blocked by the CPU/IO-bound compute (light ~30s, full 5–15 min).

    Called by the core consolidation orchestrator (forward-only, R3 Car 1 D3).
    """
    from yadgar.backend.consolidation.service import (  # noqa: PLC0415
        run_consolidation_cycle,
    )

    stats = await asyncio.to_thread(run_consolidation_cycle, req.mode)
    return ConsolidateResponse(stats=stats)


# ---------------------------------------------------------------------------
# R3 Car 3a (R5 forward pattern): backend /admin route — runs the storage-WRITE
# half of the pure-CRUD MCP tools (bookmarks, blocks, …). Core keeps the @_tool
# shell + validation + secret-gate and forwards the write here via the core
# _forward_admin helper. Goal: core touches zero DB directly.
# ---------------------------------------------------------------------------


class AdminRequest(BaseModel):
    """Request body for POST /admin."""

    op: str
    payload: dict = {}  # noqa: RUF012 — Pydantic model field default, not a mutable class attr

    model_config = {"extra": "forbid"}


class AdminResponse(BaseModel):
    """Response body for POST /admin."""

    result: dict


@app.post("/admin", response_model=AdminResponse)
@observe(tier="boundary", metric="backend.admin")
async def admin_route(req: AdminRequest, _: None = Depends(_require_admin_token)) -> AdminResponse:
    """Run a single admin op's storage-write body backend-side and return its result.

    Mirrors the /recall + /consolidate routes: lazily builds the slim engine set
    (which includes storage) via _ensure_recall_engines, then runs the op in a
    worker thread so the event loop is not blocked by the storage IO.

    op must be a registered admin op (yadgar.backend.admin_exec.run_admin_op).
    Unknown ops → 400. Called by the core thin forwarders (_forward_admin).
    """
    from yadgar.backend.admin_exec import run_admin_op  # noqa: PLC0415

    # Bootstrap engines (idempotent, guarded by lock) — the op needs storage.
    await asyncio.to_thread(_ensure_recall_engines)

    try:
        result = await asyncio.to_thread(run_admin_op, req.op, req.payload)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return AdminResponse(result=result)


# ---------------------------------------------------------------------------
# T2 Car E3 (census verdict #11): backend /viz route — runs the DB-heavy graph
# data assembly (GraphAPI) + cached-layout attach backend-side. The core
# /api/graph* endpoints keep their route shells and forward here via the core
# _forward_viz helper. Mirrors /admin + run_admin_op (reads-flavored twin).
# ---------------------------------------------------------------------------


class VizRequest(BaseModel):
    """Request body for POST /viz."""

    op: str
    payload: dict = {}  # noqa: RUF012 — Pydantic model field default, not a mutable class attr

    model_config = {"extra": "forbid"}


class VizResponse(BaseModel):
    """Response body for POST /viz."""

    result: dict


@app.post("/viz", response_model=VizResponse)
@observe(tier="boundary", metric="backend.viz")
async def viz_route(req: VizRequest, _: None = Depends(_require_admin_token)) -> VizResponse:
    """Run a single viz op's graph-assembly body backend-side and return its result.

    Mirrors the /admin route: lazily builds the slim engine set (which includes
    storage) via _ensure_recall_engines, then runs the op in a worker thread so
    the event loop is not blocked by the assembly IO/compute.

    op must be a registered viz op (yadgar.backend.viz_exec.run_viz_op).
    Unknown ops → 400. Called by the core thin forwarders (_forward_viz).
    """
    from yadgar.backend.viz_exec import run_viz_op  # noqa: PLC0415

    # Bootstrap engines (idempotent, guarded by lock) — the op needs storage.
    await asyncio.to_thread(_ensure_recall_engines)

    try:
        result = await asyncio.to_thread(run_viz_op, req.op, req.payload)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return VizResponse(result=result)
