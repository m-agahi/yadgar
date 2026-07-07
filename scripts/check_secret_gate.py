#!/usr/bin/env python3
"""I26 — Secret-gate invariant: every write tool must call gate_or_reject().

Scans all @_tool()-decorated functions in yadgar/server/tools/*.py.
For each function that has a `content: str` parameter (or known write-param
names), asserts that the function body either:
  1. Calls gate_or_reject( directly
  2. Calls check_secrets( directly
  3. Has a # secret-gate: skip annotation (non-write tools)
  4. Is NOT a write tool (no content/tags/current_task params)

Exit codes:
  0  all write tools are gated
  1  one or more write tools are missing gate

Usage:
  python scripts/check_secret_gate.py                        # check repo
  python scripts/check_secret_gate.py --tools-dir <path>    # check specific dir
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

# Parameter names that indicate a write tool
_WRITE_PARAM_NAMES: frozenset[str] = frozenset(
    {
        "content",
        "current_task",
        "custom_context",
        "key_decisions",
        "next_steps",
        "open_questions",
        "active_errors",
    }
)

# Functions explicitly exempted (non-write tools that happen to have content-ish params
# or delegate write security to a sub-call that is itself gated)
_SKIP_ANNOTATION = "# secret-gate: skip"

# Functions known to delegate to memorize() which already has its own gate
_DELEGATING_TOOLS: frozenset[str] = frozenset(
    {
        "seed_project",  # delegates to _seed() which calls memorize()
        "wiki_approve",  # reads a draft, calls wiki store directly (no free-text input)
    }
)


def _has_tool_decorator(node: ast.FunctionDef) -> bool:
    """Return True if the function has a @_tool() decorator."""
    for dec in node.decorator_list:
        if isinstance(dec, ast.Call):
            func = dec.func
            if isinstance(func, ast.Name) and func.id == "_tool":
                return True
            if isinstance(func, ast.Attribute) and func.attr == "_tool":
                return True
        elif isinstance(dec, ast.Name) and dec.id == "_tool":
            return True
        elif isinstance(dec, ast.Attribute) and dec.attr == "_tool":
            return True
    return False


def _get_param_names(node: ast.FunctionDef) -> set[str]:
    """Return the set of parameter names for a function."""
    names: set[str] = set()
    for arg in node.args.args + node.args.kwonlyargs:
        names.add(arg.arg)
    if node.args.vararg:
        names.add(node.args.vararg.arg)
    if node.args.kwarg:
        names.add(node.args.kwarg.arg)
    return names


def _is_write_tool(node: ast.FunctionDef) -> bool:
    """Return True if the function has write-relevant parameter names."""
    params = _get_param_names(node)
    return bool(params & _WRITE_PARAM_NAMES)


def _body_calls_gate(node: ast.FunctionDef, source_lines: list[str]) -> bool:
    """Return True if the function body contains gate_or_reject( or check_secrets(."""
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Name) and func.id in ("gate_or_reject", "check_secrets"):
                return True
            if isinstance(func, ast.Attribute) and func.attr in (
                "gate_or_reject",
                "check_secrets",
            ):
                return True
    return False


def _has_skip_annotation(node: ast.FunctionDef, source_lines: list[str]) -> bool:
    """Return True if the function has a # secret-gate: skip comment in its body."""
    # Check the lines comprising the function body for the annotation
    start = node.lineno
    end = node.end_lineno or start
    for lineno in range(start - 1, min(end, len(source_lines))):
        if _SKIP_ANNOTATION in source_lines[lineno]:
            return True
    return False


def check_file(filepath: Path) -> list[str]:
    """Check a single file. Returns list of violation messages."""
    violations: list[str] = []
    try:
        source = filepath.read_text(encoding="utf-8")
        source_lines = source.splitlines()
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError as exc:
        return [f"SYNTAX_ERROR {filepath}: {exc}"]

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if not _has_tool_decorator(node):
            continue
        if node.name in _DELEGATING_TOOLS:
            continue
        if not _is_write_tool(node):
            continue
        if _has_skip_annotation(node, source_lines):
            continue
        if _body_calls_gate(node, source_lines):
            continue
        violations.append(
            f"{filepath.name}:{node.lineno}: {node.name} — "
            f"write tool missing gate_or_reject() call "
            f"(params: {sorted(_get_param_names(node) & _WRITE_PARAM_NAMES)})"
        )
    return violations


def main(argv: list[str] | None = None) -> int:
    """Run I26 check. Returns 0 if clean, 1 if violations found."""
    import argparse

    parser = argparse.ArgumentParser(description="I26 secret-gate invariant check")
    parser.add_argument(
        "--tools-dir",
        default=None,
        help="Directory containing tool .py files (default: yadgar/server/tools/)",
    )
    args = parser.parse_args(argv)

    if args.tools_dir:
        tools_dir = Path(args.tools_dir)
    else:
        # Find repo root relative to this script
        script_dir = Path(__file__).parent
        repo_root = script_dir.parent
        tools_dir = repo_root / "yadgar" / "core" / "server" / "tools"

    if not tools_dir.exists():
        print(f"ERROR: tools directory not found: {tools_dir}", file=sys.stderr)
        return 1

    all_violations: list[str] = []
    for py_file in sorted(tools_dir.glob("*.py")):
        if py_file.name.startswith("_") or py_file.name == "__init__.py":
            continue
        violations = check_file(py_file)
        all_violations.extend(violations)

    if all_violations:
        print("I26 VIOLATIONS — write tools missing secret gate:", file=sys.stderr)
        for v in all_violations:
            print(f"  {v}", file=sys.stderr)
        print(
            f"\n{len(all_violations)} violation(s) found. "
            "Add gate_or_reject() before any state mutation, "
            "or add '# secret-gate: skip' with justification.",
            file=sys.stderr,
        )
        return 1

    print(f"I26 OK — all write tools gated ({tools_dir})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
