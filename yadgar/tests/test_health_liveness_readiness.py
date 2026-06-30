"""Liveness/readiness split + readiness anti-flap (#74 salvage — fix #1).

Root cause: the container P0 healthcheck (`curl -f /health`) probes READINESS,
which probes the backend embed/db with a 2s timeout. A transiently-busy backend
(saturated by concurrent reranks) makes that probe time out → readiness 503 →
P0 `--health-on-failure=kill` SIGKILLs the core. A busy dependency must NEVER
SIGKILL the core.

The fix:
  - LIVENESS (`/health/live`): answerable from the core's own loop WITHOUT any
    backend probe. 200 normally; 503 ONLY when the tool pool is genuinely WEDGED
    (pool_saturated() — in-memory counters, no network). A busy-but-draining
    backend never trips it (E3 in the repro proves saturation does not fire under
    concurrent in-flight reranks). P0 watches THIS.
  - READINESS (`/health`): keeps the db+embed probe for monitoring, but is
    ANTI-FLAP — a single transient probe miss does NOT flip to 503; it requires N
    consecutive failures.

These tests prove both, RED→GREEN:
  - /health/live route exists, is 200 with no backend, 503 iff pool saturated;
  - /health/live makes NO outbound dependency probe;
  - /health readiness does not 503 on a single transient miss (anti-flap), but
    does after N consecutive misses.

OTEL untouched at module scope.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

import yadgar.server.http as srv_http


def _make_request() -> MagicMock:
    req = MagicMock()
    req.query_params = {}
    return req


def _body(resp) -> dict:
    return json.loads(bytes(resp.body).decode())


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    # Reset the readiness anti-flap counter between tests.
    srv_http._reset_readiness_state()
    # No real backend.
    monkeypatch.delenv("YADGAR_DB_URL", raising=False)
    monkeypatch.delenv("YADGAR_EMBED_URL", raising=False)
    yield
    srv_http._reset_readiness_state()


# ---------------------------------------------------------------------------
# Liveness
# ---------------------------------------------------------------------------


def test_liveness_route_returns_200_with_no_backend():
    """Liveness is answerable from the loop alone → 200 even with no backend."""
    resp = asyncio.run(srv_http.liveness_check(_make_request()))
    assert resp.status_code == 200
    assert _body(resp)["status"] == "ok"


def test_liveness_makes_no_backend_probe(monkeypatch):
    """Liveness must NOT probe the backend (that coupling is the #74 root cause)."""
    monkeypatch.setenv("YADGAR_DB_URL", "http://db.test")
    monkeypatch.setenv("YADGAR_EMBED_URL", "http://embed.test")

    with patch("httpx.AsyncClient") as mock_client_cls:
        asyncio.run(srv_http.liveness_check(_make_request()))
        assert not mock_client_cls.called, (
            "liveness must NOT open an httpx client / probe the backend"
        )


def test_liveness_503_only_when_pool_saturated():
    """Liveness 503 iff the tool pool is genuinely wedged (preserves O2 P0-kill)."""
    with patch("yadgar.server._offload.pool_saturated", return_value=True):
        resp = asyncio.run(srv_http.liveness_check(_make_request()))
    assert resp.status_code == 503, (
        "a wedged pool must still drive liveness 503 so P0 can kill (O2 preserved)"
    )


def test_liveness_200_when_pool_busy_but_not_saturated():
    """A busy-but-draining pool (not saturated) keeps liveness 200 — no self-kill."""
    with patch("yadgar.server._offload.pool_saturated", return_value=False):
        resp = asyncio.run(srv_http.liveness_check(_make_request()))
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Readiness anti-flap
# ---------------------------------------------------------------------------


def test_readiness_does_not_flap_on_single_transient_miss(monkeypatch):
    """A single transient embed-probe miss must NOT flip readiness to 503."""
    monkeypatch.setenv("YADGAR_EMBED_URL", "http://embed.test")
    monkeypatch.setenv("YADGAR_HEALTH_READINESS_FAIL_THRESHOLD", "3")

    async def _probe(_client, _url):
        return False  # one transient miss

    with patch.object(srv_http, "_probe_dependency", _probe):
        resp = asyncio.run(srv_http.health_check(_make_request()))
    assert resp.status_code == 200, (
        "single transient probe miss must not 503 (anti-flap); threshold=3"
    )


def test_readiness_503_after_n_consecutive_misses(monkeypatch):
    """N consecutive misses DO flip readiness to 503 (genuine outage detected)."""
    monkeypatch.setenv("YADGAR_EMBED_URL", "http://embed.test")
    monkeypatch.setenv("YADGAR_HEALTH_READINESS_FAIL_THRESHOLD", "3")

    async def _probe(_client, _url):
        return False

    with patch.object(srv_http, "_probe_dependency", _probe):
        codes = [asyncio.run(srv_http.health_check(_make_request())).status_code for _ in range(3)]
    assert codes == [200, 200, 503], (
        f"readiness must 503 only after 3 consecutive misses, got {codes}"
    )


def test_readiness_recovers_resets_counter(monkeypatch):
    """A single success resets the consecutive-failure counter (no latent flip)."""
    monkeypatch.setenv("YADGAR_EMBED_URL", "http://embed.test")
    monkeypatch.setenv("YADGAR_HEALTH_READINESS_FAIL_THRESHOLD", "3")

    state = {"ok": False}

    async def _probe(_client, _url):
        return state["ok"]

    with patch.object(srv_http, "_probe_dependency", _probe):
        asyncio.run(srv_http.health_check(_make_request()))  # miss 1
        asyncio.run(srv_http.health_check(_make_request()))  # miss 2
        state["ok"] = True
        asyncio.run(srv_http.health_check(_make_request()))  # success → reset
        state["ok"] = False
        resp = asyncio.run(srv_http.health_check(_make_request()))  # miss 1 again
    assert resp.status_code == 200, "counter must reset on success — not latent-503"
