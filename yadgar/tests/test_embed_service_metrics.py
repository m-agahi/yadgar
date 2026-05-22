"""V1a — /metrics endpoint tests for embed_service (TDD, written before implementation).

Verifies:
- GET /metrics returns 200 + text/plain content-type (unauthenticated)
- Output is parseable by prometheus_client.parser
- All declared metric families appear in output
- yadgar_embed_rerank_requests_total{mode="ce"} increments on rerank call
- yadgar_embed_rerank_503_total{mode="ce"} increments on semaphore-timeout 503
- yadgar_embed_rerank_duration_seconds{mode="ce"} records observation on success
- /metrics requires NO auth (Prometheus scrapers can't easily carry bearer tokens;
  matches core /metrics pattern in yadgar/server/http.py §15)

Counter tests use before/after delta assertions — not absolute values —
so tests are independent of module-level global state and run order.
"""

from __future__ import annotations

import importlib
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Note: prometheus_client parser strips _total suffix from counter family names
# when TYPE is declared as counter (OpenMetrics-aligned behaviour since v0.20).
# The actual family names returned by text_string_to_metric_families are without _total.
EXPECTED_METRIC_FAMILIES = {
    "yadgar_embed_rerank_requests",  # counter (bare name, _total stripped by parser)
    "yadgar_embed_rerank_503",  # counter (bare name, _total stripped by parser)
    "yadgar_embed_rerank_duration_seconds",
    "yadgar_embed_rerank_semaphore_held",
    "yadgar_embed_model_loaded",
}


def _make_client(monkeypatch, max_concurrency: int = 1, acquire_timeout: float = 2.0):
    """Return a fresh FastAPI TestClient with embed_service patched to avoid model load."""
    import yadgar.config as cfg

    monkeypatch.setenv("YADGAR_RERANK_MAX_CONCURRENCY", str(max_concurrency))
    monkeypatch.setenv("YADGAR_RERANK_SEMAPHORE_ACQUIRE_TIMEOUT_SEC", str(acquire_timeout))
    monkeypatch.setenv("YADGAR_ALLOW_ROOT", "1")
    cfg.get_settings.cache_clear()

    # Reload so module-level semaphores + metric registry pick up new env
    import yadgar.embed_service as es

    importlib.reload(es)

    from fastapi.testclient import TestClient

    return TestClient(es.app, raise_server_exceptions=False)


def _parse_metric_families(body: str) -> dict[str, object]:
    """Parse Prometheus text output; return {metric_name: MetricFamily}."""
    from prometheus_client.parser import text_string_to_metric_families

    return {mf.name: mf for mf in text_string_to_metric_families(body)}


def _sample_value(families: dict, name: str, labels: dict[str, str]) -> float | None:
    """Return sample value for given metric name + label set, or None if absent.

    prometheus_client parser strips _total suffix from counter family names
    (OpenMetrics-aligned, since v0.20).  Accept both bare name and _total name:
    look up family by bare name; match samples by their full name (may have _total).
    """
    # Try exact name first, then strip _total suffix for counter family lookup
    mf = families.get(name) or families.get(name.removesuffix("_total"))
    if mf is None:
        return None
    for sample in mf.samples:
        if sample.labels == labels:
            return sample.value
    return None


def _counter_delta(families_before: dict, families_after: dict, name: str, labels: dict) -> float:
    before = _sample_value(families_before, name, labels) or 0.0
    after = _sample_value(families_after, name, labels) or 0.0
    return after - before


# ---------------------------------------------------------------------------
# 1. Endpoint basics
# ---------------------------------------------------------------------------


class TestMetricsEndpointBasics:
    def test_metrics_returns_200(self, monkeypatch):
        """GET /metrics returns HTTP 200."""
        client = _make_client(monkeypatch)
        resp = client.get("/metrics")
        assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"

    def test_metrics_content_type_is_text_plain(self, monkeypatch):
        """Content-Type header includes text/plain."""
        client = _make_client(monkeypatch)
        resp = client.get("/metrics")
        assert "text/plain" in resp.headers.get("content-type", ""), (
            f"expected text/plain, got {resp.headers.get('content-type')}"
        )

    def test_metrics_no_auth_required(self, monkeypatch):
        """GET /metrics succeeds WITHOUT Authorization header."""
        client = _make_client(monkeypatch)
        # No Authorization header — must succeed
        resp = client.get("/metrics", headers={})
        assert resp.status_code == 200, f"/metrics must be unauthenticated; got {resp.status_code}"

    def test_metrics_parseable_by_prometheus_parser(self, monkeypatch):
        """Output is parseable by prometheus_client.parser (valid exposition format)."""
        client = _make_client(monkeypatch)
        resp = client.get("/metrics")
        assert resp.status_code == 200
        # Should not raise
        families = _parse_metric_families(resp.text)
        assert isinstance(families, dict)

    def test_metrics_prometheus_version_in_content_type(self, monkeypatch):
        """Content-Type includes a Prometheus version string."""
        client = _make_client(monkeypatch)
        resp = client.get("/metrics")
        ct = resp.headers.get("content-type", "")
        # prometheus_client sets version=0.0.4 or version=1.0.0 depending on release.
        # Just verify a version token is present — avoid coupling to specific version.
        assert "version=" in ct, f"expected version= in content-type, got {ct!r}"


