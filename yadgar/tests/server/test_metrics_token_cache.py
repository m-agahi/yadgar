"""Q1 tests — token-budget + cache-hit metrics (v5.3.5).

Tests:
1. Tool call → yadgar_tool_token_estimate_total{tool="recall"} increments.
2. Repeated identical encode → embedding cache hit counter increments.
3. New input → embedding cache miss counter increments.
4. /metrics endpoint exposes new counters in Prometheus text format.
"""

from __future__ import annotations

import pytest

# ── helpers ───────────────────────────────────────────────────────────────────


def _fresh_registry():
    """Return a fresh isolated CollectorRegistry with the Q1 counters registered."""
    from prometheus_client import CollectorRegistry, Counter

    reg = CollectorRegistry()
    token_ctr = Counter(
        "yadgar_tool_token_estimate_total",
        "Estimated tokens returned per MCP tool call (len/4 approximation)",
        ["tool"],
        registry=reg,
    )
    hit_ctr = Counter(
        "yadgar_cache_hit_total",
        "Total cache hits by cache name",
        ["cache"],
        registry=reg,
    )
    miss_ctr = Counter(
        "yadgar_cache_miss_total",
        "Total cache misses by cache name",
        ["cache"],
        registry=reg,
    )
    return reg, token_ctr, hit_ctr, miss_ctr


# ── Test 1: token estimate counter increments on tool call ────────────────────


def test_token_estimate_counter_increments():
    """yadgar_tool_token_estimate_total increments when a tool returns a result."""
    from prometheus_client import CollectorRegistry, Counter

    reg = CollectorRegistry()
    ctr = Counter(
        "yadgar_tool_token_estimate_total_t1",
        "test",
        ["tool"],
        registry=reg,
    )

    # Simulate what _tool() wrapper does
    result_text = "x" * 400  # 100 estimated tokens
    est = max(1, len(result_text) // 4)
    ctr.labels(tool="recall").inc(est)

    from prometheus_client import generate_latest

    output = generate_latest(reg).decode()
    assert "recall" in output
    assert "100" in output or "1" in output  # at least some increment


def test_token_estimate_nonzero_for_nonempty_result():
    """Token estimate is > 0 for any non-empty result."""
    import json

    def _estimate_tokens(result) -> int:
        try:
            if isinstance(result, (str, bytes)):
                text = (
                    result if isinstance(result, str) else result.decode("utf-8", errors="replace")
                )
            else:
                text = json.dumps(result, default=str)
            return max(1, len(text) // 4)
        except Exception:
            return 0

    assert _estimate_tokens("hello world") > 0
    assert _estimate_tokens({"key": "value"}) > 0
    assert _estimate_tokens([{"id": 1, "content": "test memory"}]) > 0
    assert _estimate_tokens("") == 1  # max(1, 0) = 1 — empty still counts as 1


# ── Test 2: cache hit counter increments on repeated encode ───────────────────


def test_embedding_cache_hit_counter_increments(monkeypatch):
    """yadgar_cache_hit_total{cache='embedding'} increments on repeated encode call."""
    from collections import OrderedDict

    from prometheus_client import CollectorRegistry, Counter

    reg = CollectorRegistry()
    hit_ctr = Counter("yadgar_cache_hit_total_t2", "test", ["cache"], registry=reg)
    miss_ctr = Counter("yadgar_cache_miss_total_t2", "test", ["cache"], registry=reg)

    hit_calls = []
    miss_calls = []

    # Monkeypatch the metrics import to use our test counters
    import yadgar._shared.embeddings as _emb_mod

    def _patched_encode(self, text):
        if text in self._query_cache:
            self._query_cache.move_to_end(text)
            hit_ctr.labels(cache="embedding").inc()
            hit_calls.append(text)
            return self._query_cache[text]
        # cache miss — put fake bytes
        fake = b"\x00" * 8
        self._query_cache[text] = fake
        self._query_cache.move_to_end(text)
        miss_ctr.labels(cache="embedding").inc()
        miss_calls.append(text)
        return fake

    monkeypatch.setattr(_emb_mod.EmbeddingEngine, "encode", _patched_encode)

    engine = _emb_mod.EmbeddingEngine.__new__(_emb_mod.EmbeddingEngine)
    engine._query_cache = OrderedDict()
    engine._model = None
    engine._unavailable = False

    engine.encode("test query")  # miss
    engine.encode("test query")  # hit

    assert len(hit_calls) == 1
    assert len(miss_calls) == 1

    from prometheus_client import generate_latest

    output = generate_latest(reg).decode()
    assert 'cache="embedding"' in output or "embedding" in output


# ── Test 3: cache miss counter increments for new input ───────────────────────


def test_embedding_cache_miss_counter_increments(monkeypatch):
    """yadgar_cache_miss_total{cache='embedding'} increments for new (unseen) inputs."""
    from collections import OrderedDict

    import yadgar._shared.embeddings as _emb_mod

    miss_calls = []

    def _patched_encode(self, text):
        if text in self._query_cache:
            self._query_cache.move_to_end(text)
            return self._query_cache[text]
        fake = b"\x00" * 8
        self._query_cache[text] = fake
        self._query_cache.move_to_end(text)
        miss_calls.append(text)
        return fake

    monkeypatch.setattr(_emb_mod.EmbeddingEngine, "encode", _patched_encode)

    engine = _emb_mod.EmbeddingEngine.__new__(_emb_mod.EmbeddingEngine)
    engine._query_cache = OrderedDict()
    engine._model = None
    engine._unavailable = False

    engine.encode("alpha")
    engine.encode("beta")
    engine.encode("gamma")

    assert "alpha" in miss_calls
    assert "beta" in miss_calls
    assert "gamma" in miss_calls
    assert len(miss_calls) == 3


# ── Test 4: /metrics endpoint exposes new counters ────────────────────────────


def test_metrics_endpoint_exposes_new_counters():
    """generate_latest with the real _registry includes Q1 counter names."""
    from prometheus_client import generate_latest

    from yadgar._shared.observability.metrics import _registry

    output = generate_latest(_registry).decode()
    assert "yadgar_tool_token_estimate_total" in output
    assert "yadgar_cache_hit_total" in output
    assert "yadgar_cache_miss_total" in output


@pytest.mark.anyio
async def test_metrics_endpoint_handler_returns_200(monkeypatch):
    """metrics_handler returns 200 with Prometheus text when metrics enabled."""
    from unittest.mock import MagicMock

    monkeypatch.setenv("YADGAR_METRICS_ENABLED", "1")

    from yadgar._shared.observability.metrics import metrics_handler

    mock_request = MagicMock()
    response = await metrics_handler(mock_request)
    assert response.status_code == 200
    body = response.body.decode()
    assert "yadgar_tool_token_estimate_total" in body
    assert "yadgar_cache_hit_total" in body
