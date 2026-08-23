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

import yadgar.core.server.http as srv_http


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
    with patch("yadgar._shared.runtime.offload.pool_saturated", return_value=True):
        resp = asyncio.run(srv_http.liveness_check(_make_request()))
    assert resp.status_code == 503, (
        "a wedged pool must still drive liveness 503 so P0 can kill (O2 preserved)"
    )


def test_liveness_200_when_pool_busy_but_not_saturated():
    """A busy-but-draining pool (not saturated) keeps liveness 200 — no self-kill."""
    with patch("yadgar._shared.runtime.offload.pool_saturated", return_value=False):
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


# ---------------------------------------------------------------------------
# Task 67 — the probe field MUST agree with status during the anti-flap window.
#
# Pre-fix: /health returned ``status: ok`` for the first ~2 misses while reporting
# ``embed: false`` the whole time — an operator staring at ``curl /health`` saw a
# payload that contradicted itself. The fix gates ``db`` / ``embed`` on the same
# consecutive-failure counter that gates ``status``: the field reads ``true``
# while the readiness verdict still says ok, and only flips to ``false`` once
# the verdict itself has degraded.
#
# PR #65 review finding #6 (CARRIED-FORWARD ON TOP of task 67): masking
# per-field ``db`` / ``embed`` during the grace window trades diagnostic
# signal for flap-suppression purity. An operator who sees ``embed: true`` for
# 3 probe intervals while the backend is actually down cannot tell whether
# the probe is configured wrong, the probe is anti-flapping, or the probe is
# lying. The per-field value is supposed to be the truth of the LAST PROBE
# — the anti-flap grace lives on ``status`` (which DOES drive P0 503), not on
# the diagnostic field. Post-#6, ``db`` / ``embed`` reflect REAL probe outcomes
# (no masking); ``status`` keeps its anti-flap gate (P0 kill semantics
# preserved, O2 honoured, O1 satisfied). Test 1 (under-threshold embed) is
# inverted: it now asserts the truth surfaces, not the masked value.
# ---------------------------------------------------------------------------


class TestProbeFieldsAgreeWithStatus:
    def test_probe_field_reports_truth_under_threshold_misses(self, monkeypatch):
        """Under threshold misses, status stays ok BUT embed must read FALSE.

        PR #65 review finding #6: per-field ``db`` / ``embed`` are
        diagnostic — they MUST surface the actual probe outcome even while
        ``status`` is anti-flapping. The anti-flap grace lives on ``status``
        (which drives P0 503), not on the diagnostic field. An operator who
        sees ``embed: true`` while the backend is genuinely down cannot tell
        whether the probe is broken, masked, or wrong. The trade is explicit:
        flap-suppression purity on ``status`` (P0 kill semantics, O2), REAL
        per-field truth on ``db`` / ``embed`` (diagnostic signal, O1).
        """
        monkeypatch.setenv("YADGAR_EMBED_URL", "http://embed.test")
        monkeypatch.setenv("YADGAR_DB_URL", "http://db.test")
        monkeypatch.setenv("YADGAR_HEALTH_READINESS_FAIL_THRESHOLD", "3")

        async def _probe(_client, _url):
            return False  # every probe misses

        with patch.object(srv_http, "_probe_dependency", _probe):
            resp = asyncio.run(srv_http.health_check(_make_request()))
        body = _body(resp)
        assert resp.status_code == 200, "anti-flap must keep status ok under threshold"
        assert body["status"] == "ok", body
        assert body["embed"] is False, (
            "embed field MUST surface the real probe outcome under the grace "
            "window (PR #65 review finding #6). status stays anti-flapped so "
            "P0 does not self-kill on a transient miss; the per-field value is "
            "the diagnostic that tells the operator WHY status still says ok."
        )
        assert body["db"] is False, "same for db — per-field truth, not anti-flapped"

    def test_probe_field_flips_false_when_status_degrades(self, monkeypatch):
        """Once the counter crosses the threshold, status AND the field flip together."""
        monkeypatch.setenv("YADGAR_EMBED_URL", "http://embed.test")
        monkeypatch.setenv("YADGAR_HEALTH_READINESS_FAIL_THRESHOLD", "3")

        async def _probe(_client, _url):
            return False

        with patch.object(srv_http, "_probe_dependency", _probe):
            for _ in range(2):
                asyncio.run(srv_http.health_check(_make_request()))
            resp = asyncio.run(srv_http.health_check(_make_request()))  # 3rd miss → degraded
        body = _body(resp)
        assert resp.status_code == 503, "the 3rd miss must degrade status"
        assert body["status"] == "degraded"
        assert body["embed"] is False, (
            "embed must flip to false in lockstep with status — same counter, "
            "same verdict (task 67, C4)"
        )

    def test_probe_field_reports_truth_when_healthy(self, monkeypatch):
        """When the probe is healthy, db/embed reflect reality (not anti-flapped).

        Anti-flap is for misses; a true positive must still surface. If we gated
        on ``status: ok`` instead of the counter, a healthy probe on a degraded
        counter would lie as ``db: false``.
        """
        monkeypatch.setenv("YADGAR_EMBED_URL", "http://embed.test")
        monkeypatch.setenv("YADGAR_DB_URL", "http://db.test")

        async def _probe(_client, _url):
            return True

        with patch.object(srv_http, "_probe_dependency", _probe):
            resp = asyncio.run(srv_http.health_check(_make_request()))
        body = _body(resp)
        assert body["status"] == "ok"
        assert body["db"] is True and body["embed"] is True, (
            "healthy probes must report truth, not be masked by anti-flap (task 67, C4)"
        )

    def test_probe_field_recovers_alongside_status(self, monkeypatch):
        """A recovery resets the counter AND clears the field; no latent lie.

        Mirror of ``test_readiness_recovers_resets_counter`` but for the
        payload field — if the counter resets to zero on a probe success, the
        field must immediately agree, not keep reading ``false`` for an extra
        probe cycle.
        """
        monkeypatch.setenv("YADGAR_EMBED_URL", "http://embed.test")
        monkeypatch.setenv("YADGAR_HEALTH_READINESS_FAIL_THRESHOLD", "3")

        state = {"ok": False}

        async def _probe(_client, _url):
            return state["ok"]

        with patch.object(srv_http, "_probe_dependency", _probe):
            asyncio.run(srv_http.health_check(_make_request()))  # miss 1
            asyncio.run(srv_http.health_check(_make_request()))  # miss 2
            state["ok"] = True
            resp = asyncio.run(srv_http.health_check(_make_request()))  # success
        body = _body(resp)
        assert body["status"] == "ok"
        assert body["embed"] is True, (
            "recovery must clear embed in the same call as status — a single "
            "probe success resets the counter, the field has no excuse to "
            "lag (task 67, C4)"
        )


