"""I33 v2 (ADR-0085 / P-SB §3.4 Commit A) — _span_budget + ADR-0041 lint tests.

Four new lint capabilities:

1. ``_span_budget`` allowlist section — ``module:qualname -> {rationale}`` (>=40
   chars), meaning "this fn must NOT open a per-call span". Lint HARD-FAILS a
   listed fn that carries a span-OPENING decorator (``@observe`` without
   ``span=False``, ``@trace_span``, ``@_tool``). Stale ``_span_budget`` entry
   (matches no in-scope fn) hard-fails, same governance as the other sections.

2. Advisory NON-failing loop-heuristic: a span-decorated fn whose name is called
   inside a ``for``/``while`` in the same module → stdout report, never affects
   the exit code (ADR-0040 glob-audit report pattern).

3. ADR-0041 HARD rule: a span-opening decorator in the logging-handler module set
   (``log_config`` + the ``LogSpanProcessor`` class in ``tracing``) hard-fails.

4. (docstring widening in observe.py — not lint-testable here.)

KEY-FORM NOTE: every allowlist key is ``module:qualname`` where ``module`` is the
file STEM — so ``_cosine_similarity`` in server_helpers.py is keyed
``server_helpers:_cosine_similarity``, NOT its ``metric=`` label
``tools.project._cosine_similarity``. Keying by the metric string would silently
no-op the section (stale-check fires, hard-fail lookup never matches).
"""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).parent.parent.parent.parent / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import check_observe_coverage  # noqa: E402


def _write(tmp_path: Path, name: str, src: str) -> Path:
    p = tmp_path / name
    p.write_text(textwrap.dedent(src), encoding="utf-8")
    return p


def _write_allowlist(tmp_path: Path, obj: dict) -> Path:
    p = tmp_path / ".observe-allowlist.json"
    p.write_text(json.dumps(obj), encoding="utf-8")
    return p


# ── _span_budget hard-fail: listed fn opens a per-call span ───────────────────


def test_span_budget_listed_fn_opening_span_hard_fails(tmp_path):
    """A _span_budget fn that carries @observe (span-opening) → hard-fail exit 1."""
    root = tmp_path / "yadgar"
    root.mkdir()
    _write(
        root,
        "hotmod.py",
        """
        from yadgar._shared.observability.observe import observe

        @observe(tier="hot", metric="x.hot_fn")
        def hot_fn(items):
            total = 0
            for x in items:
                total += x
            return total
        """,
    )
    _write_allowlist(
        tmp_path,
        {
            "_span_budget": {
                "hotmod:hot_fn": {
                    "rationale": "per-item hot-loop helper; must carry span=False so no per-call span is opened",
                }
            }
        },
    )
    rc = check_observe_coverage.main(
        [
            "--warn",
            "--root",
            str(root),
            "--allowlist-file",
            str(tmp_path / ".observe-allowlist.json"),
        ]
    )
    assert rc == 1, "a _span_budget fn opening a per-call span must hard-fail"


def test_span_budget_listed_fn_with_span_false_passes(tmp_path):
    """A _span_budget fn carrying @observe(span=False) satisfies the budget (no fail)."""
    root = tmp_path / "yadgar"
    root.mkdir()
    _write(
        root,
        "hotmod.py",
        """
        from yadgar._shared.observability.observe import observe

        @observe(tier="hot", metric="x.hot_fn", span=False)
        def hot_fn(items):
            total = 0
            for x in items:
                total += x
            return total
        """,
    )
    _write_allowlist(
        tmp_path,
        {
            "_span_budget": {
                "hotmod:hot_fn": {
                    "rationale": "per-item hot-loop helper; must carry span=False so no per-call span is opened",
                }
            }
        },
    )
    rc = check_observe_coverage.main(
        [
            "--warn",
            "--root",
            str(root),
            "--allowlist-file",
            str(tmp_path / ".observe-allowlist.json"),
        ]
    )
    assert rc == 0, "a _span_budget fn with span=False must NOT fail the budget"


def test_span_budget_stale_entry_hard_fails(tmp_path):
    """A _span_budget key matching no in-scope fn → stale hard-fail."""
    root = tmp_path / "yadgar"
    root.mkdir()
    _write(
        root,
        "hotmod.py",
        """
        def real_fn():
            x = 1
            for i in range(3):
                x += i
            return x
        """,
    )
    _write_allowlist(
        tmp_path,
        {
            "_span_budget": {
                "hotmod:ghost_fn": {
                    "rationale": "no such function in scope — this stale span-budget entry must hard-fail",
                }
            }
        },
    )
    rc = check_observe_coverage.main(
        [
            "--warn",
            "--root",
            str(root),
            "--allowlist-file",
            str(tmp_path / ".observe-allowlist.json"),
        ]
    )
    assert rc == 1, "a stale _span_budget entry must hard-fail"


