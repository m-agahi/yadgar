"""I33 P5 — hook @observe boundary instrumentation tests.

Asserts that hook main() functions emit spans when decorated with
@observe(tier="boundary"). Uses InMemorySpanExporter fixture from
test_observe_decorator.py pattern.

All tests feed empty / minimal stdin so main() early-returns without
hitting the daemon (model-free, no network I/O).

Tests also verify shutdown_tracing() is called after main() (span
processor must flush before process exit).
"""

from __future__ import annotations

import io
import sys
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _has_observe_sentinel(fn) -> bool:
    """Return True if fn has the _yadgar_observe_has_span sentinel set by @observe."""
    return bool(getattr(fn, "_yadgar_observe_has_span", False))


# ---------------------------------------------------------------------------
# instructions_loaded — clean early-exit path (load_reason absent → no fire)
# ---------------------------------------------------------------------------


class TestInstructionsLoadedHookSpan:
    """instructions_loaded.main() emits span on empty input (early-return path)."""

    def test_main_has_observe_sentinel(self):
        """@observe sets _yadgar_observe_has_span sentinel on main."""
        from yadgar.core.hooks.instructions_loaded import main

        assert _has_observe_sentinel(main), (
            "instructions_loaded.main must have _yadgar_observe_has_span=True "
            "(set by @observe decorator)"
        )

    def test_main_emits_span_on_early_return(self, in_memory_tracer, monkeypatch):
        """main() emits exactly one span even when early-returning (no load_reason)."""
        _tracer, exporter = in_memory_tracer

        # Empty stdin → _should_fire returns False → early return, no daemon call
        monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
        # Patch shutdown_tracing to no-op (no OTel endpoint in test)
        with patch("yadgar.core.hooks.instructions_loaded.shutdown_tracing"):
            from yadgar.core.hooks.instructions_loaded import main as instructions_main

            instructions_main()

        spans = exporter.get_finished_spans()
        assert len(spans) >= 1, (
            f"Expected >=1 span from instructions_loaded.main, got {len(spans)}. "
            "The @observe(tier='boundary') decorator must wrap the entire call."
        )

    def test_main_span_name_contains_main(self, in_memory_tracer, monkeypatch):
        """Span name must identify the function (contains 'main' or module path)."""
        _tracer, exporter = in_memory_tracer

        monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
        with patch("yadgar.core.hooks.instructions_loaded.shutdown_tracing"):
            from yadgar.core.hooks.instructions_loaded import main as instructions_main

            instructions_main()

        spans = exporter.get_finished_spans()
        assert spans, "No spans emitted"
        span_names = [s.name for s in spans]
        assert any("main" in n.lower() for n in span_names), (
            f"Expected span name containing 'main', got: {span_names}"
        )


# ---------------------------------------------------------------------------
# file_changed — empty file_path → early return
# ---------------------------------------------------------------------------


class TestFileChangedHookSpan:
    """file_changed.main() emits span on empty-input early-return."""

    def test_main_has_observe_sentinel(self):
        from yadgar.core.hooks.file_changed import main

        assert _has_observe_sentinel(main), (
            "file_changed.main must have _yadgar_observe_has_span=True"
        )

    def test_main_emits_span_on_empty_input(self, in_memory_tracer, monkeypatch):
        """main() emits span even when file_path is absent (early return)."""
        _tracer, exporter = in_memory_tracer

        monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
        with patch("yadgar.core.hooks.file_changed.shutdown_tracing"):
            from yadgar.core.hooks.file_changed import main as file_changed_main

            file_changed_main()

        spans = exporter.get_finished_spans()
        assert len(spans) >= 1, f"Expected >=1 span from file_changed.main, got {len(spans)}"


# ---------------------------------------------------------------------------
# subagent_start — daemon call is skipped when daemon is down (timeout)
# ---------------------------------------------------------------------------


