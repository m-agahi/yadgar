"""Tests for yadgar/observability/timing.py — v5.49.9 wave 4 coverage.

Module: yadgar.observability.timing
Target: ≥80% line coverage

Strategy:
- Use _make_stage_timer(stage, registry=reg) with fresh CollectorRegistry per test
  to avoid "Duplicated timeseries" errors.
- stage_timer() delegates to _make_stage_timer; test via the public API.
- request_timer() and labeled_timer() lazy-bind metrics at first call;
  test both the "histogram found" and "histogram missing" (swallowed) paths.
- _PROMETHEUS_AVAILABLE=False branches are marked # pragma: no cover in source;
  excluded from coverage requirement.

Floor: lines 43-44 (ImportError branch: _PROMETHEUS_AVAILABLE=False) and
       lines 58-59 (registry=None without available prometheus inside _make_stage_timer)
       are excluded via # pragma: no cover in source — untestable without
       uninstalling prometheus_client.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fresh_registry():
    from prometheus_client import CollectorRegistry

    return CollectorRegistry()


# ---------------------------------------------------------------------------
# _make_stage_timer
# ---------------------------------------------------------------------------


class TestMakeStageTimer:
    def test_decorator_wraps_function(self):
        """_make_stage_timer returns a decorator; wrapped fn preserves __name__."""
        from yadgar.observability.timing import _make_stage_timer

        reg = _fresh_registry()

        @_make_stage_timer("encode", registry=reg)
        def my_func():
            return "result"

        assert my_func.__name__ == "my_func"

    def test_wrapped_function_returns_value(self):
        """Wrapped function passes return value through."""
        from yadgar.observability.timing import _make_stage_timer

        reg = _fresh_registry()

        @_make_stage_timer("encode", registry=reg)
        def compute():
            return 42

        assert compute() == 42

    def test_histogram_observed_on_call(self):
        """After calling wrapped function, histogram has count=1."""
        from prometheus_client import generate_latest

        from yadgar.observability.timing import _make_stage_timer

        reg = _fresh_registry()

        @_make_stage_timer("encode", registry=reg)
        def do_work():
            return "ok"

        do_work()

        output = generate_latest(reg).decode()
        assert "yadgar_drain_stage_ms" in output
        assert "_count" in output

    def test_histogram_observed_with_correct_stage_label(self):
        """Histogram sample carries the stage label passed to _make_stage_timer."""
        from prometheus_client import generate_latest

        from yadgar.observability.timing import _make_stage_timer

        reg = _fresh_registry()

        @_make_stage_timer("my_stage", registry=reg)
        def fn():
            pass

        fn()

        output = generate_latest(reg).decode()
        assert 'stage="my_stage"' in output

    def test_histogram_observed_multiple_calls(self):
        """Multiple calls accumulate count in histogram."""
        from prometheus_client import generate_latest

        from yadgar.observability.timing import _make_stage_timer

        reg = _fresh_registry()

        @_make_stage_timer("batch", registry=reg)
        def step():
            pass

        step()
        step()
        step()

        output = generate_latest(reg).decode()
        # count = 3.0
        assert "_count" in output and "3.0" in output

    def test_exception_still_records_histogram(self):
        """Even if wrapped fn raises, histogram is still observed (finally block)."""
        from prometheus_client import generate_latest

        from yadgar.observability.timing import _make_stage_timer

        reg = _fresh_registry()

        @_make_stage_timer("error_stage", registry=reg)
        def explode():
            raise ValueError("boom")

        with pytest.raises(ValueError):
            explode()

        output = generate_latest(reg).decode()
        assert "yadgar_drain_stage_ms" in output
        assert "_count" in output

    def test_wraps_preserves_docstring(self):
        """functools.wraps preserves __doc__."""
        from yadgar.observability.timing import _make_stage_timer

        reg = _fresh_registry()

        @_make_stage_timer("doc_stage", registry=reg)
        def documented():
            """This is my docstring."""

        assert documented.__doc__ == "This is my docstring."

    def test_kwargs_passed_through(self):
        """Wrapped function receives positional and keyword arguments correctly."""
        from yadgar.observability.timing import _make_stage_timer

        reg = _fresh_registry()
        received = {}

        @_make_stage_timer("kwarg_stage", registry=reg)
        def capture(a, b=None):
            received["a"] = a
            received["b"] = b

        capture(1, b=2)
        assert received == {"a": 1, "b": 2}


# ---------------------------------------------------------------------------
# stage_timer (public API, delegates to _make_stage_timer with shared registry)
# ---------------------------------------------------------------------------


class TestStageTimer:
    def test_stage_timer_returns_callable(self):
        """stage_timer(stage) returns a decorator (callable)."""
        from yadgar.observability.timing import stage_timer

        decorator = stage_timer("encode")
        assert callable(decorator)

    def test_stage_timer_decorates_function(self):
        """@stage_timer wraps the function; original return value preserved."""
        from yadgar.observability.timing import stage_timer

        @stage_timer("encode")
        def fn():
            return "done"

        assert fn() == "done"

    def test_stage_timer_wraps_name(self):
        """@stage_timer preserves __name__ via functools.wraps."""
        from yadgar.observability.timing import stage_timer

        @stage_timer("named_stage")
        def my_named_fn():
            pass

        assert my_named_fn.__name__ == "my_named_fn"


# ---------------------------------------------------------------------------
# request_timer
# ---------------------------------------------------------------------------


class TestRequestTimer:
    def test_request_timer_returns_callable(self):
        """request_timer(name) returns a decorator."""
        from yadgar.observability.timing import request_timer

        dec = request_timer("recall")
        assert callable(dec)

    def test_request_timer_wraps_function(self):
        """@request_timer decorated fn preserves __name__."""
        from yadgar.observability.timing import request_timer

        @request_timer("recall")
        def recall_fn():
            return "data"

        assert recall_fn.__name__ == "recall_fn"

    def test_request_timer_returns_value(self):
        """Wrapped fn return value passes through."""
        from yadgar.observability.timing import request_timer

        @request_timer("recall")
        def fn():
            return 99

        assert fn() == 99

    def test_request_timer_histogram_found_path(self):
        """request_timer: when real metrics attr exists, function executes and returns value."""
        from yadgar.observability.timing import request_timer

        # yadgar.metrics.yadgar_recall_duration_ms is a real Histogram; use it directly.
        # Verify: no exception, return value passes through, lazy bind doesn't crash.
        @request_timer("recall")
        def fn():
            return "ok"

        result = fn()
        assert result == "ok"

    def test_request_timer_histogram_missing_attr(self):
        """When yadgar.metrics lacks the attr, observe is skipped gracefully."""
        from yadgar.observability.timing import request_timer

        # Use a metric name that definitely doesn't exist in yadgar.metrics
        @request_timer("__nonexistent_metric_xyz__")
        def fn():
            return "ok"

        result = fn()
        assert result == "ok"

    def test_request_timer_observe_exception_swallowed(self):
        """If observe raises, exception is swallowed and fn return value returned.

        Patch yadgar.metrics attribute directly on the module object to avoid
        the sys.modules / package-attribute mismatch issue.
        """
        import yadgar.metrics as _real_metrics
        from yadgar.observability.timing import request_timer

        mock_hist = MagicMock()
        mock_hist.observe.side_effect = RuntimeError("metric error")

        original = getattr(_real_metrics, "yadgar_recall_duration_ms", None)
        try:
            _real_metrics.yadgar_recall_duration_ms = mock_hist

            @request_timer("recall")
            def fn():
                return "safe"

            result = fn()
        finally:
            if original is not None:
                _real_metrics.yadgar_recall_duration_ms = original
            else:
                delattr(_real_metrics, "yadgar_recall_duration_ms")

        assert result == "safe"

    def test_request_timer_import_exception_swallowed(self):
        """If yadgar.metrics import fails inside wrapper, exception swallowed; value returned."""
        # Remove yadgar.metrics from sys.modules so import inside wrapper re-imports
        # Replace with a broken module that raises on attribute access
        from yadgar.observability.timing import request_timer

        class _BrokenMetrics:
            def __getattr__(self, name):
                raise RuntimeError("broken import")

        import sys as _sys

        _sys.modules.get("yadgar.metrics")
        try:
            # Temporarily replace the yadgar.metrics attribute on the yadgar package
            # (not just sys.modules) so the import inside the wrapper gets the broken one
            import yadgar as _yadgar_pkg

            orig_attr = getattr(_yadgar_pkg, "metrics", "ABSENT")
            _yadgar_pkg.metrics = _BrokenMetrics()

            @request_timer("recall")
            def fn():
                return "exc_safe"

            result = fn()
        finally:
            if orig_attr == "ABSENT":
                delattr(_yadgar_pkg, "metrics")
            else:
                _yadgar_pkg.metrics = orig_attr

        assert result == "exc_safe"

    def test_request_timer_lazy_rebind_after_reset(self):
        """_hist binds on first call; subsequent calls reuse it.

        Patch attribute directly on yadgar.metrics module to bypass
        the sys.modules / package-attribute mismatch.
        """
        import yadgar.metrics as _real_metrics
        from yadgar.observability.timing import request_timer

        mock_hist = MagicMock()

        original = getattr(_real_metrics, "yadgar_fetch_duration_ms", "ABSENT")
        try:
            _real_metrics.yadgar_fetch_duration_ms = mock_hist

            @request_timer("fetch")
            def fn():
                return 1

            fn()
            fn()
        finally:
            if original == "ABSENT":
                if hasattr(_real_metrics, "yadgar_fetch_duration_ms"):
                    delattr(_real_metrics, "yadgar_fetch_duration_ms")
            else:
                _real_metrics.yadgar_fetch_duration_ms = original

        assert mock_hist.observe.call_count == 2


# ---------------------------------------------------------------------------
# labeled_timer
# ---------------------------------------------------------------------------


class TestLabeledTimer:
    def test_labeled_timer_returns_callable(self):
        """labeled_timer returns a decorator."""
        from yadgar.observability.timing import labeled_timer

        dec = labeled_timer("yadgar_recall_stage_ms", {"stage": "nli"})
        assert callable(dec)

    def test_labeled_timer_wraps_function(self):
        """@labeled_timer preserves __name__."""
        from yadgar.observability.timing import labeled_timer

        @labeled_timer("yadgar_recall_stage_ms", {"stage": "nli"})
        def fn():
            return "x"

        assert fn.__name__ == "fn"

    def test_labeled_timer_returns_value(self):
        """Wrapped fn return value passes through."""
        from yadgar.observability.timing import labeled_timer

        @labeled_timer("yadgar_recall_stage_ms", {"stage": "nli"})
        def fn():
            return "result"

        assert fn() == "result"

    def test_labeled_timer_histogram_found_path(self):
        """When metric attr exists on yadgar.metrics, labels().observe() is called.

        Patches the attribute directly on the module object to avoid
        sys.modules / package-attribute mismatch.
        """
        import yadgar.metrics as _real_metrics
        from yadgar.observability.timing import labeled_timer

        mock_labels = MagicMock()
        mock_hist = MagicMock()
        mock_hist.labels.return_value = mock_labels

        original = getattr(_real_metrics, "yadgar_recall_stage_ms", "ABSENT")
        try:
            _real_metrics.yadgar_recall_stage_ms = mock_hist

            @labeled_timer("yadgar_recall_stage_ms", {"stage": "nli"})
            def fn():
                return "ok"

            result = fn()
        finally:
            if original == "ABSENT":
                if hasattr(_real_metrics, "yadgar_recall_stage_ms"):
                    delattr(_real_metrics, "yadgar_recall_stage_ms")
            else:
                _real_metrics.yadgar_recall_stage_ms = original

        assert result == "ok"
        mock_hist.labels.assert_called_once_with(stage="nli")
        mock_labels.observe.assert_called_once()

    def test_labeled_timer_histogram_missing_attr(self):
        """When metric attr is missing, no error."""
        from yadgar.observability.timing import labeled_timer

        # Use attr name that definitely doesn't exist in yadgar.metrics
        @labeled_timer("__nonexistent_labeled_metric_xyz__", {"stage": "nli"})
        def fn():
            return "ok"

        result = fn()
        assert result == "ok"

    def test_labeled_timer_import_exception_swallowed(self):
        """If yadgar.metrics getattr raises inside wrapper, exception swallowed; value returned."""
        import yadgar as _yadgar_pkg
        from yadgar.observability.timing import labeled_timer

        class _BrokenMetrics:
            def __getattr__(self, name):
                raise RuntimeError("broken import")

        orig_attr = getattr(_yadgar_pkg, "metrics", "ABSENT")
        try:
            _yadgar_pkg.metrics = _BrokenMetrics()

            @labeled_timer("yadgar_recall_stage_ms", {"stage": "nli"})
            def fn():
                return "exc_safe"

            result = fn()
        finally:
            if orig_attr == "ABSENT":
                delattr(_yadgar_pkg, "metrics")
            else:
                _yadgar_pkg.metrics = orig_attr

        assert result == "exc_safe"

    def test_labeled_timer_observe_exception_swallowed(self):
        """observe() exception is swallowed; fn return value returned."""
        import yadgar.metrics as _real_metrics
        from yadgar.observability.timing import labeled_timer

        mock_labels = MagicMock()
        mock_labels.observe.side_effect = RuntimeError("observe error")
        mock_hist = MagicMock()
        mock_hist.labels.return_value = mock_labels

        original = getattr(_real_metrics, "yadgar_recall_stage_ms", "ABSENT")
        try:
            _real_metrics.yadgar_recall_stage_ms = mock_hist

            @labeled_timer("yadgar_recall_stage_ms", {"stage": "nli"})
            def fn():
                return "safe"

            result = fn()
        finally:
            if original == "ABSENT":
                if hasattr(_real_metrics, "yadgar_recall_stage_ms"):
                    delattr(_real_metrics, "yadgar_recall_stage_ms")
            else:
                _real_metrics.yadgar_recall_stage_ms = original

        assert result == "safe"