# ---------------------------------------------------------------------------
# 2. All declared metric families appear in output
# ---------------------------------------------------------------------------


class TestMetricFamiliesPresent:
    def test_all_expected_families_present(self, monkeypatch):
        """All declared metric family names appear in /metrics output."""
        client = _make_client(monkeypatch)
        resp = client.get("/metrics")
        assert resp.status_code == 200
        families = _parse_metric_families(resp.text)
        missing = EXPECTED_METRIC_FAMILIES - set(families.keys())
        assert not missing, f"Missing metric families: {missing}"


# ---------------------------------------------------------------------------
# 3. Counter increments on successful rerank
# ---------------------------------------------------------------------------


class TestRequestsCounterIncrements:
    def test_requests_counter_increments_on_success(self, monkeypatch):
        """yadgar_embed_rerank_requests_total{mode='ce'} increments on rerank call."""
        import yadgar.embed_service as es

        client = _make_client(monkeypatch)

        mock_ml = MagicMock()
        mock_ml.score_cross_encoder.return_value = [0.9]

        with patch.object(es, "_get_reranker", return_value=mock_ml):
            resp_before = client.get("/metrics")
            assert resp_before.status_code == 200
            families_before = _parse_metric_families(resp_before.text)

            client.post(
                "/rerank",
                json={"query": "test", "texts": ["doc"], "mode": "ce"},
            )

            resp_after = client.get("/metrics")
            assert resp_after.status_code == 200
            families_after = _parse_metric_families(resp_after.text)

        delta = _counter_delta(
            families_before, families_after, "yadgar_embed_rerank_requests_total", {"mode": "ce"}
        )
        assert delta >= 1.0, f"expected requests_total to increment by >=1, delta={delta}"


# ---------------------------------------------------------------------------
# 4. 503 counter increments on semaphore-busy
# ---------------------------------------------------------------------------


class TestSemaphoreBusy503Counter:
    def test_503_counter_increments_on_semaphore_timeout(self, monkeypatch):
        """yadgar_embed_rerank_503_total{mode='ce'} increments on semaphore-busy 503."""
        import asyncio

        import yadgar.embed_service as es

        # Short timeout so semaphore-busy fast-fails
        client = _make_client(monkeypatch, max_concurrency=1, acquire_timeout=0.1)

        mock_ml = MagicMock()
        mock_ml.score_cross_encoder.return_value = [0.9]

        loop = asyncio.new_event_loop()
        try:
            # Hold the ce semaphore to force 503
            loop.run_until_complete(es._rerank_semaphores["ce"].acquire())

            with patch.object(es, "_get_reranker", return_value=mock_ml):
                resp_before = client.get("/metrics")
                assert resp_before.status_code == 200
                families_before = _parse_metric_families(resp_before.text)

                resp_rerank = client.post(
                    "/rerank",
                    json={"query": "q", "texts": ["t"], "mode": "ce"},
                )
                assert resp_rerank.status_code == 503, (
                    f"expected 503 with semaphore held, got {resp_rerank.status_code}"
                )

                resp_after = client.get("/metrics")
                assert resp_after.status_code == 200
                families_after = _parse_metric_families(resp_after.text)
        finally:
            loop.close()

        delta = _counter_delta(
            families_before, families_after, "yadgar_embed_rerank_503_total", {"mode": "ce"}
        )
        assert delta >= 1.0, (
            f"expected 503_total to increment by >=1 on semaphore-busy, delta={delta}"
        )


# ---------------------------------------------------------------------------
# 5. Duration histogram records observation on success
# ---------------------------------------------------------------------------


class TestDurationHistogramObservation:
    def test_duration_histogram_records_on_success(self, monkeypatch):
        """yadgar_embed_rerank_duration_seconds{mode='ce'} records observation on success."""
        import yadgar.embed_service as es

        client = _make_client(monkeypatch)

        mock_ml = MagicMock()
        mock_ml.score_cross_encoder.return_value = [0.7]

        with patch.object(es, "_get_reranker", return_value=mock_ml):
            resp_before = client.get("/metrics")
            assert resp_before.status_code == 200
            families_before = _parse_metric_families(resp_before.text)

            client.post(
                "/rerank",
                json={"query": "test", "texts": ["doc1", "doc2"], "mode": "ce"},
            )

            resp_after = client.get("/metrics")
            assert resp_after.status_code == 200
            families_after = _parse_metric_families(resp_after.text)

        # _count sample increments for each observation
        count_before = (
            _sample_value(
                families_before,
                "yadgar_embed_rerank_duration_seconds",
                {"mode": "ce", "le": "+Inf"},
            )
            or 0.0
        )
        count_after = (
            _sample_value(
                families_after, "yadgar_embed_rerank_duration_seconds", {"mode": "ce", "le": "+Inf"}
            )
            or 0.0
        )

        assert count_after > count_before, (
            f"expected duration histogram to record observation; "
            f"count before={count_before}, after={count_after}"
        )
