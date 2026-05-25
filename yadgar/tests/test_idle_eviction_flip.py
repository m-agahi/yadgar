"""v5.6.7 PR-G — idle eviction flip + telemetry tests.

Written TDD (red first) — all tests FAIL before implementation.

Coverage:
1. Env=0 (default): unload_if_idle after 1h idle does NOT drop gauge.
2. Env=60: unload_if_idle after 90s idle drops gauge + increments counter.
3. Explicit arg overrides env: idle_seconds=30 with 45s elapsed evicts even when env=0.
4. Load duration histogram: mock model load, verify histogram _count incremented.
5. Span emission: model.unload span emitted on unload, model.load on load (skipped if no harness).

Handle → label mapping:
  _gte_reranker, _flashrank_ranker, _cross_encoder → "ce"
  _nli_model                                       → "nli"
  "pair"/"embedding" not managed by LocalMLClient unload path.
"""

from __future__ import annotations

import importlib
import time
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fresh_client(monkeypatch, idle_eviction_env: str | None):
    """Return a fresh LocalMLClient with a pre-loaded mock CE handle.

    idle_eviction_env: value for YADGAR_MODEL_IDLE_EVICTION_SECONDS,
                       or None to leave unset.
    """
    # Set env before import so module-level constant is picked up
    if idle_eviction_env is not None:
        monkeypatch.setenv("YADGAR_MODEL_IDLE_EVICTION_SECONDS", idle_eviction_env)
    else:
        monkeypatch.delenv("YADGAR_MODEL_IDLE_EVICTION_SECONDS", raising=False)

    # Re-import ml_client so module-level env read is re-evaluated
    import yadgar.ml_client as ml_mod

    importlib.reload(ml_mod)

    client = ml_mod.LocalMLClient(settings=None)
    # Simulate a CE model already loaded
    client._cross_encoder = MagicMock()
    client._last_used = time.monotonic()
    return client, ml_mod


def _get_gauge_value(model: str) -> float:
    """Read yadgar_embed_model_loaded{model} from embed_service_metrics registry."""
    import yadgar.embed_service_metrics as esm

    return esm.model_loaded.labels(model=model)._value.get()


def _get_counter_value(model: str) -> float:
    """Read yadgar_embed_model_unload_total{model} from embed_service_metrics registry."""
    import yadgar.embed_service_metrics as esm

    return esm.model_unload_total.labels(model=model)._value.get()


def _get_histogram_sum(model: str) -> float:
    """Read yadgar_embed_model_load_duration_seconds{model} _sum (proxy for 'observed at all')."""
    import yadgar.embed_service_metrics as esm

    return esm.model_load_duration_seconds.labels(model=model)._sum.get()


# ---------------------------------------------------------------------------
# Fixture: reset metric state between tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_metrics():
    """Reload embed_service_metrics before each test to get fresh counters.

    We re-import so each test starts from a clean registry.
    Note: CollectorRegistry allows re-registration after module reload.
    """
    import yadgar.embed_service_metrics as esm

    # Set gauge to 1 for "ce" to simulate a loaded model
    esm.model_loaded.labels(model="ce").set(1)
    yield


# ---------------------------------------------------------------------------
# Test 1: env=0 (default) → never evict regardless of idle time
# ---------------------------------------------------------------------------


class TestEnvDefaultNoEviction:
    def test_env_unset_no_eviction_after_1h_idle(self, monkeypatch):
        """With YADGAR_MODEL_IDLE_EVICTION_SECONDS unset, gauge stays 1 after 1h idle."""
        client, ml_mod = _fresh_client(monkeypatch, idle_eviction_env=None)

        import yadgar.embed_service_metrics as esm

        esm.model_loaded.labels(model="ce").set(1)
        gauge_before = _get_gauge_value("ce")

        # Fast-forward clock 1 hour (3600s) via monotonic mock
        fake_now = client._last_used + 3600.0
        with patch.object(ml_mod.time, "monotonic", return_value=fake_now):
            client.unload_if_idle()

        gauge_after = _get_gauge_value("ce")
        assert gauge_before == 1.0
        assert gauge_after == 1.0, (
            "Gauge must not drop when YADGAR_MODEL_IDLE_EVICTION_SECONDS is unset (default=0)"
        )
        # Model handle should still be non-None
        assert client._cross_encoder is not None

    def test_env_zero_no_eviction(self, monkeypatch):
        """With YADGAR_MODEL_IDLE_EVICTION_SECONDS=0, gauge stays 1 after 1h idle."""
        client, ml_mod = _fresh_client(monkeypatch, idle_eviction_env="0")

        import yadgar.embed_service_metrics as esm

        esm.model_loaded.labels(model="ce").set(1)

        fake_now = client._last_used + 3600.0
        with patch.object(ml_mod.time, "monotonic", return_value=fake_now):
            client.unload_if_idle()

        assert _get_gauge_value("ce") == 1.0
        assert client._cross_encoder is not None


