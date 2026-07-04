"""The `@observe(tier=...)` tri-signal decorator (I33, v5.101 P0).

One decorator composes the three existing signal paths — a span (delegated to
`yadgar.tracing.trace_span`), a bounded Prometheus metric, and an I14 JSON log —
and emits BY TIER:

    boundary : span + RED family (yadgar_observe_requests_total{name,outcome} +
               yadgar_observe_request_duration_seconds{name}) + INFO log on
               success / ERROR log on raise.
    stage    : span + ONE shared stage family
               (yadgar_observe_stage_duration_seconds{stage} +
               yadgar_observe_stage_errors_total{stage}); ERROR log on raise only.
    hot      : span only (attribute/count on the enclosing span). NO per-call
               metric, NO per-call log.
    exempt   : no-op passthrough (the categorized "documented reason not to").

Anti-cardinality (plan §3.3): boundaries SHARE the RED family keyed by a bounded
`name` label; stages SHARE one histogram family keyed by a bounded `stage` label.
There is NO per-function histogram object — that is the cardinality bomb the
standard rejects.

Double-instrumentation guard (plan §3.2): if the wrapped function already carries
`@trace_span`/`@_tool` (detected via a sentinel attribute those decorators set, or
via `@observe` stacking) OR runs under an already-active recording span, `@observe`
runs in metric+log-only mode and does NOT open a second span. A `@trace_span` +
`@observe` fn emits exactly ONE span.

Backward-compatible (I3): when prometheus_client / opentelemetry are absent, the
metric / span paths become no-ops; the function still runs and still logs.
"""

from __future__ import annotations

import functools
import inspect
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Callable

from yadgar.tracing import trace_span

logger = logging.getLogger("yadgar.observe")

# Sentinel attribute set on any function this module (or trace_span) has already
# given a span to. The coverage lint also treats trace_span/_tool statically.
_OBSERVE_SPAN_SENTINEL = "_yadgar_observe_has_span"

Tier = Literal["boundary", "stage", "hot"]

# ── Shared metric families (bounded — see plan §3.3) ─────────────────────────
# Rebindable by tests via monkeypatch (see tests/test_observe_decorator.py).
try:
    from prometheus_client import Counter as _Counter
    from prometheus_client import Histogram as _Histogram

    from yadgar.metrics import _registry as _yadgar_registry

    _PROM_AVAILABLE = True
except Exception:  # pragma: no cover
    _PROM_AVAILABLE = False


def _get_or_create():
    """Register the four shared observe metric families on the yadgar registry.

    Idempotent: on re-import (or duplicate registration) reuse the existing
    collectors rather than raising Duplicated timeseries.
    """
    if not _PROM_AVAILABLE:  # pragma: no cover
        return None, None, None, None

    def _mk(factory, name, doc, labels, **kw):
        try:
            return factory(name, doc, labels, registry=_yadgar_registry, **kw)
        except ValueError:
            # Already registered (re-import) — fetch the existing collector.
            return _yadgar_registry._names_to_collectors.get(name)

    req_total = _mk(
        _Counter,
        "yadgar_observe_requests_total",
        "RED requests counter for @observe boundary tier",
        ["name", "outcome"],
    )
    req_dur = _mk(
        _Histogram,
        "yadgar_observe_request_duration_seconds",
        "RED duration histogram for @observe boundary tier",
        ["name"],
    )
    stage_dur = _mk(
        _Histogram,
        "yadgar_observe_stage_duration_seconds",
        "Shared stage duration histogram for @observe stage tier",
        ["stage"],
    )
    stage_err = _mk(
        _Counter,
        "yadgar_observe_stage_errors_total",
        "Shared stage error counter for @observe stage tier",
        ["stage"],
    )
    return req_total, req_dur, stage_dur, stage_err


_REQUESTS_TOTAL, _REQUEST_DURATION, _STAGE_DURATION, _STAGE_ERRORS = _get_or_create()


