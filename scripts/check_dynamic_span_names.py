#!/usr/bin/env python3
"""R2b drift-guard — span names MUST stay dynamic (module.qualname).

Reorg Round 2b (Cars 1+2) made every decorator-sourced span name dynamic:
``@trace_span("X")`` → ``@trace_span()`` and ``@observe(name="X")`` →
``@observe(metric="X")``, so a span's name is now ``f"{fn.__module__}.{fn.__qualname__}"``
and follows the code when it moves. This checker prevents regression: it AST-scans
``yadgar/**/*.py`` (tests excluded) and FAILS if any REAL decorator hardcodes a
span name via a string literal.

REJECTED (a hardcoded span name — reintroduces the drift Cars 1+2 removed):
  @trace_span("literal")            positional str constant
  @trace_span(name="literal")       name= str constant
  @observe(name="literal")          name= str constant (observe is keyword-only)
  @observe(..., name="lit", ...)    name= anywhere in the call

ALLOWED (span name stays dynamic, or the literal is not a span name):
  @trace_span()  /  @trace_span     bare — dynamic module.qualname
  @trace_span(attributes={...})     attributes, not a name
  @trace_span(name=SOME_CONST)      non-literal name (a variable) — not a hardcode
  @observe(metric="X")              Prometheus metric LABEL, preserved by design
  @observe(tier="boundary")         tier / log_event / exempt kwargs
  span("recall.side_effects")       inline free-function CM — ADR-0061 exception;
                                    it is a *call*, not in a decorator_list, so the
                                    AST scope (decorator_list only) never sees it.
  "…@trace_span(\"x\")…" in a docstring / string literal — AST parses it as a
                                    Constant str, never a decorator node.

Scope is ``node.decorator_list`` (mirrors check_trace_spans._is_trace_span, incl.
the Attribute form ``mod.trace_span``), NOT ``ast.walk`` of every Call — that
automatically exempts inline ``span("…")`` calls and the internal
``trace_span(span_name, …)`` call inside observe.py's own machinery.

Stdlib-only. Modeled on scripts/check_trace_spans.py (I24) +
scripts/check_observe_coverage.py (I33).

Usage:
  python scripts/check_dynamic_span_names.py                 # check yadgar/, exit 0/1
  python scripts/check_dynamic_span_names.py --root <dir>    # scan a different root
  python scripts/check_dynamic_span_names.py --list-all      # list every violation

Exit codes:
  0  no hardcoded span names found
  1  one or more decorators hardcode a span name
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Decorators whose span name is expected to be dynamic. A hardcoded name on any
# of these reintroduces the drift Cars 1+2 removed.
_SPAN_DECORATORS = {"trace_span", "observe"}


@dataclass(frozen=True)
class Violation:
    source_file: Path
    lineno: int
    func_name: str
    decorator: str
    literal: str


def _decorator_name(dec: ast.expr) -> str | None:
    """Return the bare decorator name, unwrapping Call and Attribute forms.

    Mirrors check_trace_spans._is_trace_span / check_observe_coverage._decorator_name:
    handles ``@trace_span``, ``@trace_span(...)``, and ``@mod.trace_span(...)``.
    """
    node = dec.func if isinstance(dec, ast.Call) else dec
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _str_literal(value: ast.expr) -> str | None:
    """Return the string value if `value` is a string Constant, else None."""
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value
    return None


def _hardcoded_span_name(dec: ast.Call, dec_name: str) -> str | None:
    """Return the hardcoded span-name literal for this decorator, else None.

    - trace_span: a positional str constant OR name="literal".
    - observe:    name="literal" only (observe is keyword-only; positional args
                  are impossible, and metric=/tier=/log_event= are NOT span names).
    """
    # name="literal" applies to both trace_span and observe.
    for kw in dec.keywords:
        if kw.arg == "name":
            lit = _str_literal(kw.value)
            if lit is not None:
                return lit

    # Positional str constant — only trace_span("literal") (observe is kw-only).
    if dec_name == "trace_span" and dec.args:
        lit = _str_literal(dec.args[0])
        if lit is not None:
            return lit

    return None


def _scan_node(node: ast.FunctionDef | ast.AsyncFunctionDef, src_file: Path) -> list[Violation]:
    """Inspect a single function's decorator_list for hardcoded span names."""
    out: list[Violation] = []
    for dec in node.decorator_list:
        if not isinstance(dec, ast.Call):
            continue  # bare @trace_span / @observe carry no name — always fine
        dec_name = _decorator_name(dec)
        if dec_name not in _SPAN_DECORATORS:
            continue
        literal = _hardcoded_span_name(dec, dec_name)
        if literal is not None:
            out.append(
                Violation(
                    source_file=src_file,
                    lineno=dec.lineno,
                    func_name=node.name,
                    decorator=dec_name,
                    literal=literal,
                )
            )
    return out


def scan_file(src_file: Path) -> list[Violation]:
    """Parse src_file and return every hardcoded-span-name violation in it."""
    try:
        source = src_file.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(src_file))
    except SyntaxError as exc:
        print(f"WARNING: could not parse {src_file}: {exc}", file=sys.stderr)
        return []
    except OSError as exc:
        print(f"WARNING: could not read {src_file}: {exc}", file=sys.stderr)
        return []

    out: list[Violation] = []
    for sub in ast.walk(tree):
        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.extend(_scan_node(sub, src_file))
    return out


def _iter_py_files(root: Path) -> list[Path]:
    """Yield in-scope .py files under root (tests excluded, mirrors I33)."""
    files: list[Path] = []
    for p in sorted(root.rglob("*.py")):
        rel = str(p)
        if "/tests/" in rel or rel.endswith("_test.py") or p.name.startswith("test_"):
            continue
        files.append(p)
    return files


def scan(root: Path | None = None) -> list[Violation]:
    """Scan all in-scope files under root and return every violation."""
    if root is None:
        root = _REPO_ROOT / "yadgar"
    violations: list[Violation] = []
    for f in _iter_py_files(root):
        violations.extend(scan_file(f))
    return violations


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="R2b — reject hardcoded span names in @trace_span/@observe decorators.",
    )
    parser.add_argument(
        "--root",
        default=str(_REPO_ROOT / "yadgar"),
        help="Directory to scan (default: yadgar/).",
    )
    parser.add_argument(
        "--list-all",
        action="store_true",
        help="Print every violation (same as default failure output).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    violations = scan(Path(args.root))

    for v in violations:
        print(
            f"{v.source_file}:{v.lineno}: {v.func_name} — "
            f"@{v.decorator} hardcodes span name {v.literal!r}. "
            f"Use @{v.decorator}() for a dynamic module.qualname span name"
            + (" (put the metric label in metric=)" if v.decorator == "observe" else "")
            + "."
        )

    if violations:
        print(
            f"\n{len(violations)} hardcoded span name(s) found — span names must stay "
            "dynamic (module.qualname). See scripts/check_dynamic_span_names.py.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