class TestSubagentStartHookSpan:
    """subagent_start.main() emits span; _call_daemon skipped via monkeypatch."""

    def test_main_has_observe_sentinel(self):
        from yadgar.core.hooks.subagent_start import main

        assert _has_observe_sentinel(main), (
            "subagent_start.main must have _yadgar_observe_has_span=True"
        )

    def test_main_emits_span_with_patched_daemon(self, in_memory_tracer, monkeypatch):
        """main() emits span; daemon call is patched to return empty string."""
        _tracer, exporter = in_memory_tracer

        monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
        # Patch _call_daemon so we don't hit the 2s socket timeout
        with patch("yadgar.core.hooks.subagent_start._call_daemon", return_value=""):
            with patch("yadgar.core.hooks.subagent_start.shutdown_tracing"):
                from yadgar.core.hooks.subagent_start import main as subagent_start_main

                subagent_start_main()

        spans = exporter.get_finished_spans()
        assert len(spans) >= 1, f"Expected >=1 span from subagent_start.main, got {len(spans)}"


# ---------------------------------------------------------------------------
# subagent_stop — empty stdin → early return (no transcript)
# ---------------------------------------------------------------------------


class TestSubagentStopHookSpan:
    """subagent_stop.main() emits span on empty-input early-return."""

    def test_main_has_observe_sentinel(self):
        from yadgar.core.hooks.subagent_stop import main

        assert _has_observe_sentinel(main), (
            "subagent_stop.main must have _yadgar_observe_has_span=True"
        )

    def test_main_emits_span_on_empty_input(self, in_memory_tracer, monkeypatch):
        """main() emits span even when stdin is empty (early return, no transcript)."""
        _tracer, exporter = in_memory_tracer

        monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
        with patch("yadgar.core.hooks.subagent_stop.shutdown_tracing"):
            from yadgar.core.hooks.subagent_stop import main as subagent_stop_main

            subagent_stop_main()

        spans = exporter.get_finished_spans()
        assert len(spans) >= 1, f"Expected >=1 span from subagent_stop.main, got {len(spans)}"


# ---------------------------------------------------------------------------
# shutdown_tracing integration — verify flush called in finally block
# ---------------------------------------------------------------------------


class TestShutdownTracingFlush:
    """shutdown_tracing() is called in the finally block of each hook main()."""

    def test_instructions_loaded_flushes_on_exit(self, monkeypatch):
        """shutdown_tracing called even on early return from instructions_loaded.main."""
        flush_called = []

        monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
        with patch(
            "yadgar.core.hooks.instructions_loaded.shutdown_tracing",
            side_effect=lambda *a, **kw: flush_called.append(True),
        ):
            from yadgar.core.hooks.instructions_loaded import main as instructions_main

            instructions_main()

        assert flush_called, (
            "shutdown_tracing() must be called in finally block of instructions_loaded.main. "
            "Hook processes are short-lived; BatchSpanProcessor will not flush without it."
        )

    def test_file_changed_flushes_on_exit(self, monkeypatch):
        """shutdown_tracing called on empty-input early return from file_changed.main."""
        flush_called = []

        monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
        with patch(
            "yadgar.core.hooks.file_changed.shutdown_tracing",
            side_effect=lambda *a, **kw: flush_called.append(True),
        ):
            from yadgar.core.hooks.file_changed import main as file_changed_main

            file_changed_main()

        assert flush_called, (
            "shutdown_tracing() must be called in finally block of file_changed.main."
        )

    def test_subagent_stop_flushes_on_exit(self, monkeypatch):
        """shutdown_tracing called on empty-input early return from subagent_stop.main."""
        flush_called = []

        monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
        with patch(
            "yadgar.core.hooks.subagent_stop.shutdown_tracing",
            side_effect=lambda *a, **kw: flush_called.append(True),
        ):
            from yadgar.core.hooks.subagent_stop import main as subagent_stop_main

            subagent_stop_main()

        assert flush_called, (
            "shutdown_tracing() must be called in finally block of subagent_stop.main."
        )