# ── span presence detection (double-instrumentation guard) ───────────────────


def _already_span_sourced(fn: Callable) -> bool:
    """True if fn already has a span source (trace_span/_tool/observe sentinel).

    Detection is decoration-time (static sentinel) rather than runtime
    (`get_current_span()`): a fn carrying `@trace_span`/`@_tool` is span-sourced by
    construction, so `@observe` runs metric+log-only and never opens a second span.
    Spans nesting under an *auto-instrumented* parent (httpx/FastAPI) are correct
    nesting, not double-instrumentation — so no runtime suppression is needed.
    """
    return bool(getattr(fn, _OBSERVE_SPAN_SENTINEL, False))


# ── emission helpers (module-level; keep the decorator body thin per I13) ─────


@dataclass
class _Spec:
    """Resolved per-decoration config passed to the emit helpers."""

    tier: str
    metric_key: str
    event: str
    component: str


def _emit_success(spec: _Spec, elapsed_s: float) -> None:
    if spec.tier == "boundary":
        if _REQUESTS_TOTAL is not None:
            _REQUESTS_TOTAL.labels(name=spec.metric_key, outcome="ok").inc()
        if _REQUEST_DURATION is not None:
            _REQUEST_DURATION.labels(name=spec.metric_key).observe(elapsed_s)
        # An @observe'd fn can run at interpreter/atexit shutdown when the log
        # stream is already CLOSED → logging raises "I/O operation on closed
        # file". A signal firing during teardown must NEVER raise, so swallow it.
        try:
            logger.info(
                spec.event,
                extra={
                    "component": spec.component,
                    "action": spec.event,
                    "outcome": "ok",
                    "latency_ms": round(elapsed_s * 1000, 3),
                },
            )
        except (ValueError, OSError):  # fmt: skip
            pass
    elif spec.tier == "stage" and _STAGE_DURATION is not None:
        _STAGE_DURATION.labels(stage=spec.metric_key).observe(elapsed_s)
    # hot: nothing


def _emit_error(spec: _Spec, exc: BaseException, elapsed_s: float) -> None:
    if spec.tier == "boundary":
        if _REQUESTS_TOTAL is not None:
            _REQUESTS_TOTAL.labels(name=spec.metric_key, outcome="error").inc()
        if _REQUEST_DURATION is not None:
            _REQUEST_DURATION.labels(name=spec.metric_key).observe(elapsed_s)
    elif spec.tier == "stage" and _STAGE_ERRORS is not None:
        _STAGE_ERRORS.labels(stage=spec.metric_key).inc()
    if spec.tier in ("boundary", "stage"):
        # Same shutdown resilience as _emit_success: a closed log stream at
        # teardown must not raise (and must never mask the caller's exception,
        # which sync_wrapper/async_wrapper re-raise after this returns).
        try:
            logger.error(
                spec.event,
                extra={
                    "component": spec.component,
                    "action": spec.event,
                    "outcome": "error",
                    "latency_ms": round(elapsed_s * 1000, 3),
                    "error": type(exc).__name__,
                },
            )
        except (ValueError, OSError):  # fmt: skip
            pass


