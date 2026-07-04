"""I33 tri-signal @observe decorator — test suite (v5.101 P0).

The @observe decorator composes the three existing signal paths (span via
@trace_span, a bounded Prometheus metric, an I14 JSON log) and emits BY TIER:

    boundary : span + RED metric family + INFO/ERROR log
    stage    : span + ONE shared stage-labelled histogram family + ERROR-on-raise log
    hot      : span/attribute only — NO per-call metric, NO per-call log
    exempt   : no-op passthrough

Anti-cardinality: boundaries share the RED family (yadgar_observe_requests_total +
yadgar_observe_request_duration_seconds), stages share
yadgar_observe_stage_duration_seconds{stage} + yadgar_observe_stage_errors_total{stage}.
No per-function histogram objects.

Double-instrumentation guard: a fn already carrying @trace_span/@_tool must emit
exactly ONE span even if @observe is (redundantly) stacked — @observe runs in
metric+log-only mode.
"""

from __future__ import annotations

import logging

import pytest


@pytest.fixture()
def in_memory_tracer():
    """(tracer, exporter) with an InMemorySpanExporter; installs a clean test provider."""
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    once = getattr(trace, "_TRACER_PROVIDER_SET_ONCE", None)
    if once is not None and hasattr(once, "_done"):
        once._done = False
    if hasattr(trace, "_TRACER_PROVIDER"):
        trace._TRACER_PROVIDER = None

    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    tracer = trace.get_tracer("test")
    return tracer, exporter


@pytest.fixture()
def obs_registry(monkeypatch):
    """Fresh CollectorRegistry wired into the @observe metric families.

    Rebinds the module-level shared metric objects so each test observes into an
    isolated registry (avoids Duplicated timeseries across the suite).
    """
    from prometheus_client import CollectorRegistry, Counter, Histogram

    import yadgar.observability.observe as obs

    reg = CollectorRegistry()
    req_total = Counter(
        "yadgar_observe_requests_total",
        "RED requests counter (test registry)",
        ["name", "outcome"],
        registry=reg,
    )
    req_dur = Histogram(
        "yadgar_observe_request_duration_seconds",
        "RED duration histogram (test registry)",
        ["name"],
        registry=reg,
    )
    stage_dur = Histogram(
        "yadgar_observe_stage_duration_seconds",
        "Shared stage duration histogram (test registry)",
        ["stage"],
        registry=reg,
    )
    stage_err = Counter(
        "yadgar_observe_stage_errors_total",
        "Shared stage errors counter (test registry)",
        ["stage"],
        registry=reg,
    )
    monkeypatch.setattr(obs, "_REQUESTS_TOTAL", req_total, raising=False)
    monkeypatch.setattr(obs, "_REQUEST_DURATION", req_dur, raising=False)
    monkeypatch.setattr(obs, "_STAGE_DURATION", stage_dur, raising=False)
    monkeypatch.setattr(obs, "_STAGE_ERRORS", stage_err, raising=False)
    return reg


# ── boundary tier: span + RED metric + INFO log ──────────────────────────────


def test_boundary_emits_span(in_memory_tracer, obs_registry):
    _, exporter = in_memory_tracer
    from yadgar.observability.observe import observe

    @observe(tier="boundary", name="my.boundary")
    def handler():
        return "ok"

    assert handler() == "ok"
    spans = exporter.get_finished_spans()
    assert any(s.name == "my.boundary" for s in spans), [s.name for s in spans]


def test_boundary_emits_red_metric(in_memory_tracer, obs_registry):
    from prometheus_client import generate_latest

    from yadgar.observability.observe import observe

    @observe(tier="boundary", name="my.boundary")
    def handler():
        return 1

    handler()
    out = generate_latest(obs_registry).decode()
    assert "yadgar_observe_requests_total" in out
    assert 'name="my.boundary"' in out
    assert 'outcome="ok"' in out
    assert "yadgar_observe_request_duration_seconds" in out