def test_span_budget_short_rationale_hard_fails(tmp_path):
    """A _span_budget entry with a <40-char rationale → integrity hard-fail."""
    root = tmp_path / "yadgar"
    root.mkdir()
    _write(
        root,
        "hotmod.py",
        """
        from yadgar._shared.observability.observe import observe

        @observe(tier="hot", metric="x.hot_fn", span=False)
        def hot_fn(items):
            total = 0
            for x in items:
                total += x
            return total
        """,
    )
    _write_allowlist(
        tmp_path,
        {"_span_budget": {"hotmod:hot_fn": {"rationale": "too short"}}},
    )
    rc = check_observe_coverage.main(
        [
            "--warn",
            "--root",
            str(root),
            "--allowlist-file",
            str(tmp_path / ".observe-allowlist.json"),
        ]
    )
    assert rc == 1, "a short _span_budget rationale must hard-fail"


# ── ADR-0041: span-opening decorator forbidden in the logging-handler set ─────


def test_adr0041_span_decorator_in_log_config_hard_fails(tmp_path):
    """A span-opening decorator in a `log_config` module → ADR-0041 hard-fail."""
    root = tmp_path / "yadgar"
    root.mkdir()
    _write(
        root,
        "log_config.py",
        """
        from yadgar._shared.observability.observe import observe

        @observe(tier="stage", metric="log.handler_fn")
        def handler_fn(record):
            out = []
            for x in record:
                out.append(x)
            return out
        """,
    )
    _write_allowlist(tmp_path, {})
    rc = check_observe_coverage.main(
        [
            "--warn",
            "--root",
            str(root),
            "--allowlist-file",
            str(tmp_path / ".observe-allowlist.json"),
        ]
    )
    assert rc == 1, "a span decorator in the logging-handler module set must hard-fail (ADR-0041)"


def test_adr0041_span_decorator_on_logspanprocessor_hard_fails(tmp_path):
    """A span-opening decorator on a LogSpanProcessor method → ADR-0041 hard-fail."""
    root = tmp_path / "yadgar"
    root.mkdir()
    _write(
        root,
        "tracing.py",
        """
        from yadgar._shared.observability.observe import observe

        class LogSpanProcessor:
            @observe(tier="stage", metric="log.on_end")
            def on_end(self, span):
                out = []
                for x in span:
                    out.append(x)
                return out
        """,
    )
    _write_allowlist(tmp_path, {})
    rc = check_observe_coverage.main(
        [
            "--warn",
            "--root",
            str(root),
            "--allowlist-file",
            str(tmp_path / ".observe-allowlist.json"),
        ]
    )
    assert rc == 1, "a span decorator on LogSpanProcessor must hard-fail (ADR-0041)"


def test_adr0041_plain_tracing_fn_not_flagged(tmp_path):
    """A non-LogSpanProcessor span-opener in tracing.py is NOT ADR-0041-flagged
    (the rule targets the log-handler surface, not trace_span's own infra)."""
    root = tmp_path / "yadgar"
    root.mkdir()
    _write(
        root,
        "tracing.py",
        """
        from yadgar._shared.observability.observe import observe

        @observe(tier="stage", metric="some.other_fn")
        def some_other_fn(items):
            total = 0
            for x in items:
                total += x
            return total
        """,
    )
    _write_allowlist(tmp_path, {})
    rc = check_observe_coverage.main(
        [
            "--warn",
            "--root",
            str(root),
            "--allowlist-file",
            str(tmp_path / ".observe-allowlist.json"),
        ]
    )
    assert rc == 0, "ADR-0041 must NOT flag a plain span-opener outside LogSpanProcessor"


# ── advisory loop-heuristic report (non-failing, stdout) ──────────────────────


def test_loop_heuristic_report_is_advisory_and_prints(tmp_path, capsys):
    """A span-decorated fn called inside a for/while in the same module is reported
    to stdout but NEVER fails the exit code."""
    root = tmp_path / "yadgar"
    root.mkdir()
    _write(
        root,
        "loopmod.py",
        """
        from yadgar._shared.observability.observe import observe

        @observe(tier="stage", metric="x.per_item")
        def per_item(x):
            y = 0
            for i in range(x):
                y += i
            return y

        def driver(items):
            out = []
            for it in items:
                out.append(per_item(it))
            return out
        """,
    )
    _write_allowlist(tmp_path, {})
    rc = check_observe_coverage.main(
        [
            "--warn",
            "--root",
            str(root),
            "--allowlist-file",
            str(tmp_path / ".observe-allowlist.json"),
        ]
    )
    assert rc == 0, "the loop-heuristic report is advisory — must never fail the exit code"
    out = capsys.readouterr().out
    assert "loop" in out.lower(), out
    assert "per_item" in out, out