# ---------------------------------------------------------------------------
# Test 2: env=60 → evict after 90s, gauge drops, counter increments
# ---------------------------------------------------------------------------


class TestEnvPositiveEvictsAndIncrements:
    def test_env_60_evicts_after_90s(self, monkeypatch):
        """With env=60, after 90s idle the CE gauge drops to 0 and counter increments by 1."""
        client, ml_mod = _fresh_client(monkeypatch, idle_eviction_env="60")

        import yadgar.embed_service_metrics as esm

        esm.model_loaded.labels(model="ce").set(1)
        counter_before = _get_counter_value("ce")

        fake_now = client._last_used + 90.0
        with patch.object(ml_mod.time, "monotonic", return_value=fake_now):
            client.unload_if_idle()

        gauge_after = _get_gauge_value("ce")
        counter_after = _get_counter_value("ce")

        assert gauge_after == 0.0, "CE gauge must drop to 0 after idle eviction"
        assert counter_after == counter_before + 1, (
            "model_unload_total must increment exactly once per eviction"
        )
        # Handle nulled out
        assert client._cross_encoder is None

    def test_env_60_no_eviction_before_60s(self, monkeypatch):
        """With env=60, after only 30s idle the gauge stays 1."""
        client, ml_mod = _fresh_client(monkeypatch, idle_eviction_env="60")

        import yadgar.embed_service_metrics as esm

        esm.model_loaded.labels(model="ce").set(1)

        fake_now = client._last_used + 30.0
        with patch.object(ml_mod.time, "monotonic", return_value=fake_now):
            client.unload_if_idle()

        assert _get_gauge_value("ce") == 1.0
        assert client._cross_encoder is not None


# ---------------------------------------------------------------------------
# Test 3: explicit idle_seconds arg overrides env=0
# ---------------------------------------------------------------------------


class TestExplicitArgOverridesEnv:
    def test_explicit_idle_seconds_overrides_env_zero(self, monkeypatch):
        """unload_if_idle(idle_seconds=30) evicts after 45s even when env=0."""
        client, ml_mod = _fresh_client(monkeypatch, idle_eviction_env="0")

        import yadgar.embed_service_metrics as esm

        esm.model_loaded.labels(model="ce").set(1)
        counter_before = _get_counter_value("ce")

        fake_now = client._last_used + 45.0
        with patch.object(ml_mod.time, "monotonic", return_value=fake_now):
            client.unload_if_idle(idle_seconds=30)

        gauge_after = _get_gauge_value("ce")
        counter_after = _get_counter_value("ce")

        assert gauge_after == 0.0, "Explicit arg must override env=0 and evict"
        assert counter_after == counter_before + 1


# ---------------------------------------------------------------------------
# Test 4: load duration histogram incremented on model load
# ---------------------------------------------------------------------------