def test_boundary_emits_info_log(in_memory_tracer, obs_registry, caplog):
    from yadgar.observability.observe import observe

    @observe(tier="boundary", name="my.boundary", log_event="handled")
    def handler():
        return 1

    with caplog.at_level(logging.INFO, logger="yadgar.observe"):
        handler()
    recs = [r for r in caplog.records if getattr(r, "action", None) == "handled"]
    assert recs, "expected an INFO log with action=handled"
    assert getattr(recs[0], "outcome", None) == "ok"
    assert hasattr(recs[0], "latency_ms")


def test_boundary_error_metric_and_log(in_memory_tracer, obs_registry, caplog):
    from prometheus_client import generate_latest

    from yadgar.observability.observe import observe

    @observe(tier="boundary", name="my.boundary", log_event="handled")
    def boom():
        raise ValueError("nope")

    with caplog.at_level(logging.ERROR), pytest.raises(ValueError):
        boom()
    out = generate_latest(obs_registry).decode()
    assert 'outcome="error"' in out
    err_recs = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert err_recs, "expected an ERROR log on raise"


# ── stage tier: shared stage family + ERROR-on-raise only ────────────────────


def test_stage_emits_span(in_memory_tracer, obs_registry):
    _, exporter = in_memory_tracer
    from yadgar.observability.observe import observe

    @observe(tier="stage", name="scoring")
    def stage_fn():
        return 1

    stage_fn()
    spans = exporter.get_finished_spans()
    assert any(s.name == "scoring" for s in spans), [s.name for s in spans]


def test_stage_uses_shared_histogram_family(in_memory_tracer, obs_registry):
    from prometheus_client import generate_latest

    from yadgar.observability.observe import observe

    @observe(tier="stage", name="scoring")
    def stage_fn():
        return 1

    stage_fn()
    out = generate_latest(obs_registry).decode()
    assert "yadgar_observe_stage_duration_seconds" in out
    assert 'stage="scoring"' in out
    # stage tier must NOT write a sample into the boundary RED requests counter.
    # (The counter family is declared in the registry, but no boundary outcome
    # sample must be emitted for a stage-tier call.)
    assert "yadgar_observe_requests_total{" not in out and "outcome=" not in out


def test_stage_no_info_log_but_error_on_raise(in_memory_tracer, obs_registry, caplog):
    from prometheus_client import generate_latest

    from yadgar.observability.observe import observe

    @observe(tier="stage", name="scoring")
    def ok_fn():
        return 1

    with caplog.at_level(logging.INFO):
        ok_fn()
    # stage tier emits no INFO-level app log on success
    info_recs = [r for r in caplog.records if r.levelno == logging.INFO]
    assert not info_recs, [r.getMessage() for r in info_recs]

    @observe(tier="stage", name="scoring")
    def boom():
        raise RuntimeError("x")

    caplog.clear()
    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError):
        boom()
    out = generate_latest(obs_registry).decode()
    assert "yadgar_observe_stage_errors_total" in out
    assert 'stage="scoring"' in out
    assert any(r.levelno >= logging.ERROR for r in caplog.records)


# ── hot tier: span only, no per-call metric/log ──────────────────────────────


def test_hot_no_metric_no_log(in_memory_tracer, obs_registry, caplog):
    from prometheus_client import generate_latest

    from yadgar.observability.observe import observe

    @observe(tier="hot", name="inner")
    def inner():
        return 42

    with caplog.at_level(logging.DEBUG):
        assert inner() == 42
    out = generate_latest(obs_registry).decode()
    # hot tier writes ZERO samples into any observe metric family
    assert 'name="inner"' not in out
    assert 'stage="inner"' not in out
    assert not [r for r in caplog.records if r.levelno >= logging.INFO]


# ── exempt: pure no-op ───────────────────────────────────────────────────────


def test_exempt_is_passthrough(in_memory_tracer, obs_registry):
    _, exporter = in_memory_tracer
    from yadgar.observability.observe import observe

    @observe(exempt="trivial")
    def pure():
        return 7

    assert pure() == 7
    assert not exporter.get_finished_spans()


# ── span=False: metric+log, no span (for fns with their own inner span) ───────


