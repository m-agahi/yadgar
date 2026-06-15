#!/usr/bin/env python3
"""I23 — Declared metric MUST have ≥1 writer.

Scans metric declaration files (yadgar/metrics.py, yadgar/backend/embed_service_metrics.py)
for Prometheus metric objects (Gauge, Counter, Histogram, Summary) and verifies that
every declared variable has at least one write/reference site elsewhere in yadgar/.

Usage:
  python scripts/check_metric_writers.py            # check, exit 0/1
  python scripts/check_metric_writers.py --list-all # list all + writer counts
  python scripts/check_metric_writers.py --allowlist var1,var2

Exit codes:
  0  all declared metrics have ≥1 writer (or are allowlisted)
  1  one or more declared metrics have no writers

"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path
from typing import NamedTuple

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

METRIC_CONSTRUCTORS = {"Gauge", "Counter", "Histogram", "Summary"}

# Patterns that count as a "writer/reference" — lenient by design (false
# negatives are worse than false positives per spec).
#   <var>.set(...)
#   <var>.inc(...)
#   <var>.observe(...)
#   <var>.labels(...)
#   <var> passed as positional/keyword argument: any call containing <var>
_WRITER_METHOD_RE = re.compile(r"\b{var}\s*\.(set|inc|observe|labels)\s*\(")
_ARG_PASS_RE = re.compile(r"\b{var}\b")  # broad: any occurrence outside declaration file


class MetricDecl(NamedTuple):
    var_name: str
    prom_name: str
    source_file: Path
    lineno: int


# ---------------------------------------------------------------------------
# Declaration parsing (AST)
# ---------------------------------------------------------------------------


def _extract_declarations(src_file: Path) -> list[MetricDecl]:
    """Return all module-level Gauge/Counter/Histogram/Summary assignments."""
    try:
        tree = ast.parse(src_file.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        print(f"WARNING: could not parse {src_file}: {exc}", file=sys.stderr)
        return []

    decls: list[MetricDecl] = []
    for node in ast.walk(tree):
        # Only consider top-level (module body) assignments
        if not isinstance(node, ast.Assign):
            continue
        # RHS must be a simple Call
        if not isinstance(node.value, ast.Call):
            continue
        call = node.value
        # Constructor name: either Name(...) or Attribute(...).Name(...)
        func = call.func
        if isinstance(func, ast.Name):
            ctor_name = func.id
        elif isinstance(func, ast.Attribute):
            ctor_name = func.attr
        else:
            continue
        if ctor_name not in METRIC_CONSTRUCTORS:
            continue
        # LHS must be a single name target
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            continue
        var_name = node.targets[0].id
        # First positional arg is the Prometheus metric name string
        prom_name = var_name  # fallback
        if (
            call.args
            and isinstance(call.args[0], ast.Constant)
            and isinstance(call.args[0].value, str)
        ):
            prom_name = call.args[0].value
        decls.append(MetricDecl(var_name, prom_name, src_file, node.lineno))
    return decls


# ---------------------------------------------------------------------------
# Writer search (text scan — intentionally lenient)
# ---------------------------------------------------------------------------


def _find_writer_count(
    decl: MetricDecl,
    search_files: list[Path],
    decl_files: list[Path],
) -> int:
    """Count files that contain a writer or reference for decl.var_name.

    For non-declaration files: any occurrence of the variable name counts
    (including passing as a function argument).

    For declaration files themselves: only writer-method calls (.set / .inc /
    .observe / .labels) on lines OTHER than the declaration line count. This
    allows helper functions defined in metrics.py that wrap the metric to be
    detected, while avoiding the declaration itself being counted.
    """
    var_name = decl.var_name
    writer_re = re.compile(_WRITER_METHOD_RE.pattern.format(var=re.escape(var_name)))
    arg_re = re.compile(_ARG_PASS_RE.pattern.format(var=re.escape(var_name)))
    decl_file_set = {p.resolve() for p in decl_files}
    count = 0
    for path in search_files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if path.resolve() in decl_file_set:
            # In declaration file: only writer-method calls outside the
            # declaration line count (e.g. helper wrapper functions).
            lines = text.splitlines()
            for lineno_0, line in enumerate(lines, start=1):
                if lineno_0 == decl.lineno:
                    continue  # skip the declaration line itself
                if writer_re.search(line):
                    count += 1
                    break
        else:
            # In other files: any mention counts (lenient).
            if writer_re.search(text) or arg_re.search(text):
                count += 1
    return count


# ---------------------------------------------------------------------------
# CLI / main
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent


def _default_metrics_files() -> list[Path]:
    return [
        _REPO_ROOT / "yadgar" / "metrics.py",
        _REPO_ROOT / "yadgar" / "backend" / "embed_service_metrics.py",
    ]


def _default_search_dirs() -> list[Path]:
    return [_REPO_ROOT / "yadgar"]


def _default_exclude_files() -> list[Path]:
    return [
        _REPO_ROOT / "yadgar" / "metrics.py",
        _REPO_ROOT / "yadgar" / "backend" / "embed_service_metrics.py",
    ]


def _collect_search_files(
    search_dirs: list[Path], extra_files: list[Path] | None = None
) -> list[Path]:
    """Collect .py files under search_dirs, skipping tests/.

    extra_files: additional files (e.g. declaration files) to include in
    the search. They are not skipped by the test-directory filter.
    """
    seen: set[Path] = set()
    files: list[Path] = []

    def _add(p: Path) -> None:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            files.append(p)

    for d in search_dirs:
        for p in sorted(d.rglob("*.py")):
            # Skip test directories and test files
            if "tests" in p.parts or "test_" in p.name:
                continue
            _add(p)

    for p in extra_files or []:
        _add(p)

    return files


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="I23 — check that every declared Prometheus metric has ≥1 writer.",
    )
    parser.add_argument(
        "--list-all",
        action="store_true",
        help="Print every declared metric and its writer count, then exit 0.",
    )
    parser.add_argument(
        "--allowlist",
        metavar="VAR[,VAR...]",
        default="",
        help="Comma-separated variable names to exempt from the check.",
    )
    parser.add_argument(
        "--metrics-files",
        nargs="+",
        metavar="FILE",
        help="Override metric declaration files (default: yadgar/metrics.py + embed_service_metrics.py).",
    )
    parser.add_argument(
        "--search-dirs",
        nargs="+",
        metavar="DIR",
        help="Override directories to search for writer call sites.",
    )
    parser.add_argument(
        "--exclude-files",
        nargs="+",
        metavar="FILE",
        help="(Unused legacy flag — kept for backward compatibility.)",
    )
    return parser


def _load_declarations(metrics_files: list[Path]) -> list[MetricDecl]:
    """Parse metrics files and return all declared metrics."""
    decls: list[MetricDecl] = []
    for mf in metrics_files:
        if not mf.exists():
            print(f"WARNING: metrics file not found: {mf}", file=sys.stderr)
            continue
        decls.extend(_extract_declarations(mf))
    return decls


def _score_rows(
    all_decls: list[MetricDecl],
    search_files: list[Path],
    metrics_files: list[Path],
    allowlist: set[str],
) -> tuple[list[tuple[MetricDecl, int]], list[MetricDecl]]:
    """Return (all_rows, dead_decls) where dead = 0 writers and not allowlisted."""
    rows: list[tuple[MetricDecl, int]] = []
    dead: list[MetricDecl] = []
    for decl in all_decls:
        count = _find_writer_count(decl, search_files, decl_files=metrics_files)
        rows.append((decl, count))
        if count == 0 and decl.var_name not in allowlist:
            dead.append(decl)
    return rows, dead


def _print_list_all(rows: list[tuple[MetricDecl, int]], allowlist: set[str]) -> None:
    for decl, count in rows:
        if decl.var_name in allowlist:
            status = "ALLOWLISTED"
        elif count > 0:
            status = "OK"
        else:
            status = "DEAD"
        print(
            f"{decl.source_file.name}:{decl.lineno}: {decl.var_name}"
            f" ({decl.prom_name}) — {count} writer(s) [{status}]"
        )


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    allowlist: set[str] = {v.strip() for v in args.allowlist.split(",") if v.strip()}
    metrics_files = (
        [Path(f) for f in args.metrics_files] if args.metrics_files else _default_metrics_files()
    )
    search_dirs = (
        [Path(d) for d in args.search_dirs] if args.search_dirs else _default_search_dirs()
    )

    all_decls = _load_declarations(metrics_files)
    search_files = _collect_search_files(search_dirs, extra_files=metrics_files)
    rows, dead = _score_rows(all_decls, search_files, metrics_files, allowlist)

    if args.list_all:
        _print_list_all(rows, allowlist)
        return 0

    for decl in dead:
        print(
            f"{decl.source_file}:{decl.lineno}: {decl.var_name} ({decl.prom_name}) — no writers found"
        )
    return 1 if dead else 0


if __name__ == "__main__":
    sys.exit(main())
