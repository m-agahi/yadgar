"""The test that would have caught #74: concurrent offloaded recalls against a
LOADED/SLOW backend must NOT drive the core to SIGKILL itself.

Reproduces the #74 mechanism end-to-end through the REAL offload + rerank-gate +
liveness path (not a synthetic sleep tool):

  - offload ON, pool of POOL_WORKERS workers → the loop is free;
  - each "recall" body issues a backend /rerank through RemoteMLClient;
  - the backend is SLOW UNDER CONCURRENCY (its /rerank serializes — like a box
    with fewer cores than POOL_WORKERS, repro E4);
  - fire POOL_WORKERS+overflow concurrent recalls;
  - assert: (1) the backend NEVER sees more than RECALL_HEAVY_CONCURRENCY in-flight
    reranks (the gate holds), and (2) the core LIVENESS probe (/health/live) stays
    200 the WHOLE time — it never probes the backend, so backend busyness can't
    503 it → P0 never SIGKILLs the core.

Pre-fix (no gate, liveness == readiness probing the backend) this fails: the
backend saturates and the readiness probe times out → 503. The gate + the
liveness/readiness split are the fix.

OTEL untouched at module scope.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from unittest.mock import MagicMock

import httpx
import pytest

import yadgar.backend.ml_client as ml
import yadgar.config as cfg
import yadgar.server.http as srv_http
from yadgar.server import _offload


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setenv("YADGAR_OFFLOAD_TOOLS", "1")
    monkeypatch.setenv("YADGAR_TOOL_POOL_WORKERS", "8")
    monkeypatch.setenv("YADGAR_RECALL_HEAVY_CONCURRENCY", "2")
    monkeypatch.setenv("YADGAR_TOOL_TIMEOUT_SEC", "10")
    monkeypatch.setenv("YADGAR_TOOL_SATURATION_GRACE_SEC", "30")
    monkeypatch.setenv("YADGAR_CIRCUIT_BREAKER_ENABLED", "false")
    # liveness must not probe the backend even if these are set
    monkeypatch.setenv("YADGAR_DB_URL", "http://db.test")
    monkeypatch.setenv("YADGAR_EMBED_URL", "http://embed.test")
    cfg.get_settings.cache_clear()
    _offload.shutdown_pool()
    _offload.reset_rerank_gate()
    srv_http._reset_readiness_state()
    yield
    _offload.shutdown_pool()
    _offload.reset_rerank_gate()
    srv_http._reset_readiness_state()
    cfg.get_settings.cache_clear()


def _liveness_request() -> MagicMock:
    req = MagicMock()
    req.query_params = {}
    return req


async def test_loaded_backend_does_not_self_sigkill(monkeypatch):
    """Concurrent offloaded recalls + slow backend → backend reranks bounded by the
    gate AND liveness stays 200 throughout (no self-SIGKILL)."""
    backend_inflight = 0
    backend_max_inflight = 0
    blk = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal backend_inflight, backend_max_inflight
        with blk:
            backend_inflight += 1
            backend_max_inflight = max(backend_max_inflight, backend_inflight)
        # The backend is SLOW (CE inference under load).
        time.sleep(0.3)
        with blk:
            backend_inflight -= 1
        return httpx.Response(200, json={"scores": [0.5]})

    client = ml.RemoteMLClient(base_url="http://backend.test")
    client._client = httpx.Client(
        base_url="http://backend.test", transport=httpx.MockTransport(handler)
    )

    def recall_body(i: int) -> dict:
        # storage round-trip (fast) → heavy rerank → fusion glue (the real shape)
        time.sleep(0.02)
        scores = client.score_cross_encoder(f"q{i}", ["candidate text"])
        return {"i": i, "scores": scores}

    # Probe liveness continuously on the loop while the workers hammer the backend.
    live_codes: list[int] = []
    stop = asyncio.Event()

    async def liveness_ticker():
        while not stop.is_set():
            resp = await srv_http.liveness_check(_liveness_request())
            live_codes.append(resp.status_code)
            await asyncio.sleep(0.02)

    ticker = asyncio.create_task(liveness_ticker())

    # Fire 12 concurrent offloaded recalls (> pool of 8) through the real offload.
    results = await asyncio.gather(
        *(_offload.run_offloaded(recall_body, i) for i in range(12)),
        return_exceptions=True,
    )

    await asyncio.sleep(0.05)
    stop.set()
    await ticker

    # 1) The gate held — backend never saw more than RECALL_HEAVY_CONCURRENCY.
    assert backend_max_inflight <= 2, (
        f"backend saw {backend_max_inflight} concurrent reranks; the heavy gate must "
        "cap at 2 (unbounded fan-out is the #74 saturation trigger)"
    )

    # 2) Liveness stayed 200 the whole time — no self-SIGKILL.
    assert live_codes, "liveness ticker never ran"
    bad = [c for c in live_codes if c != 200]
    assert not bad, (
        f"liveness returned non-200 {bad} under a busy backend — that 503 is what "
        "P0 acts on to SIGKILL the core (#74). Liveness must never probe the backend."
    )

    # And the recalls completed (degraded recalls return scores=None, which is fine).
    assert all(not isinstance(r, BaseException) for r in results), (
        f"recall bodies raised: {[r for r in results if isinstance(r, BaseException)]}"
    )


def test_readiness_would_503_proving_the_split_matters(monkeypatch):
    """Contrast: a backend down past the anti-flap threshold DOES 503 readiness —
    proving readiness still detects real outages (monitoring), while liveness (above)
    stays 200. The split is what decouples the P0 kill from backend busyness."""
    monkeypatch.setenv("YADGAR_HEALTH_READINESS_FAIL_THRESHOLD", "3")

    async def _probe_down(_client, _url):
        return False

    from unittest.mock import patch

    with patch.object(srv_http, "_probe_dependency", _probe_down):
        codes = [
            asyncio.run(srv_http.health_check(_liveness_request())).status_code for _ in range(3)
        ]
        live = asyncio.run(srv_http.liveness_check(_liveness_request()))

    assert codes[-1] == 503, "readiness must eventually 503 on a real outage"
    assert live.status_code == 200, (
        "liveness stays 200 even when readiness 503s — backend outage must not SIGKILL the core"
    )
    # quiet the unused import warning
    _ = json