def test_span_false_opens_no_span_but_keeps_stage_metric(in_memory_tracer, obs_registry):
    """@observe(span=False) emits metric+log but opens NO span.

    For fns that already carry an EXPLICIT inner `with span(NAME)` grouping span
    (e.g. recall._apply_recall_side_effects, recall._fanout_recall): the @observe
    span would nest between the enclosing op and the inner grouping span, pushing
    the intended grouping span down a level. span=False suppresses the @observe
    span so the inner span reparents directly to the enclosing op — while the
    stage duration metric + lint sentinel are preserved.
    """
    from prometheus_client import generate_latest

    from yadgar.observability.observe import observe

    @observe(tier="stage", name="side_effects_stage", span=False)
    def stage_fn():
        return 1

    # sentinel still set (lint / stacked-decorator detection)
    assert getattr(stage_fn, "_yadgar_observe_has_span", False) is True

    stage_fn()
    # NO span opened by @observe
    assert not exporter_spans(in_memory_tracer), "span=False must open no span"
    # stage duration metric still emitted
    out = generate_latest(obs_registry).decode()
    assert "yadgar_observe_stage_duration_seconds" in out
    assert 'stage="side_effects_stage"' in out


def exporter_spans(in_memory_tracer):
    _, exporter = in_memory_tracer
    return [s.name for s in exporter.get_finished_spans()]


def test_span_false_inner_span_reparents_to_enclosing(in_memory_tracer, obs_registry):
    """A span=False fn with an inner `with span()` → inner span nests directly
    under the enclosing op, not under a redundant @observe span layer."""
    from opentelemetry import trace

    from yadgar.observability.observe import observe
    from yadgar.tracing import span

    _, exporter = in_memory_tracer

    @observe(tier="stage", name="outer_stage", span=False)
    def outer():
        with span("inner.group"):
            return 1

    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("enclosing"):
        outer()

    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert "inner.group" in spans, list(spans)
    assert "outer_stage" not in spans, "span=False must not open the @observe span"
    inner = spans["inner.group"]
    enclosing = spans["enclosing"]
    assert inner.parent is not None
    assert inner.parent.span_id == enclosing.context.span_id, (
        "inner.group must reparent directly under enclosing, not a redundant layer"
    )


# ── double-instrumentation guard ─────────────────────────────────────────────


def test_double_span_guard_emits_one_span(in_memory_tracer, obs_registry):
    """A fn carrying @trace_span AND @observe(boundary) must emit exactly ONE span."""
    _, exporter = in_memory_tracer
    from yadgar.observability.observe import observe
    from yadgar.tracing import trace_span

    @observe(tier="boundary", name="dup")
    @trace_span("dup.span")
    def handler():
        return "ok"

    assert handler() == "ok"
    spans = exporter.get_finished_spans()
    # exactly one span, not two nested "dup" spans
    assert len(spans) == 1, [s.name for s in spans]


# ── async support ────────────────────────────────────────────────────────────


def test_async_boundary(in_memory_tracer, obs_registry):
    import asyncio

    _, exporter = in_memory_tracer
    from yadgar.observability.observe import observe

    @observe(tier="boundary", name="async.boundary")
    async def handler():
        return "ok"

    assert asyncio.run(handler()) == "ok"
    spans = exporter.get_finished_spans()
    assert any(s.name == "async.boundary" for s in spans)


# ── staticmethod / classmethod descriptor preservation (BUG-2 regression) ─────
# @observe stacked ABOVE @staticmethod/@classmethod must preserve the descriptor
# so `self.method()` / `instance.method()` does NOT inject `self` as a positional
# arg. Reproduces the P5-rollout misfire where `@observe` turned a staticmethod
# into a plain function → "takes 0 positional arguments but 1 was given".


def test_observe_preserves_staticmethod(in_memory_tracer, obs_registry):
    from yadgar.observability.observe import observe

    class C:
        @observe(tier="hot", name="static.hot")
        @staticmethod
        def add(a, b):
            return a + b

    # call via class
    assert C.add(2, 3) == 5
    # call via instance — must NOT inject self
    assert C().add(4, 5) == 9


