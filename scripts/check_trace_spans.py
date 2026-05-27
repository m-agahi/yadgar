#!/usr/bin/env python3
"""I19-extended (I24) — Public HTTP handler MUST have @trace_span.

Scans observable modules for public methods / top-level functions that lack a
@trace_span decorator.  Ships as companion to I23 (scripts/check_metric_writers.py).

Observable scope (narrow by design — avoids false positives on helpers):
  yadgar/server/http.py  — top-level async def functions not starting with "_"

A function is considered "spanned" if the line immediately before its `def`
statement contains `@trace_span` (with optional arguments).

Usage:
  python scripts/check_trace_spans.py            # check, exit 0/1
  python scripts/check_trace_spans.py --list-all # list all + span status
  python scripts/check_trace_spans.py --allowlist fn1,fn2

Exit codes:
  0  all public observable functions have @trace_span (or are allowlisted)
  1  one or more public observable functions are missing @trace_span
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path
from typing import NamedTuple

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent

# Files to scan and what constitutes a "public" function within each.
# Each entry: (path_relative_to_repo, is_top_level_only, name_filter_fn)
#
# "top-level only" means we only look at module-level defs, not class methods.
# name_filter_fn(name) -> True if the function name should be checked.
_OBSERVABLE_FILES: list[tuple[Path, bool, object]] = [
    (
        _REPO_ROOT / "yadgar" / "server" / "http.py",
        True,  # top-level only
        lambda name: not name.startswith("_"),  # skip private helpers
    ),
]

_TRACE_SPAN_DECORATOR = "trace_span"


class FunctionInfo(NamedTuple):
    func_name: str
    source_file: Path
    lineno: int
    has_span: bool


# ---------------------------------------------------------------------------
# AST scanner
# ---------------------------------------------------------------------------


def _is_trace_span(decorator: ast.expr) -> bool:
    """Return True if decorator node represents @trace_span or @trace_span(...)."""
    # Unwrap Call node to get the underlying name/attr node.
    node = decorator.func if isinstance(decorator, ast.Call) else decorator
    if isinstance(node, ast.Name):
        return node.id == _TRACE_SPAN_DECORATOR
    if isinstance(node, ast.Attribute):
        return node.attr == _TRACE_SPAN_DECORATOR
    return False


def _scan_file(
    src_file: Path,
    top_level_only: bool,
    name_filter,
) -> list[FunctionInfo]:
    """Parse src_file and return FunctionInfo for each matching function."""
    try:
        source = src_file.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(src_file))
    except SyntaxError as exc:
        print(f"WARNING: could not parse {src_file}: {exc}", file=sys.stderr)
        return []
    except OSError as exc:
        print(f"WARNING: could not read {src_file}: {exc}", file=sys.stderr)
        return []

    results: list[FunctionInfo] = []

    if top_level_only:
        candidates = [
            node
            for node in ast.iter_child_nodes(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
    else:
        candidates = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]

    for node in candidates:
        if not name_filter(node.name):
            continue
        has_span = any(_is_trace_span(d) for d in node.decorator_list)
        results.append(
            FunctionInfo(
                func_name=node.name,
                source_file=src_file,
                lineno=node.lineno,
                has_span=has_span,
            )
        )

    return results


# ---------------------------------------------------------------------------
# Public scan API (used by tests)
# ---------------------------------------------------------------------------


def scan(
    repo_root: Path | None = None,
    allowlist: set[str] | None = None,
) -> tuple[list[FunctionInfo], list[FunctionInfo]]:
    """Scan all observable files.

    Returns (all_functions, missing_span) where missing_span excludes allowlisted names.
    """
    if repo_root is None:
        repo_root = _REPO_ROOT
    if allowlist is None:
        allowlist = set()

    observable_files = [
        (
            repo_root / "yadgar" / "server" / "http.py",
            True,
            lambda name: not name.startswith("_"),
        ),
    ]

    all_fns: list[FunctionInfo] = []
    missing: list[FunctionInfo] = []

    for rel_path, top_level_only, name_filter in observable_files:
        if not rel_path.exists():
            print(f"WARNING: observable file not found: {rel_path}", file=sys.stderr)
            continue
        fns = _scan_file(rel_path, top_level_only, name_filter)
        all_fns.extend(fns)
        for fn in fns:
            if not fn.has_span and fn.func_name not in allowlist:
                missing.append(fn)

    return all_fns, missing


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="I24 — check that public HTTP-handler functions have @trace_span.",
    )
    parser.add_argument(
        "--list-all",
        action="store_true",
        help="Print every scanned function and its span status, then exit 0.",
    )
    parser.add_argument(
        "--allowlist",
        metavar="FN[,FN...]",
        default="",
        help="Comma-separated function names to exempt from the check.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    allowlist: set[str] = {v.strip() for v in args.allowlist.split(",") if v.strip()}

    all_fns, missing = scan(allowlist=allowlist)

    if args.list_all:
        for fn in all_fns:
            status = (
                "ALLOWLISTED" if fn.func_name in allowlist else ("OK" if fn.has_span else "MISSING")
            )
            print(f"{fn.source_file}:{fn.lineno}: {fn.func_name} [{status}]")
        return 0

    for fn in missing:
        print(f"{fn.source_file}:{fn.lineno}: {fn.func_name} — missing @trace_span decorator")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
