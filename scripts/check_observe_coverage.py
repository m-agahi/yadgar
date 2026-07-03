#!/usr/bin/env python3
"""I33 — Observe-coverage lint (tri-signal observability ratchet).

AST-walks in-scope Python functions and classifies each as SATISFIED (has a span
source), auto-exempt (dunder / property / trivial), EXEMPT via
`.observe-allowlist.json`, or MISSING (a non-exempt function with no span source).

Modeled 1:1 on `scripts/check_trace_spans.py` (I24) + `.complexity-allowlist.json`
governance (I30). Stdlib-only.

Modes:
  --warn                 : print MISSING, exit 0 (baseline mode; P0 default).
  (no --warn)            : hard-fail on MISSING (exit 1). Per-area rollout flips.
  --area <name>          : restrict scan to files whose path contains <name>.

Allowlist integrity is ALWAYS hard (even in --warn mode): a stale entry (maps to
no current function), a rationale < 40 chars, or an invalid category → exit 1.

Usage:
  python scripts/check_observe_coverage.py --warn
  python scripts/check_observe_coverage.py --warn --root yadgar --allowlist-file .observe-allowlist.json
  python scripts/check_observe_coverage.py --list-all --warn
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Decorators that count as a span source (satisfy coverage).
_SPAN_DECORATORS = {"trace_span", "observe", "_tool"}

# Exemption categories permitted in .observe-allowlist.json.
_ALLOWED_CATEGORIES = {
    "hot-loop",
    "generated",
    "trivial",
    "property",
    "dunder",
    "framework-instrumented",
    "test",
    "pre-existing",
}

_MIN_RATIONALE_CHARS = 40

# Conservative I/O-sink names — presence disqualifies a fn from "trivial".
_IO_SINK_HINTS = {
    "post",
    "get",
    "request",
    "execute",
    "query",
    "open",
    "write",
    "read",
    "run",
    "check_output",
    "call",
    "encode",
    "insert",
    "connect",
    "commit",
    "info",
    "debug",
    "warning",
    "error",
    "critical",
    "exception",
}


@dataclass
class Finding:
    qualname: str
    lineno: int
    status: str  # SATISFIED | EXEMPT_DUNDER | EXEMPT_PROPERTY | EXEMPT_TRIVIAL | EXEMPT_ALLOWLIST | MISSING


# ── classification helpers ───────────────────────────────────────────────────


def _decorator_name(dec: ast.expr) -> str | None:
    node = dec.func if isinstance(dec, ast.Call) else dec
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _has_span_decorator(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(_decorator_name(d) in _SPAN_DECORATORS for d in node.decorator_list)


def _is_property(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for d in node.decorator_list:
        n = _decorator_name(d)
        if n in {"property", "cached_property", "setter", "getter", "deleter"}:
            return True
    return False


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def _is_trivial(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """≤3 statements, no control-flow, no raise/await, no I/O-sink call.

    Deliberately strict — favours REQUIRING @observe over silently exempting.
    """
    body = [
        s for s in node.body if not isinstance(s, ast.Expr) or not isinstance(s.value, ast.Constant)
    ]
    # (drop a leading docstring)
    if len(body) > 3:
        return False
    for sub in ast.walk(node):
        if isinstance(
            sub,
            (ast.If, ast.For, ast.While, ast.With, ast.AsyncWith, ast.Try, ast.Raise, ast.Await),
        ):
            return False
        if isinstance(sub, ast.Call):
            fn_name = (
                _decorator_name(sub.func)
                if isinstance(sub.func, (ast.Name, ast.Attribute))
                else None
            )
            if fn_name in _IO_SINK_HINTS:
                return False
    return True


def _qualname(stack: list[str], name: str) -> str:
    return ".".join([*stack, name])


# ── file scan ────────────────────────────────────────────────────────────────


def scan_file(path: Path, allowlist: dict) -> list[Finding]:
    """Classify every function in a file. `allowlist` maps 'module:qualname' -> entry."""
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (SyntaxError, OSError) as exc:  # pragma: no cover
        print(f"WARNING: could not parse {path}: {exc}", file=sys.stderr)
        return []

    module = path.stem
    findings: list[Finding] = []

    def visit(node: ast.AST, stack: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qn = _qualname(stack, child.name)
                fq = f"{module}:{qn}"
                status = _classify(child, fq, allowlist)
                findings.append(Finding(qualname=qn, lineno=child.lineno, status=status))
                visit(child, [*stack, child.name])
            elif isinstance(child, ast.ClassDef):
                visit(child, [*stack, child.name])
            else:
                visit(child, stack)

    visit(tree, [])
    return findings


def _classify(node, fq: str, allowlist: dict) -> str:
    if _is_dunder(node.name):
        return "EXEMPT_DUNDER"
    if _is_property(node):
        return "EXEMPT_PROPERTY"
    if _has_span_decorator(node):
        return "SATISFIED"
    if fq in allowlist:
        return "EXEMPT_ALLOWLIST"
    if _is_trivial(node):
        return "EXEMPT_TRIVIAL"
    return "MISSING"


# ── allowlist governance (always hard) ───────────────────────────────────────


def validate_allowlist_entry(fq: str, entry: dict) -> list[str]:
    errs: list[str] = []
    if not isinstance(entry, dict):
        return [f"{fq}: entry must be an object"]
    cat = entry.get("category")
    if cat not in _ALLOWED_CATEGORIES:
        errs.append(f"{fq}: invalid category {cat!r} (allowed: {sorted(_ALLOWED_CATEGORIES)})")
    rationale = entry.get("rationale", "")
    if not isinstance(rationale, str) or len(rationale.strip()) < _MIN_RATIONALE_CHARS:
        errs.append(f"{fq}: rationale must be >= {_MIN_RATIONALE_CHARS} chars")
    return errs


def _iter_py_files(root: Path, area: str | None) -> list[Path]:
    files = []
    for p in sorted(root.rglob("*.py")):
        rel = str(p)
        if "/tests/" in rel or rel.endswith("_test.py") or p.name.startswith("test_"):
            continue
        if area and area not in rel:
            continue
        files.append(p)
    return files


def run(root: Path, allowlist: dict, area: str | None) -> tuple[list[Finding], set[str], list[str]]:
    """Return (all_findings, seen_fqs, integrity_errors)."""
    all_findings: list[Finding] = []
    seen_fqs: set[str] = set()
    for path in _iter_py_files(root, area):
        module = path.stem
        for f in scan_file(path, allowlist):
            all_findings.append(f)
            seen_fqs.add(f"{module}:{f.qualname}")

    integrity: list[str] = []
    for fq, entry in allowlist.items():
        if fq.startswith("_"):
            continue  # metadata keys (e.g. "_comment") are not allowlist entries
        integrity.extend(validate_allowlist_entry(fq, entry))
        if fq not in seen_fqs:
            integrity.append(f"{fq}: STALE allowlist entry — no such function in scope")
    return all_findings, seen_fqs, integrity


# ── CLI ──────────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="I33 — observe-coverage lint (tri-signal ratchet).")
    p.add_argument("--warn", action="store_true", help="Warn-mode: MISSING never blocks (exit 0).")
    p.add_argument("--area", default=None, help="Restrict scan to paths containing this substring.")
    p.add_argument("--root", default=str(_REPO_ROOT / "yadgar"), help="Root dir to scan.")
    p.add_argument(
        "--allowlist-file",
        default=str(_REPO_ROOT / ".observe-allowlist.json"),
        help="Path to .observe-allowlist.json.",
    )
    p.add_argument("--list-all", action="store_true", help="Print every function + status, exit 0.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    root = Path(args.root)

    allowlist: dict = {}
    alf = Path(args.allowlist_file)
    if alf.exists():
        try:
            allowlist = json.loads(alf.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"ERROR: {alf} is not valid JSON: {exc}", file=sys.stderr)
            return 1

    findings, _seen, integrity = run(root, allowlist, args.area)

    if args.list_all:
        for f in findings:
            print(f"{f.qualname}:{f.lineno} [{f.status}]")

    missing = [f for f in findings if f.status == "MISSING"]

    # Allowlist integrity is ALWAYS hard.
    if integrity:
        print("I33 allowlist integrity FAILURES:", file=sys.stderr)
        for e in integrity:
            print(f"  {e}", file=sys.stderr)
        return 1

    if missing:
        mode = "WARN" if args.warn else "FAIL"
        print(
            f"I33 [{mode}] — {len(missing)} function(s) missing tri-signal coverage "
            f"(no span source, not exempt, not allowlisted):",
            file=sys.stderr,
        )
        for f in missing[:50]:
            print(f"  {f.qualname}:{f.lineno}", file=sys.stderr)
        if len(missing) > 50:
            print(f"  ... and {len(missing) - 50} more", file=sys.stderr)
        if not args.warn:
            return 1

    if not missing and not integrity:
        print("I33 OK — all in-scope functions classified (SATISFIED or exempt).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
