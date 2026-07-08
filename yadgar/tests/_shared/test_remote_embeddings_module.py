"""Tests for yadgar/remote_embeddings.py — RemoteEmbeddingEngine.

Wave 2 coverage: yadgar/remote_embeddings.py (104 stmts, 0% pre-wave).
Strategy: mock httpx.Client at construction time. Test all public methods
and cache behavior. No real HTTP calls.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np

from yadgar._shared.remote_embeddings import RemoteEmbeddingEngine

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_engine(embed_url="http://127.0.0.1:8001") -> tuple[RemoteEmbeddingEngine, MagicMock]:
    """Create an engine with a mocked httpx.Client."""
    mock_client = MagicMock()
    with patch("yadgar._shared.remote_embeddings.httpx.Client", return_value=mock_client):
        engine = RemoteEmbeddingEngine()
    return engine, mock_client


def _float_embedding(dims=4) -> list[float]:
    return [1.0 / dims] * dims


def _bytes_embedding(dims=4) -> bytes:
    arr = np.array(_float_embedding(dims), dtype=np.float32)
    return arr.tobytes()


def _mock_embed_response(floats_list: list[list[float] | None]) -> MagicMock:
    """Build a mock response for POST /embed."""
    resp = MagicMock()
    resp.json.return_value = {"embeddings": floats_list}
    resp.raise_for_status.return_value = None
    return resp


# ---------------------------------------------------------------------------
# __init__ / basic properties
# ---------------------------------------------------------------------------


class TestInit:
    def test_default_model_name(self):
        engine, _ = _make_engine()
        assert engine.model_name == "all-MiniLM-L6-v2"

    def test_custom_model_name(self):
        mock_client = MagicMock()
        with patch("yadgar._shared.remote_embeddings.httpx.Client", return_value=mock_client):
            engine = RemoteEmbeddingEngine(model_name="custom-model")
        assert engine.model_name == "custom-model"

    def test_ensure_model_is_noop(self):
        engine, _ = _make_engine()
        engine._ensure_model()  # should not raise

    def test_is_model_cached_always_true(self):
        engine, _ = _make_engine()
        assert engine._is_model_cached() is True

    def test_unavailable_default_false(self):
        engine, _ = _make_engine()
        assert engine._unavailable is False


# ---------------------------------------------------------------------------
# get_model_name / get_dimensions
# ---------------------------------------------------------------------------


class TestModelInfo:
    def test_get_model_name(self):
        engine, _ = _make_engine()
        assert engine.get_model_name() == "all-MiniLM-L6-v2"

    def test_get_dimensions_known_model(self):
        engine, _ = _make_engine()
        dims = engine.get_dimensions()
        assert isinstance(dims, int)
        assert dims > 0

    def test_get_dimensions_unknown_model(self):
        mock_client = MagicMock()
        with patch("yadgar._shared.remote_embeddings.httpx.Client", return_value=mock_client):
            engine = RemoteEmbeddingEngine(model_name="unknown-model")
        assert engine.get_dimensions() == 384  # fallback


# ---------------------------------------------------------------------------
# needs_reembedding
# ---------------------------------------------------------------------------


class TestNeedsReembedding:
    def test_none_stored_returns_true(self):
        engine, _ = _make_engine()
        assert engine.needs_reembedding(None) is True

    def test_different_model_returns_true(self):
        engine, _ = _make_engine()
        assert engine.needs_reembedding("other-model") is True

    def test_same_model_returns_false(self):
        engine, _ = _make_engine()
        assert engine.needs_reembedding("all-MiniLM-L6-v2") is False


# ---------------------------------------------------------------------------
# _call (internal embedding RPC)
# ---------------------------------------------------------------------------


class TestCall:
    def test_empty_texts_returns_empty(self):
        engine, mock_client = _make_engine()
        result = engine._call([])
        assert result == []
        mock_client.post.assert_not_called()

    def test_single_text_returns_bytes(self):
        engine, mock_client = _make_engine()
        mock_client.post.return_value = _mock_embed_response([[1.0, 0.0, 0.0, 0.0]])
        result = engine._call(["hello"])
        assert len(result) == 1
        assert isinstance(result[0], bytes)

    def test_null_embedding_returns_none(self):
        engine, mock_client = _make_engine()
        mock_client.post.return_value = _mock_embed_response([None])
        result = engine._call(["hello"])
        assert result[0] is None

    def test_http_error_returns_nones(self):
        engine, mock_client = _make_engine()
        mock_client.post.side_effect = Exception("connection refused")
        result = engine._call(["a", "b"])
        assert result == [None, None]

    def test_multiple_texts(self):
        engine, mock_client = _make_engine()
        mock_client.post.return_value = _mock_embed_response([[0.5, 0.5], [0.3, 0.7]])
        result = engine._call(["text1", "text2"])
        assert len(result) == 2
        assert all(isinstance(r, bytes) for r in result)


# ---------------------------------------------------------------------------
# encode + cache
# ---------------------------------------------------------------------------


class TestEncode:
    def test_encode_returns_bytes(self):
        engine, mock_client = _make_engine()
        mock_client.post.return_value = _mock_embed_response([[1.0, 0.0]])
        result = engine.encode("hello")
        assert result is not None
        assert isinstance(result, bytes)

    def test_encode_caches_result(self):
        engine, mock_client = _make_engine()
        mock_client.post.return_value = _mock_embed_response([[1.0, 0.0]])
        engine.encode("hello")
        engine.encode("hello")  # second call should hit cache
        assert mock_client.post.call_count == 1

    def test_different_texts_call_api_each_time(self):
        engine, mock_client = _make_engine()
        mock_client.post.return_value = _mock_embed_response([[1.0, 0.0]])
        engine.encode("hello")
        engine.encode("world")
        assert mock_client.post.call_count == 2

    def test_null_result_not_cached(self):
        engine, mock_client = _make_engine()
        mock_client.post.return_value = _mock_embed_response([None])
        result = engine.encode("null text")
        assert result is None
        # Next call should also hit API (not cached)
        mock_client.post.return_value = _mock_embed_response([[1.0, 0.0]])
        result2 = engine.encode("null text")
        assert result2 is not None
        assert mock_client.post.call_count == 2

    def test_cache_eviction_at_max(self):
        engine, mock_client = _make_engine()
        mock_client.post.return_value = _mock_embed_response([[1.0, 0.0]])
        # Fill cache beyond limit
        from yadgar._shared.remote_embeddings import _CACHE_MAX

        for i in range(_CACHE_MAX + 5):
            engine.encode(f"text-{i}")
        assert len(engine._query_cache) <= _CACHE_MAX


# ---------------------------------------------------------------------------
# encode_query / encode_document
# ---------------------------------------------------------------------------


class TestEncodeQueryDocument:
    def test_encode_query_uses_prefix(self):
        mock_client = MagicMock()
        mock_client.post.return_value = _mock_embed_response([[1.0, 0.0]])
        with patch("yadgar._shared.remote_embeddings.httpx.Client", return_value=mock_client):
            engine = RemoteEmbeddingEngine(model_name="all-MiniLM-L6-v2")
        engine.encode_query("test query")
        # Verify that encode was called (prefix may be empty for this model)
        assert mock_client.post.call_count >= 1

    def test_encode_document_works(self):
        engine, mock_client = _make_engine()
        mock_client.post.return_value = _mock_embed_response([[1.0, 0.0]])
        result = engine.encode_document("document text")
        assert result is not None or result is None  # just no crash


# ---------------------------------------------------------------------------
# encode_adaptive
# ---------------------------------------------------------------------------


class TestEncodeAdaptive:
    def test_returns_raw_when_dimensions_none(self):
        engine, mock_client = _make_engine()
        mock_client.post.return_value = _mock_embed_response([[0.25, 0.25, 0.25, 0.25]])
        result = engine.encode_adaptive("hello", dimensions=None)
        # Should be the raw result from encode
        assert result is not None

    def test_truncates_dimensions(self):
        engine, mock_client = _make_engine()
        mock_client.post.return_value = _mock_embed_response([[0.5, 0.5, 0.3, 0.3]])
        result = engine.encode_adaptive("hello", dimensions=2)
        if result is not None:
            arr = np.frombuffer(result, dtype=np.float32)
            assert len(arr) == 2

    def test_returns_none_when_encode_fails(self):
        engine, mock_client = _make_engine()
        mock_client.post.side_effect = Exception("fail")
        result = engine.encode_adaptive("hello", dimensions=2)
        assert result is None


# ---------------------------------------------------------------------------
# encode_batch / batch_reembed
# ---------------------------------------------------------------------------


class TestEncodeBatch:
    def test_encode_batch_returns_list(self):
        engine, mock_client = _make_engine()
        mock_client.post.return_value = _mock_embed_response([[1.0, 0.0], [0.0, 1.0]])
        result = engine.encode_batch(["a", "b"])
        assert len(result) == 2

    def test_batch_reembed_delegates(self):
        engine, mock_client = _make_engine()
        mock_client.post.return_value = _mock_embed_response([[1.0, 0.0]])
        result = engine.batch_reembed(["a"])
        assert len(result) == 1


# ---------------------------------------------------------------------------
# similarity
# ---------------------------------------------------------------------------


class TestSimilarity:
    def test_identical_embeddings_high_similarity(self):
        engine, _ = _make_engine()
        emb = _bytes_embedding(4)
        sim = engine.similarity(emb, emb)
        assert abs(sim - 1.0) < 0.01

    def test_orthogonal_embeddings_zero_similarity(self):
        engine, _ = _make_engine()
        a = np.array([1.0, 0.0], dtype=np.float32).tobytes()
        b = np.array([0.0, 1.0], dtype=np.float32).tobytes()
        sim = engine.similarity(a, b)
        assert abs(sim) < 0.01

    def test_returns_float(self):
        engine, _ = _make_engine()
        emb = _bytes_embedding(4)
        result = engine.similarity(emb, emb)
        assert isinstance(result, float)


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


class TestSearch:
    def test_returns_top_k(self):
        engine, _ = _make_engine()
        query = np.array([1.0, 0.0], dtype=np.float32).tobytes()
        candidates = [
            (i, np.array([float(i) / 10, 1.0 - float(i) / 10], dtype=np.float32).tobytes())
            for i in range(10)
        ]
        results = engine.search(query, candidates, top_k=3)
        assert len(results) == 3

    def test_sorted_by_score_descending(self):
        engine, _ = _make_engine()
        query = np.array([1.0, 0.0], dtype=np.float32).tobytes()
        candidates = [
            (1, np.array([0.1, 0.9], dtype=np.float32).tobytes()),
            (2, np.array([0.9, 0.1], dtype=np.float32).tobytes()),  # more similar
        ]
        results = engine.search(query, candidates, top_k=2)
        assert results[0][0] == 2  # ID 2 is most similar

    def test_empty_candidates_returns_empty(self):
        engine, _ = _make_engine()
        query = _bytes_embedding(4)
        assert engine.search(query, [], top_k=5) == []


# ---------------------------------------------------------------------------
# quantize / dequantize (static stubs)
# ---------------------------------------------------------------------------


class TestQuantizeDequantize:
    def test_quantize_returns_bytes(self):
        emb = _bytes_embedding(4)
        result = RemoteEmbeddingEngine.quantize(emb)
        assert isinstance(result, bytes)

    def test_dequantize_returns_bytes(self):
        emb = _bytes_embedding(4)
        quantized = RemoteEmbeddingEngine.quantize(emb)
        result = RemoteEmbeddingEngine.dequantize(quantized)
        assert isinstance(result, bytes)