def _build_wrapper(fn: Callable, core: Callable, spec: _Spec) -> Callable:
    """Wrap `core` (fn possibly span-decorated) with the metric+log emitters."""
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args, **kwargs):
            t0 = time.monotonic()
            try:
                result = await core(*args, **kwargs)
            except BaseException as exc:  # noqa: BLE001 — re-raised below
                _emit_error(spec, exc, time.monotonic() - t0)
                raise
            _emit_success(spec, time.monotonic() - t0)
            return result

        return async_wrapper

    @functools.wraps(fn)
    def sync_wrapper(*args, **kwargs):
        t0 = time.monotonic()
        try:
            result = core(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 — re-raised below
            _emit_error(spec, exc, time.monotonic() - t0)
            raise
        _emit_success(spec, time.monotonic() - t0)
        return result

    return sync_wrapper


# ── the decorator ────────────────────────────────────────────────────────────


def observe(
    *,
    tier: Tier = "stage",
    name: str | None = None,
    metric: str | None = None,  # reserved; stage/boundary use shared families
    log_event: str | None = None,
    attributes: dict[str, Any] | None = None,
    exempt: str | None = None,
    span: bool = True,
) -> Callable:
    """Tri-signal observability decorator. See module docstring for tier semantics.

    span=False: emit metric + log (and set the lint sentinel) but do NOT open an
    @observe span. Use ONLY for a fn that already carries an EXPLICIT inner
    ``with span(NAME)`` grouping span (e.g. recall._apply_recall_side_effects,
    recall._fanout_recall) — the @observe span would otherwise nest BETWEEN the
    enclosing op and that inner grouping span, pushing the intended grouping span
    down a level and breaking its "direct child of the enclosing op" contract.
    The lint counts a non-exempt @observe as satisfied regardless of span=.
    """

    def decorator(fn: Callable) -> Callable:
        # Descriptor guard: `@observe` stacked ABOVE `@staticmethod`/`@classmethod`
        # receives the *descriptor object*, not the plain function. If we wrap that
        # descriptor and return a plain function, the method loses its descriptor
        # binding — `self`/`cls` gets injected as a positional arg on `self.m()`
        # ("takes 0 positional arguments but 1 was given"). Unwrap FIRST (so span
        # name / signature detection see the real function) and RE-WRAP the result
        # in the same descriptor LAST.
        descriptor_type: type | None = None
        if isinstance(fn, (staticmethod, classmethod)):
            descriptor_type = type(fn)
            fn = fn.__func__

        if exempt is not None:
            # Categorized no-op passthrough. Record the exemption for the lint.
            fn._yadgar_observe_exempt = exempt  # type: ignore[attr-defined]
            return descriptor_type(fn) if descriptor_type is not None else fn

        span_name = name if name is not None else f"{fn.__module__}.{fn.__qualname__}"
        spec = _Spec(
            tier=tier,
            metric_key=metric or span_name,
            event=log_event or span_name,
            component=fn.__module__,
        )

        # Open a span only if fn is not already span-sourced (@trace_span below us)
        # AND span= was not explicitly disabled. span=False keeps metric+log+sentinel
        # but leaves span-opening to the fn's own inner `with span(...)` grouping span.
        already_sourced = _already_span_sourced(fn)
        add_span = span and not already_sourced
        core = trace_span(span_name, attributes=attributes)(fn) if add_span else fn

        wrapper = _build_wrapper(fn, core, spec)

        # lru_cache guard: `@observe` stacked ABOVE `@functools.lru_cache` receives
        # the cache wrapper, whose public surface (cache_info / cache_clear /
        # cache_parameters) callers rely on. functools.wraps copies __wrapped__ but
        # NOT these bound methods, so `fn.cache_info()` on the observe wrapper raised
        # AttributeError — silently swallowed by callers (e.g. recall's branch
        # detection), collapsing the allowed-branch set. Re-expose them on the
        # wrapper; the bound methods point at the real lru object so caching (and its
        # hit/miss metrics) stay intact.
        for _cache_attr in ("cache_info", "cache_clear", "cache_parameters"):
            _cache_fn = getattr(fn, _cache_attr, None)
            if _cache_fn is not None:
                setattr(wrapper, _cache_attr, _cache_fn)

        # Mark that this fn now has a span source so a stacked @observe / the
        # coverage lint treats it as satisfied and never double-spans. span=False
        # still sets it: the fn carries its own inner grouping span, so it IS
        # span-sourced for the lint's purposes.
        wrapper._yadgar_observe_has_span = True  # type: ignore[attr-defined]

        # Re-apply the descriptor so staticmethod/classmethod binding is preserved.
        if descriptor_type is not None:
            return descriptor_type(wrapper)
        return wrapper

    return decorator
