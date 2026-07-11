"""RemoteMLClient._rerank_rpc honours the heavy-rerank fan-out gate (#74 fix #2).

The gate (server/_offload heavy-rerank semaphore) bounds how many concurrent
backend /rerank requests the core issues, regardless of how many offload workers
are live. Without it, TOOL_POOL_WORKERS(8) concurrent recalls drive 8 concurrent
backend reranks → backend saturates (fewer cores) → core readiness 503 → P0 kill.

These tests prove:
  - concurrent score_cross_encoder calls never exceed RECALL_HEAVY_CONCURRENCY
    in-flight at the backend (the load-bearing assertion);
  - when the gate is full and acquire times out, the call DEGRADES (returns None →
    pre-rerank order, reusing the breaker-open path) rather than hanging a worker
    forever (which would leak its pool slot — fix #3's concern);
  - HALF_OPEN circuit-breaker probes BYPASS the gate so they fast-fail.

OTEL untouched at module scope.
"""

from __future__ import annotations

import threading
import time

import httpx
import pytest

import yadgar._shared.config as cfg
import yadgar.backend.ml_client.ml_client as ml
from yadgar._shared.runtime import offload as _offload


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.delenv("YADGAR_RECALL_HEAVY_CONCURRENCY", raising=False)
    cfg.get_settings.cache_clear()
    _offload.reset_rerank_gate()
    yield
    _offload.reset_rerank_gate()
    cfg.get_settings.cache_clear()


def _make_client(handler) -> ml.RemoteMLClient:
    """Build a RemoteMLClient whose httpx.Client uses a MockTransport handler."""
    client = ml.RemoteMLClient(base_url="http://backend.test")
    client._client = httpx.Client(
        base_url="http://backend.test",
        transport=httpx.MockTransport(handler),
    )
    return client


def test_concurrent_rerank_bounded_by_gate(monkeypatch):
    """N concurrent score_cross_encoder calls → at most HEAVY in flight at backend."""
    monkeypatch.setenv("YADGAR_RECALL_HEAVY_CONCURRENCY", "2")
    monkeypatch.setenv("YADGAR_CIRCUIT_BREAKER_ENABLED", "false")
    cfg.get_settings.cache_clear()
    _offload.reset_rerank_gate()

    inflight = 0
    max_inflight = 0
    lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal inflight, max_inflight
        with lock:
            inflight += 1
            max_inflight = max(max_inflight, inflight)
        time.sleep(0.2)  # simulate the slow CE inference
        with lock:
            inflight -= 1
        return httpx.Response(200, json={"scores": [0.5]})

    client = _make_client(handler)

    def call():
        client.score_cross_encoder("q", ["text"])

    threads = [threading.Thread(target=call) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert max_inflight <= 2, (
        f"backend saw {max_inflight} concurrent reranks; gate must cap at 2 "
        "(8 unbounded is the #74 crash trigger)"
    )


def test_gate_full_degrades_to_none(monkeypatch):
    """When the gate cannot be acquired in time, the call returns None (degrade)."""
    monkeypatch.setenv("YADGAR_RECALL_HEAVY_CONCURRENCY", "1")
    monkeypatch.setenv("YADGAR_RERANK_GATE_ACQUIRE_TIMEOUT_SEC", "0.2")
    monkeypatch.setenv("YADGAR_CIRCUIT_BREAKER_ENABLED", "false")
    cfg.get_settings.cache_clear()
    _offload.reset_rerank_gate()

    # Externally hold the single slot for the duration of the call.
    assert _offload.acquire_rerank_slot(timeout=1.0) is True

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        return httpx.Response(200, json={"scores": [0.5]})

    client = _make_client(handler)
    try:
        t0 = time.monotonic()
        result = client.score_cross_encoder("q", ["text"])
        elapsed = time.monotonic() - t0
        assert result is None, "gate-full call must degrade to None (skip rerank)"
        assert elapsed < 1.0, f"must not hang waiting on the gate (took {elapsed:.1f}s)"
    finally:
        _offload.release_rerank_slot()


def test_half_open_probe_bypasses_gate(monkeypatch):
    """A HALF_OPEN breaker probe must NOT block on the gate (probes fast-fail)."""
    monkeypatch.setenv("YADGAR_RECALL_HEAVY_CONCURRENCY", "1")
    monkeypatch.setenv("YADGAR_CIRCUIT_BREAKER_ENABLED", "true")
    monkeypatch.setenv("YADGAR_CIRCUIT_BREAKER_FAILURE_THRESHOLD", "1")
    cfg.get_settings.cache_clear()
    _offload.reset_rerank_gate()

    served = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        served["n"] += 1
        return httpx.Response(200, json={"scores": [0.5]})

    client = _make_client(handler)
    # Force the ce breaker into HALF_OPEN.
    breaker = client._breakers["ce"]
    breaker._state = "half_open"

    # Hold the only gate slot so a gated probe would block/degrade.
    assert _offload.acquire_rerank_slot(timeout=1.0) is True
    try:
        result = client.score_cross_encoder("q", ["text"])
        # Probe bypassed the gate → reached the backend and got scores.
        assert result == [0.5], "HALF_OPEN probe must bypass the gate and reach the backend"
        assert served["n"] == 1
    finally:
        _offload.release_rerank_slot()
