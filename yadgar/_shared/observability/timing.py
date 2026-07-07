"""Timing decorators for Yadgar stage-level Prometheus histograms.

Backward-compatible: if prometheus_client is not installed, all decorators
return the underlying function unchanged (I3 — opt-in features must be no-op
when the dependency is absent).

Usage:
    from yadgar._shared.observability.timing import stage_timer, request_timer

    @stage_timer("encode")          # -> yadgar_drain_stage_ms{stage="encode"}
    def encode_document(...):
        ...

    @request_timer("recall")        # -> yadgar_recall_duration_ms
    def recall(...):
        ...

Internal test helper:
    from yadgar._shared.observability.timing import _make_stage_timer
    reg = CollectorRegistry()
    @_make_stage_timer("encode", registry=reg)
    def my_fn(): ...
"""

from __future__ import annotations

import functools
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from prometheus_client import CollectorRegistry

# Default ms buckets per spec
_MS_BUCKETS = (1, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000)

try:
    from prometheus_client import CollectorRegistry, Histogram

    _PROMETHEUS_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PROMETHEUS_AVAILABLE = False


def _make_stage_timer(
    stage: str,
    registry: CollectorRegistry | None = None,
) -> Callable:
    """Return a decorator that wraps a function and observes drain_stage_ms{stage=<stage>}.

    When registry is None, uses the shared _registry from yadgar.metrics (which
    already contains the yadgar_drain_stage_ms histogram registered at import time).

    When registry is provided (tests), registers a fresh histogram into that registry.
    """
    if not _PROMETHEUS_AVAILABLE:  # pragma: no cover
        return lambda fn: fn

    if registry is None:
        # Use the pre-registered histogram from metrics module
        from yadgar._shared.metrics import yadgar_drain_stage_ms as _hist  # noqa: PLC0415
    else:
        # Fresh registry for test isolation — register our own histogram
        _hist = Histogram(
            "yadgar_drain_stage_ms",
            "Drain stage duration in milliseconds",
            ["stage"],
            buckets=_MS_BUCKETS,
            registry=registry,
        )

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            t0 = time.monotonic()
            try:
                return fn(*args, **kwargs)
            finally:
                _hist.labels(stage=stage).observe((time.monotonic() - t0) * 1000)

        return wrapper

    return decorator


def stage_timer(stage: str) -> Callable:
    """Decorator: observe yadgar_drain_stage_ms{stage=<stage>} in ms.

    Uses the shared yadgar metrics registry. No-op if prometheus_client missing.
    """
    if not _PROMETHEUS_AVAILABLE:
        return lambda fn: fn
    return _make_stage_timer(stage)


def request_timer(metric_name: str) -> Callable:
    """Decorator: observe a named histogram (yadgar_<metric_name>_duration_ms).

    Looks up the pre-registered histogram from yadgar.metrics.
    No-op if prometheus_client missing.
    """
    if not _PROMETHEUS_AVAILABLE:
        return lambda fn: fn

    full_name = f"yadgar_{metric_name}_duration_ms"

    def decorator(fn: Callable) -> Callable:
        # Lazy-bind histogram at first call to avoid circular imports at decoration time
        _hist = None

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            nonlocal _hist
            if _hist is None:
                try:
                    import yadgar._shared.metrics as _m  # noqa: PLC0415

                    _hist = getattr(_m, full_name, None)
                except Exception:
                    pass
            t0 = time.monotonic()
            try:
                return fn(*args, **kwargs)
            finally:
                if _hist is not None:
                    try:
                        _hist.observe((time.monotonic() - t0) * 1000)
                    except Exception:
                        pass

        return wrapper

    return decorator


def labeled_timer(metric_attr: str, labels: dict) -> Callable:
    """Decorator: observe a labeled histogram from yadgar.metrics.

    metric_attr: attribute name on yadgar.metrics (e.g. 'yadgar_recall_stage_ms').
    labels: dict of label_name -> label_value to pass to .labels(...).observe().

    No-op if prometheus_client missing.
    """
    if not _PROMETHEUS_AVAILABLE:
        return lambda fn: fn

    def decorator(fn: Callable) -> Callable:
        _hist = None

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            nonlocal _hist
            if _hist is None:
                try:
                    import yadgar._shared.metrics as _m  # noqa: PLC0415

                    _hist = getattr(_m, metric_attr, None)
                except Exception:
                    pass
            t0 = time.monotonic()
            try:
                return fn(*args, **kwargs)
            finally:
                if _hist is not None:
                    try:
                        _hist.labels(**labels).observe((time.monotonic() - t0) * 1000)
                    except Exception:
                        pass

        return wrapper

    return decorator
