"""Embedding microservice — runs in the backend container.

Serves POST /embed for the core container to call.
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

logger = logging.getLogger(__name__)

_engine: EmbeddingEngine | None = None
_engine_lock = threading.Lock()


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


@asynccontextmanager
async def lifespan(app: FastAPI):
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