def test_observe_preserves_classmethod(in_memory_tracer, obs_registry):
    from yadgar.observability.observe import observe

    class C:
        marker = "cls"

        @observe(tier="hot", name="class.hot")
        @classmethod
        def who(cls):
            return cls.marker

    assert C.who() == "cls"
    assert C().who() == "cls"


def test_observe_staticmethod_boundary_emits_metric(in_memory_tracer, obs_registry):
    from prometheus_client import generate_latest

    from yadgar.observability.observe import observe

    class C:
        @observe(tier="boundary", name="static.boundary")
        @staticmethod
        def compute():
            return 7

    assert C().compute() == 7
    out = generate_latest(obs_registry).decode()
    # descriptor preserved AND signal still emitted (not silently exempted)
    assert 'name="static.boundary"' in out


def test_observe_exempt_staticmethod(in_memory_tracer, obs_registry):
    from yadgar.observability.observe import observe

    class C:
        @observe(exempt="trivial static")
        @staticmethod
        def add(a, b):
            return a + b

    assert C.add(1, 2) == 3
    assert C().add(3, 4) == 7


# ── lru_cache attribute preservation (BUG-3 regression) ───────────────────────
# @observe stacked ABOVE @functools.lru_cache must preserve the cache's public
# attributes (cache_info / cache_clear / cache_parameters). The P5 rollout stacked
# @observe above @lru_cache on tools.project._get_default_branch_cached /
# _detect_branch_cached / config_registry._yaml_layer; the wrapper hid cache_info,
# so callers doing `fn.cache_info().hits` raised AttributeError — which recall.py
# swallowed, collapsing the allowed-branch set and dropping seeded wikis from recall.


def test_observe_preserves_lru_cache_info(in_memory_tracer, obs_registry):
    import functools

    from yadgar.observability.observe import observe

    calls = {"n": 0}

    @observe(tier="stage", name="cached.stage")
    @functools.lru_cache(maxsize=8)
    def square(x):
        calls["n"] += 1
        return x * x

    # cache_info must be reachable through the observe wrapper
    assert callable(square.cache_info)
    assert square(4) == 16
    assert square(4) == 16  # second call hits the cache
    info = square.cache_info()
    assert info.hits >= 1, f"cache must still function through @observe; got {info}"
    assert calls["n"] == 1, "underlying fn must run only once (cache active)"
    # cache_clear must also be reachable
    assert callable(square.cache_clear)
    square.cache_clear()
    assert square.cache_info().hits == 0


# ── shutdown resilience: log emit during closed log stream (BUG-1) ────────────
# An @observe'd fn that runs at interpreter/test shutdown (atexit teardown) may
# hit a CLOSED log stream. A span/metric/log firing during shutdown must NEVER
# raise (ValueError: I/O operation on closed file). Guards _emit_success AND
# _emit_error.


def test_emit_success_survives_closed_log_stream(monkeypatch, in_memory_tracer, obs_registry):
    """At interpreter shutdown the log stream may be closed → logging raises
    `ValueError: I/O operation on closed file`. _emit_success must swallow it."""
    import yadgar.observability.observe as obs

    def _boom_info(*a, **k):
        raise ValueError("I/O operation on closed file")

    monkeypatch.setattr(obs.logger, "info", _boom_info)

    @obs.observe(tier="boundary", name="shutdown.ok")
    def teardown():
        return "done"

    # Without the try/except guard in _emit_success, this ValueError propagates.
    assert teardown() == "done"


def test_emit_error_survives_closed_log_stream(monkeypatch, in_memory_tracer, obs_registry):
    """_emit_error's log emit must not raise at shutdown — and must NOT mask the
    original exception the wrapped fn raised."""
    import yadgar.observability.observe as obs

    def _boom_error(*a, **k):
        raise ValueError("I/O operation on closed file")

    monkeypatch.setattr(obs.logger, "error", _boom_error)

    @obs.observe(tier="boundary", name="shutdown.err")
    def boom():
        raise RuntimeError("teardown boom")

    # The RuntimeError must propagate; the closed-stream ValueError must NOT
    # replace it or raise on its own.
    with pytest.raises(RuntimeError, match="teardown boom"):
        boom()