# ---------------------------------------------------------------------------
# PR #65 review finding #9 — threshold lookup on every probe.
#
# Pre-fix: ``_apply_readiness_antiflap`` called ``_readiness_fail_threshold()``
# unconditionally — the knob resolver reads env / settings / YAML / default
# on EVERY /health probe, healthy or not. The healthy path is the common path,
# so this is a hot-path cost with no benefit: threshold is only meaningful
# when a probe misses (the verdict-flip branch). Post-#9, the lookup moves
# INSIDE the ``dependency_down`` branch, so a healthy probe skips it entirely.
# Pin: a healthy-probe cycle calls ``_readiness_fail_threshold`` ZERO times.
# ---------------------------------------------------------------------------


class TestThresholdLookupOnlyOnFailure:
    def test_healthy_probe_does_not_consult_threshold(self, monkeypatch):
        """A healthy probe must NOT trigger the threshold resolver.

        ``_readiness_fail_threshold`` reads env / settings / YAML / default
        on every call — pin that the healthy path skips it entirely (PR #65
        review finding #9). The threshold is only meaningful when a probe
        misses, so calling it on every probe is a hot-path cost with no
        behavioural benefit.
        """
        monkeypatch.setenv("YADGAR_EMBED_URL", "http://embed.test")
        monkeypatch.setenv("YADGAR_DB_URL", "http://db.test")

        async def _probe(_client, _url):
            return True

        with (
            patch.object(srv_http, "_probe_dependency", _probe),
            patch.object(
                srv_http, "_readiness_fail_threshold", wraps=srv_http._readiness_fail_threshold
            ) as spy_threshold,
        ):
            resp = asyncio.run(srv_http.health_check(_make_request()))
        body = _body(resp)
        assert body["status"] == "ok"
        assert body["db"] is True and body["embed"] is True
        assert spy_threshold.call_count == 0, (
            f"healthy probe must NOT consult _readiness_fail_threshold (PR #65 "
            f"review finding #9); got {spy_threshold.call_count} calls on a "
            f"single healthy-probe cycle"
        )

    def test_failing_probe_consults_threshold_once(self, monkeypatch):
        """A failing probe DOES consult the threshold — once per miss cycle."""
        monkeypatch.setenv("YADGAR_EMBED_URL", "http://embed.test")
        monkeypatch.setenv("YADGAR_DB_URL", "http://db.test")
        monkeypatch.setenv("YADGAR_HEALTH_READINESS_FAIL_THRESHOLD", "3")

        async def _probe(_client, _url):
            return False

        with (
            patch.object(srv_http, "_probe_dependency", _probe),
            patch.object(
                srv_http, "_readiness_fail_threshold", wraps=srv_http._readiness_fail_threshold
            ) as spy_threshold,
        ):
            # 3 misses → threshold=3 → degraded. Expect EXACTLY one threshold
            # lookup per failing probe cycle (one lookup, not three).
            codes = [
                asyncio.run(srv_http.health_check(_make_request())).status_code for _ in range(3)
            ]
        assert codes == [200, 200, 503], codes
        assert spy_threshold.call_count == 3, (
            f"one threshold lookup per failing probe is the contract (PR #65 "
            f"review finding #9); got {spy_threshold.call_count} for 3 misses"
        )
