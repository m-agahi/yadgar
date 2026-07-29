#!/usr/bin/env python3
"""Layer 3 tamper-protection lint (task #52): assertion-presence check for e2e tests.

Every ``def test_*`` (top-level or method) in the e2e scan set MUST contain at
least one real assertion.  Recognised forms:

  - ``assert`` statement
  - ``with pytest.raises(...)`` or ``with pytest.warns(...)``
  - a Call whose final name starts with ``assert`` (covers ``self.assertEqual``,
    ``self.assertIn``, project helpers like ``_assert_not_real_data_dir``, etc.)

Escape hatch: add ``# tamper-lint: no-assert <reason>`` (anywhere in the test
function body's source lines) to acknowledge that a test legitimately has no
assertion (e.g. tests that assert only via side-effects at the fixture level).

SCAN SET (widened 2026-07-29 — gate-blindness class)
----------------------------------------------------
    yadgar/tests/e2e/**/*.py  ∪  yadgar/tests/**/*e2e*.py

The lint originally pinned its scan root to ``yadgar/tests/e2e/`` alone while
six ``*e2e*`` modules lived outside it (``tests/core/``, ``tests/integration/``,
``tests/scripts/``).  Scan-scope was narrower than the artifact class the lint
claims to cover, so those modules were never assertion-linted — the lint was
green because it was not looking, not because there was nothing to see.

``scripts/check_test_weakening.py`` carries the SAME scope INDEPENDENTLY, as a
regex over ``diff --git`` lines.  The two must be widened in lockstep; a test in
``yadgar/tests/core/test_tamper_guards.py`` asserts their agreement mechanically
so silent drift fails CI rather than re-creating this defect.

Run as a plain (non-e2e) pytest: ``yadgar/tests/core/test_tamper_guards.py``.
Or standalone: ``python scripts/check_e2e_assertions.py`` (exit 0 = clean).
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_E2E_DIR = _REPO_ROOT / "yadgar" / "tests" / "e2e"
_TESTS_DIR = _REPO_ROOT / "yadgar" / "tests"

_ESCAPE_COMMENT = re.compile(r"#\s*tamper-lint:\s*no-assert\b")
_ASSERT_CALL_RE = re.compile(r"^assert")
_SKIP_DECORATOR_NAMES = frozenset({"skip", "skipif", "xfail"})


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _decorator_name(node: ast.expr) -> str | None:
    """Return the bare name/attribute of a decorator expression."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return _decorator_name(node.func)
    return None


def _is_skip_decorated(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Return True if the function is decorated with skip/skipif/xfail."""
    return any(
        _decorator_name(d) in _SKIP_DECORATOR_NAMES for d in getattr(fn, "decorator_list", [])
    )


def _call_name(node: ast.expr) -> str | None:
    """Return the final attribute/name of a call, e.g. 'assertIn' for self.assertIn."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _has_real_assertion(fn: ast.FunctionDef | ast.AsyncFunctionDef, src_lines: list[str]) -> bool:
    """Return True if *fn* contains at least one recognised assertion form."""
    # 1. Escape-hatch comment anywhere in the function source span. Scan from the
    # `def` line (NOT fn.body[0]) so a `# tamper-lint: no-assert` comment placed on
    # the first body line — above the first statement, where comments commonly sit —
    # is still seen (comments are not AST nodes, so body[0].lineno would skip it).
    # ast line numbers are 1-based; src_lines is 0-based.
    start = fn.lineno - 1  # def line (0-based)
    end = fn.end_lineno  # last line inclusive (1-based → exclusive after -1+1 = as-is)
    body_lines = src_lines[start:end]
    if any(_ESCAPE_COMMENT.search(line) for line in body_lines):
        return True

    # 2. Walk the function AST subtree.
    for node in ast.walk(fn):
        if isinstance(node, ast.Assert):
            return True
        if isinstance(node, ast.With):
            for item in node.items:
                ctx = item.context_expr
                if isinstance(ctx, ast.Call):
                    name = _call_name(ctx.func) if hasattr(ctx, "func") else None
                    if name in {"raises", "warns"}:
                        return True
        if isinstance(node, ast.Call):
            name = _call_name(node.func)
            if name and _ASSERT_CALL_RE.match(name):
                return True
    return False


# ---------------------------------------------------------------------------
# Main lint
# ---------------------------------------------------------------------------


def lint_file(path: Path) -> list[str]:
    """Return a list of violation strings for a single Python file."""
    src = path.read_text(encoding="utf-8")
    src_lines = src.splitlines()
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError as exc:
        return [f"{path}: SyntaxError — {exc}"]

    violations: list[str] = []
    # Display path relative to repo when possible; fall back to the raw path for
    # files outside the repo (e.g. tmp fixtures in the guard's own meta-tests).
    try:
        disp: Path = path.relative_to(_REPO_ROOT)
    except ValueError:
        disp = path
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test_"):
            continue
        # Skip-decorated functions are intentionally non-running; they are
        # exempt from the assertion requirement.
        if _is_skip_decorated(node):
            continue
        if not _has_real_assertion(node, src_lines):
            violations.append(
                f"{disp}:{node.lineno}: "
                f"{node.name!r} has no assertions "
                "(add assert/pytest.raises/pytest.warns or "
                "'# tamper-lint: no-assert <reason>' to acknowledge)"
            )
    return violations


def lint_dir(e2e_dir: Path = _E2E_DIR) -> list[str]:
    """Lint all .py files under *e2e_dir*. Returns list of violation strings.

    Kept as the narrow single-root entry point (public API — the guard's own
    meta-tests call it).  ``lint_scope()`` is what ``main()`` runs.
    """
    violations: list[str] = []
    for py in sorted(e2e_dir.rglob("*.py")):
        violations.extend(lint_file(py))
    return violations


def scan_paths(tests_dir: Path = _TESTS_DIR) -> list[Path]:
    """Return the full e2e scan set: tests/e2e/**/*.py ∪ tests/**/*e2e*.py.

    Deduplicated and sorted, so a module inside ``tests/e2e/`` whose name also
    contains ``e2e`` is linted once.
    """
    e2e_dir = tests_dir / "e2e"
    found: set[Path] = set()
    if e2e_dir.is_dir():
        found.update(e2e_dir.rglob("*.py"))
    if tests_dir.is_dir():
        found.update(tests_dir.rglob("*e2e*.py"))
    return sorted(found)


def lint_scope(tests_dir: Path = _TESTS_DIR) -> list[str]:
    """Lint every module in the widened e2e scan set."""
    violations: list[str] = []
    for py in scan_paths(tests_dir):
        violations.extend(lint_file(py))
    return violations


def main() -> int:
    if not _TESTS_DIR.is_dir():
        print(f"ERROR: tests directory not found: {_TESTS_DIR}", file=sys.stderr)
        return 1
    violations = lint_scope()
    if violations:
        print("e2e assertion-presence lint FAILED:", file=sys.stderr)
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        return 1
    print("e2e assertion-presence lint OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
