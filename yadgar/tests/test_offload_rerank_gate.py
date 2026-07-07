"""Heavy-rerank fan-out gate (#74 salvage — fix #2).

The offload (YADGAR_OFFLOAD_TOOLS=on) frees the loop, so up to TOOL_POOL_WORKERS
recalls run concurrently → that many concurrent backend /rerank requests. The
backend has FEWER cores than TOOL_POOL_WORKERS (default 2, v5.95); more concurrent
cross-encoder inferences saturate it → its /health goes slow → the core readiness probe times
out → 503 → P0 SIGKILLs the core. The cure is a process-wide semaphore bounding
how many concurrent backend reranks the core will issue, sized to the backend's
REAL serving capacity (a conservative default well BELOW the pool size), NOT a
flat TOOL_POOL_WORKERS.

These tests prove:
  - the gate bounds concurrent heavy-rerank entries to RECALL_HEAVY_CONCURRENCY,
    which defaults BELOW TOOL_POOL_WORKERS (else the gate is a no-op);
  - acquire honours a timeout and degrades (returns False) rather than blocking a
    worker forever (which would leak its pool slot — the very thing fix #3 avoids);
  - the gate is a process singleton (module-level), not per-instance, so multiple
    clients are bounded collectively;
  - knobs are read live from os.environ (test override), I25 three-way style.

OTEL is NOT touched at module scope. The pytest env controls OTEL_SDK_DISABLED.
"""

from __future__ import annotations

import threading
import time

import pytest

from yadgar._shared.runtime import offload as _offload


@pytest.fixture(autouse=True)
def _reset_gate(monkeypatch):
    """Each test starts with a fresh rerank gate + clean knobs."""
    monkeypatch.delenv("YADGAR_RECALL_HEAVY_CONCURRENCY", raising=False)
    monkeypatch.delenv("YADGAR_TOOL_POOL_WORKERS", raising=False)
    _offload.reset_rerank_gate()
    yield
    _offload.reset_rerank_gate()


def test_heavy_default_is_below_pool_workers():
    """The gate default MUST be < the pool size, or it is a no-op and #74 is unfixed.

    The binding constraint is backend cores (fewer than TOOL_POOL_WORKERS==2 after v5.95),
    so a flat pool would let all workers drive concurrent reranks saturating the backend.
    """
    heavy = _offload._heavy_concurrency()
    pool = _offload._pool_workers()
    assert heavy < pool, (
        f"heavy rerank concurrency ({heavy}) must default BELOW pool workers "
        f"({pool}); a flat default == pool is a no-op gate (#74 stays broken)"
    )
    assert heavy >= 1


def test_heavy_concurrency_env_override(monkeypatch):
    # Set pool > heavy so the clamp doesn't mask the override.
    monkeypatch.setenv("YADGAR_TOOL_POOL_WORKERS", "6")
    monkeypatch.setenv("YADGAR_RECALL_HEAVY_CONCURRENCY", "5")
    assert _offload._heavy_concurrency() == 5
    # Re-create the gate so it picks up the new size; 5 acquires must succeed, 6th fails.
    _offload.reset_rerank_gate()
    acquired = [_offload.acquire_rerank_slot(timeout=0.1) for _ in range(5)]
    try:
        assert all(acquired), "all 5 slots should acquire"
        assert _offload.acquire_rerank_slot(timeout=0.1) is False, "6th must fail at cap 5"
    finally:
        for _ in range(5):
            _offload.release_rerank_slot()


def test_gate_bounds_concurrent_entries(monkeypatch):
    """At most RECALL_HEAVY_CONCURRENCY callers hold the gate at once."""
    # Set pool > heavy so the pool clamp does not mask the gate test.
    monkeypatch.setenv("YADGAR_TOOL_POOL_WORKERS", "4")
    monkeypatch.setenv("YADGAR_RECALL_HEAVY_CONCURRENCY", "3")
    _offload.reset_rerank_gate()

    inside = 0
    max_inside = 0
    lock = threading.Lock()
    start = threading.Event()
    release = threading.Event()

    def worker():
        nonlocal inside, max_inside
        start.wait(timeout=5)
        acquired = _offload.acquire_rerank_slot(timeout=5.0)
        assert acquired, "should acquire within timeout"
        try:
            with lock:
                inside += 1
                max_inside = max(max_inside, inside)
            release.wait(timeout=5)
        finally:
            with lock:
                inside -= 1
            _offload.release_rerank_slot()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    start.set()
    # Let the first wave saturate the gate.
    time.sleep(0.3)
    with lock:
        observed = max_inside
    release.set()
    for t in threads:
        t.join(timeout=5)
    assert observed == 3, f"gate must cap concurrent entries at 3, saw {observed}"


def test_acquire_times_out_and_degrades(monkeypatch):
    """A full gate + a short acquire timeout → acquire returns False (degrade),
    never blocks the caller (worker) forever."""
    monkeypatch.setenv("YADGAR_RECALL_HEAVY_CONCURRENCY", "1")
    _offload.reset_rerank_gate()

    # Hold the single slot.
    assert _offload.acquire_rerank_slot(timeout=1.0) is True
    try:
        t0 = time.monotonic()
        got = _offload.acquire_rerank_slot(timeout=0.2)
        elapsed = time.monotonic() - t0
        assert got is False, "second acquire must fail (gate full)"
        assert elapsed < 1.0, "acquire must respect the timeout, not block"
    finally:
        _offload.release_rerank_slot()


def test_gate_is_process_singleton(monkeypatch):
    """The gate is module-level, so independent acquire/release pairs share it."""
    monkeypatch.setenv("YADGAR_RECALL_HEAVY_CONCURRENCY", "2")
    _offload.reset_rerank_gate()

    g1 = _offload._rerank_gate()
    g2 = _offload._rerank_gate()
    assert g1 is g2, "rerank gate must be a single shared object"
