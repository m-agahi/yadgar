"""Remote embedding client — used by the core container.

Calls the backend's POST /embed endpoint instead of loading sentence-transformers locally.
API-compatible with EmbeddingEngine so callers need no changes.
"""

from __future__ import annotations

import logging
import os
import threading
from collections import OrderedDict

import httpx
import numpy as np

from yadgar._shared.embeddings.embeddings import (
    MODEL_DIMENSIONS,
    MODEL_DOC_PREFIX,
    MODEL_QUERY_PREFIX,
)
from yadgar._shared.observability.observe import observe
from yadgar._shared.observability.tracing import trace_span

_CACHE_MAX = 512
logger = logging.getLogger(__name__)


class RemoteEmbeddingEngine:
    """EmbeddingEngine that delegates to the backend /embed HTTP endpoint."""

    def __init__(self, model_name: str = "all-MiniLM-L6-v2") -> None:
        self.model_name = model_name
        self._unavailable = False
        self._model = True  # sentinel: "available" for compatibility checks
        self._query_cache: OrderedDict[str, bytes] = OrderedDict()
        self._cache_lock = threading.Lock()
        embed_url = os.environ.get("YADGAR_EMBED_URL", "http://127.0.0.1:8001")
        _token = os.environ.get("YADGAR_MCP_AUTH_TOKEN", "")
        _headers = {"Authorization": f"Bearer {_token}"} if _token else {}
        self._client = httpx.Client(base_url=embed_url, timeout=30.0, headers=_headers)

    def _ensure_model(self) -> None:
        pass  # no-op: model lives in the backend container

    def _is_model_cached(self) -> bool:
        return True  # always considered "cached" — it's remote

    def get_model_name(self) -> str:
        return self.model_name

    @observe(tier="hot")
    def get_dimensions(self) -> int:
        return MODEL_DIMENSIONS.get(self.model_name, 384)

    @observe(tier="hot")
    def needs_reembedding(self, stored_model: str) -> bool:
        if stored_model is None:
            return True
        return stored_model != self.model_name

    @trace_span()
    def _call(self, texts: list[str], mode: str = "document") -> list[bytes | None]:
        if not texts:
            return []
        try:
            resp = self._client.post("/embed", json={"texts": texts, "mode": mode})
            resp.raise_for_status()
            data = resp.json()
            results = []
            for floats in data["embeddings"]:
                if floats is None:
                    results.append(None)
                else:
                    arr = np.array(floats, dtype=np.float32)
                    results.append(arr.tobytes())
            return results
        except Exception as exc:
            logger.warning("RemoteEmbeddingEngine: /embed call failed: %s", exc)
            return [None] * len(texts)

    @observe(tier="stage")
    def encode(self, text: str) -> bytes | None:
        from yadgar._shared.observability.metrics import (
            record_cache_evict,
            record_cache_hit,
            record_cache_miss,
        )

        with self._cache_lock:
            if text in self._query_cache:
                self._query_cache.move_to_end(text)
                record_cache_hit("remote_embedding")
                return self._query_cache[text]
        record_cache_miss("remote_embedding")
        result = self._call([text], "raw")
        val = result[0] if result else None
        if val is not None:
            with self._cache_lock:
                self._query_cache[text] = val
                self._query_cache.move_to_end(text)
                if len(self._query_cache) > _CACHE_MAX:
                    self._query_cache.popitem(last=False)
                    record_cache_evict("remote_embedding")
        return val

    @observe(tier="stage")
    def encode_query(self, text: str) -> bytes | None:
        prefix = MODEL_QUERY_PREFIX.get(self.model_name, "")
        return self.encode(prefix + text if prefix else text)

    @observe(tier="stage")
    def encode_document(self, text: str) -> bytes | None:
        prefix = MODEL_DOC_PREFIX.get(self.model_name, "")
        return self.encode(prefix + text if prefix else text)

    def encode_document_enriched(self, content: str, enriched_content: str | None = None) -> bytes:
        text = enriched_content if enriched_content else content
        return self.encode_document(text)

    @observe(tier="stage")
    def encode_adaptive(self, text: str, dimensions: int = None) -> bytes | None:
        raw = self.encode(text)
        if raw is None or dimensions is None:
            return raw
        arr = np.frombuffer(raw, dtype=np.float32)
        if dimensions < len(arr):
            arr = arr[:dimensions]
            norm = np.linalg.norm(arr)
            if norm > 0:
                arr = arr / norm
        return arr.tobytes()

    def encode_batch(self, texts: list[str]) -> list[bytes | None]:
        return self._call(texts, "document")

    def batch_reembed(self, texts: list[str]) -> list[bytes | None]:
        return self.encode_batch(texts)

    @observe(tier="hot")
    def similarity(self, embedding_a: bytes, embedding_b: bytes) -> float:
        a = np.frombuffer(embedding_a, dtype=np.float32)
        b = np.frombuffer(embedding_b, dtype=np.float32)
        dot = np.dot(a, b)
        norm = np.linalg.norm(a) * np.linalg.norm(b)
        return float(dot / norm) if norm > 0 else 0.0

    def search(
        self, query_embedding: bytes, candidate_embeddings: list[tuple[int, bytes]], top_k: int = 5
    ) -> list[tuple[int, float]]:
        scored = [(mid, self.similarity(query_embedding, emb)) for mid, emb in candidate_embeddings]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    # Stubs for API compatibility
    @staticmethod
    def quantize(embedding: bytes, bits: int = 8) -> bytes:
        from yadgar._shared.embeddings.embeddings import EmbeddingEngine

        return EmbeddingEngine.quantize(embedding, bits)

    @staticmethod
    def dequantize(quantized: bytes, bits: int = 8) -> bytes:
        from yadgar._shared.embeddings.embeddings import EmbeddingEngine

        return EmbeddingEngine.dequantize(quantized, bits)
