"""Embedding microservice — runs in the backend container.

Serves POST /embed for the core container to call.
Serves POST /rerank for ML scoring (cross-encoder, NLI, pair) via LocalMLClient.
GET /health returns 200 only when SurrealDB is also reachable (true readiness signal).
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import httpx
import numpy as np
from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, field_validator

if TYPE_CHECKING:
    from yadgar.embeddings import EmbeddingEngine
    from yadgar.ml_client import LocalMLClient

logger = logging.getLogger(__name__)

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
    # Configure log level from env var (set by docker-compose / entrypoint)
    _level = os.environ.get("YADGAR_BACKEND_LOG_LEVEL", "warn").upper()
    logging.getLogger("yadgar").setLevel(getattr(logging, _level, logging.WARNING))

    # Load model eagerly so /health reflects true readiness
    try:
        await asyncio.to_thread(_get_engine)
        logger.info("Embedding model loaded")
    except Exception as exc:
        logger.error("Failed to load embedding model: %s", exc)
    yield


app = FastAPI(title="yadgar-embed", version="1.0", lifespan=lifespan)


@app.post("/embed", response_model=EmbedResponse)
async def embed(req: EmbedRequest):
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
async def rerank(req: RerankRequest) -> RerankResponse:
    """Score texts using the local ML client (cross-encoder, NLI, or pair mode)."""
    ml = _get_reranker()

    def _score() -> list[float]:
        if req.mode == "nli":
            # LocalMLClient.score_nli already returns entailment probabilities as floats
            return ml.score_nli(req.query, req.texts)
        elif req.mode == "pair":
            if not req.texts:
                return []
            return [ml.score_pair(req.query, req.texts[0])]
        else:  # "ce" default
            return ml.score_cross_encoder(req.query, req.texts)

    scores = await asyncio.to_thread(_score)
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


@app.get("/admin/dbsize")
async def admin_dbsize():
    """Return a filesystem size breakdown of the SurrealDB data directory.

    Walks /data/surreal_db using os.walk() and buckets files by subdirectory
    (vlog/, sstables/, wal/).  Returns the same field structure as
    StorageEngine.get_db_size() so the core container can use the response
    directly without field remapping.
    """
    import os as _os
    from pathlib import Path as _Path

    db_path = _Path("/data/surreal_db")
    known_subdirs = {"vlog", "sstables", "wal"}
    size_by_dir: dict[str, int] = {k: 0 for k in known_subdirs}
    other_size = 0

    if not db_path.exists():
        total = 0
    else:
        for dirpath, _dirs, filenames in _os.walk(db_path):
            rel = _os.path.relpath(dirpath, db_path)
            top = rel.split(_os.sep)[0] if rel != "." else ""
            for fname in filenames:
                try:
                    fsize = _os.stat(_os.path.join(dirpath, fname)).st_size
                except OSError:
                    continue
                if top in known_subdirs:
                    size_by_dir[top] += fsize
                else:
                    other_size += fsize

        total = sum(size_by_dir.values()) + other_size

    vlog = size_by_dir["vlog"]
    vlog_pct = int(vlog * 100 / total) if total > 0 else 0

    return {
        "db_size_bytes": total,
        "vlog_size_bytes": vlog,
        "sstables_size_bytes": size_by_dir["sstables"],
        "wal_size_bytes": size_by_dir["wal"],
        "other_size_bytes": other_size,
        "vlog_pct_of_total": vlog_pct,
    }