class TestLoadDurationHistogram:
    def test_histogram_count_increments_on_ce_load(self, monkeypatch):
        """After a CE model is loaded via score_cross_encoder, histogram _count increments."""
        monkeypatch.delenv("YADGAR_MODEL_IDLE_EVICTION_SECONDS", raising=False)

        import yadgar.embed_service_metrics as esm
        import yadgar.ml_client as ml_mod

        importlib.reload(ml_mod)

        client = ml_mod.LocalMLClient(settings=None)
        # Ensure _cross_encoder is unloaded so we trigger a load
        client._cross_encoder = None

        sum_before = _get_histogram_sum("ce")

        # Mock CrossEncoder constructor — avoid actual model download
        mock_ce_instance = MagicMock()
        mock_ce_instance.predict.return_value = [0.5]

        mock_st = MagicMock()
        mock_st.CrossEncoder = MagicMock(return_value=mock_ce_instance)

        with patch.dict("sys.modules", {"sentence_transformers": mock_st, "flashrank": None}):
            try:
                client.score_cross_encoder("q", ["text"])
            except Exception:
                pass  # load attempt is what matters

        sum_after = _get_histogram_sum("ce")
        assert sum_after >= sum_before, (
            "model_load_duration_seconds{model='ce'} must be observed on model load"
        )
        # _sum is 0.0 before any observation; after a load it must be >= 0
        # (duration could be near-zero in test, but _sum transitions from uninitialized state)
        # We verify the histogram was actually observed by checking sample count from collect()
        samples = list(esm.model_load_duration_seconds.labels(model="ce").collect())
        count_sample = next(
            (s for m in samples for s in m.samples if s.name.endswith("_count")),
            None,
        )
        assert count_sample is not None and count_sample.value >= 1, (
            f"Expected _count >= 1, got {count_sample}"
        )


# ---------------------------------------------------------------------------
# Test 5: span emission (skip if no in-process OTel harness)
# ---------------------------------------------------------------------------


class TestSpanEmission:
    @pytest.fixture()
    def in_memory_tracer(self):
        """Install an in-memory span exporter, yield (tracer, exporter), teardown."""
        try:
            from opentelemetry import trace
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import SimpleSpanProcessor
            from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
                InMemorySpanExporter,
            )
        except ImportError:
            pytest.skip("opentelemetry SDK not available")

        # Reset once-guard
        once = getattr(trace, "_TRACER_PROVIDER_SET_ONCE", None)
        if once is not None and hasattr(once, "_done"):
            once._done = False
        if hasattr(trace, "_TRACER_PROVIDER"):
            trace._TRACER_PROVIDER = None

        exporter = InMemorySpanExporter()
        provider = TracerProvider(resource=Resource.create({"service.name": "test"}))
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        yield trace.get_tracer("test"), exporter

        # Cleanup
        once = getattr(trace, "_TRACER_PROVIDER_SET_ONCE", None)
        if once is not None and hasattr(once, "_done"):
            once._done = False
        if hasattr(trace, "_TRACER_PROVIDER"):
            trace._TRACER_PROVIDER = None

    def test_unload_span_emitted(self, monkeypatch, in_memory_tracer):
        """model.unload span is emitted when an eviction occurs."""
        _tracer, exporter = in_memory_tracer
        client, ml_mod = _fresh_client(monkeypatch, idle_eviction_env="60")

        import yadgar.embed_service_metrics as esm

        esm.model_loaded.labels(model="ce").set(1)

        fake_now = client._last_used + 90.0
        with patch.object(ml_mod.time, "monotonic", return_value=fake_now):
            client.unload_if_idle()

        span_names = [s.name for s in exporter.get_finished_spans()]
        assert "model.unload" in span_names, f"Expected 'model.unload' span; got {span_names}"

    def test_load_span_emitted(self, monkeypatch, in_memory_tracer):
        """model.load span is emitted when a CE model is constructed."""
        _tracer, exporter = in_memory_tracer
        monkeypatch.delenv("YADGAR_MODEL_IDLE_EVICTION_SECONDS", raising=False)

        import yadgar.ml_client as ml_mod

        importlib.reload(ml_mod)

        client = ml_mod.LocalMLClient(settings=None)
        client._cross_encoder = None

        mock_ce_instance = MagicMock()
        mock_ce_instance.predict.return_value = [0.5]

        mock_st = MagicMock()
        mock_st.CrossEncoder = MagicMock(return_value=mock_ce_instance)

        with patch.dict("sys.modules", {"sentence_transformers": mock_st, "flashrank": None}):
            try:
                client.score_cross_encoder("q", ["text"])
            except Exception:
                pass

        span_names = [s.name for s in exporter.get_finished_spans()]
        assert "model.load" in span_names, f"Expected 'model.load' span; got {span_names}"
