"""Tests for scripts/check_dynamic_span_names.py (R2b span-name drift-guard).

The checker AST-scans yadgar/**/*.py and FAILS if any real @trace_span/@observe
decorator hardcodes a span name (positional str or name="literal"). It ALLOWS
bare decorators, @observe(metric=...), inline span("...") calls (ADR-0061 CM
exception), and string literals inside docstrings/strings (AST skips those).

Run:
  uv run pytest yadgar/tests/test_check_dynamic_span_names.py
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

SCRIPT = Path(__file__).parent.parent.parent.parent / "scripts" / "check_dynamic_span_names.py"


def run_script(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
    )


def _make_root(tmp_path: Path, src: str, name: str = "mod.py") -> Path:
    """Write `src` into a scannable root dir and return that root."""
    root = tmp_path / "pkg"
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(textwrap.dedent(src))
    return root


# ---------------------------------------------------------------------------
# Violations → exit 1
# ---------------------------------------------------------------------------


def test_trace_span_positional_literal_fails(tmp_path):
    """@trace_span("X") — positional str constant is a hardcoded name → exit 1."""
    root = _make_root(
        tmp_path,
        """\
        def trace_span(*a, **k):
            def deco(fn):
                return fn
            return deco

        @trace_span("storage.vector.search")
        def search():
            return None
        """,
    )
    res = run_script("--root", str(root))
    assert res.returncode == 1, res.stdout
    assert "search" in res.stdout
    assert "storage.vector.search" in res.stdout


def test_trace_span_name_kwarg_literal_fails(tmp_path):
    """@trace_span(name="X") → exit 1."""
    root = _make_root(
        tmp_path,
        """\
        def trace_span(*a, **k):
            def deco(fn):
                return fn
            return deco

        @trace_span(name="hook.health")
        def health():
            return None
        """,
    )
    res = run_script("--root", str(root))
    assert res.returncode == 1, res.stdout
    assert "hook.health" in res.stdout


def test_observe_name_kwarg_literal_fails(tmp_path):
    """@observe(name="X") → exit 1 (observe span name must stay dynamic)."""
    root = _make_root(
        tmp_path,
        """\
        def observe(*a, **k):
            def deco(fn):
                return fn
            return deco

        @observe(tier="boundary", name="backend.health")
        def health():
            return None
        """,
    )
    res = run_script("--root", str(root))
    assert res.returncode == 1, res.stdout
    assert "backend.health" in res.stdout


# ---------------------------------------------------------------------------
# Clean forms → exit 0
# ---------------------------------------------------------------------------


def test_bare_and_dynamic_decorators_pass(tmp_path):
    """Bare @trace_span(), @observe(metric=), @observe(tier=), attributes= all pass."""
    root = _make_root(
        tmp_path,
        """\
        def trace_span(*a, **k):
            def deco(fn):
                return fn
            return deco

        def observe(*a, **k):
            def deco(fn):
                return fn
            return deco

        @trace_span()
        def a():
            return None

        @trace_span(attributes={"k": "v"})
        def b():
            return None

        @observe(metric="my.metric.label")
        def c():
            return None

        @observe(tier="stage")
        def d():
            return None
        """,
    )
    res = run_script("--root", str(root))
    assert res.returncode == 0, res.stdout


def test_inline_span_call_is_allowed(tmp_path):
    """An inline `with span("curated")` CALL is not a decorator → not flagged (ADR-0061)."""
    root = _make_root(
        tmp_path,
        """\
        from contextlib import nullcontext

        def span(name, **kw):
            return nullcontext()

        def side_effects():
            # span(): curated landmark name — inline CM can't auto-derive (ADR-0061 exception)
            with span("recall.side_effects.db"):
                return None
        """,
    )
    res = run_script("--root", str(root))
    assert res.returncode == 0, res.stdout


def test_string_literal_in_docstring_not_flagged(tmp_path):
    """A `@trace_span("x")` INSIDE a docstring/string is AST-parsed as a str Constant,
    never a decorator node → must NOT be flagged (proves AST-not-regex)."""
    root = _make_root(
        tmp_path,
        '''\
        def trace_span(*a, **k):
            def deco(fn):
                return fn
            return deco

        def documented():
            """Example usage::

                @trace_span("some.hardcoded.name")
                def foo(): ...
            """
            return None

        SNIPPET = 'use @trace_span("also.not.real") here'
        ''',
    )
    res = run_script("--root", str(root))
    assert res.returncode == 0, res.stdout


def test_name_variable_not_flagged(tmp_path):
    """@trace_span(name=SOME_VAR) is not a hardcoded literal → allowed."""
    root = _make_root(
        tmp_path,
        """\
        def trace_span(*a, **k):
            def deco(fn):
                return fn
            return deco

        _NAME = "computed"

        @trace_span(name=_NAME)
        def a():
            return None
        """,
    )
    res = run_script("--root", str(root))
    assert res.returncode == 0, res.stdout


def test_tests_dir_excluded(tmp_path):
    """A violating file under a tests/ dir is excluded from the scan."""
    tests_dir = tmp_path / "pkg" / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_x.py").write_text(
        textwrap.dedent(
            """\
            def trace_span(*a, **k):
                def deco(fn):
                    return fn
                return deco

            @trace_span("intentional.fixture.name")
            def fixture_fn():
                return None
            """
        )
    )
    res = run_script("--root", str(tmp_path / "pkg"))
    assert res.returncode == 0, res.stdout
