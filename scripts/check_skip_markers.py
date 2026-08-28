#!/usr/bin/env python3
"""Commit-time skip-marker gate (ADR-0087): new skip markers need an inventory entry.

Pre-commit hook (pass_filenames: true, files: ^yadgar/tests/.*\\.py$).
For each STAGED test file, statically finds skip/skipif MARKER decorators,
``pytestmark`` skip assignments, module-level ``pytest.skip(...,
allow_module_level=True)`` calls, and ``pytest.importorskip(...)`` calls whose
lines were ADDED in the staged diff. Any such NEW marker whose reason does not
match a sanctioned entry in yadgar/tests/skip_inventory.json fails the commit.

``pytest.importorskip`` joined the scan in ledger task 411. Without it this
gate's "Passed" was not evidence a new importorskip had been vetted — and
importorskip is how most of this repo's optional-extra skips are written, so
the inventory's ``sanctioned_when_module_absent`` entries were already keyed
on reasons this scan could not see. It carries no positional reason: the
reason is the ``reason=`` keyword or nothing (``args[0]`` is the module name).

Deliberately NOT scanned (CI -rs gate territory, per ADR-0087 consequences):
dynamic ``pytest.skip()`` calls inside test/helper bodies — the static scan
must not false-positive on runtime conditions. ``importorskip`` inside a body
IS scanned, and the line is where the two differ: it branches on a module
being importable, which is a property of the environment the inventory
already reasons about, not on a runtime condition the scan cannot see.

Exit 0 → no new unsanctioned markers.
Exit 1 → offender list printed (file:line + reason).
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_skip_inventory import _is_sanctioned, _load_inventory  # noqa: E402

# ---------------------------------------------------------------------------
# AST extraction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Marker:
    lineno: int
    end_lineno: int
    kind: str  # "skip" | "skipif" | "module-level-skip" | "importorskip"
    reason: str | None


@dataclass(frozen=True)
class Violation:
    lineno: int
    reason: str | None
    message: str


def _literal_text(node: ast.expr | None) -> str | None:
    """Extract literal string content from a Constant or JoinedStr (f-string)."""
    if node is None:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = [
            v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str)
        ]
        return "".join(parts) if parts else None
    return None


def _mark_attr_name(node: ast.expr) -> str | None:
    """Return 'skip'/'skipif' if node is pytest.mark.skip / pytest.mark.skipif."""
    if not isinstance(node, ast.Attribute) or node.attr not in ("skip", "skipif"):
        return None
    mark = node.value
    if isinstance(mark, ast.Attribute) and mark.attr == "mark":
        return node.attr
    return None


def _marker_from_expr(node: ast.expr) -> Marker | None:
    """Build a Marker from a decorator / pytestmark expression, if it is a skip mark."""
    if isinstance(node, ast.Call):
        kind = _mark_attr_name(node.func)
        if kind is None:
            return None
        reason = None
        for kw in node.keywords:
            if kw.arg == "reason":
                reason = _literal_text(kw.value)
        if reason is None and kind == "skip" and node.args:
            reason = _literal_text(node.args[0])
        return Marker(node.lineno, node.end_lineno or node.lineno, kind, reason)
    kind = _mark_attr_name(node)
    if kind is not None:
        return Marker(node.lineno, node.end_lineno or node.lineno, kind, None)
    return None


def _module_level_skip(node: ast.Call) -> Marker | None:
    """Match pytest.skip(..., allow_module_level=True) anywhere in the tree."""
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr == "skip"):
        return None
    if not any(
        kw.arg == "allow_module_level"
        and isinstance(kw.value, ast.Constant)
        and kw.value.value is True
        for kw in node.keywords
    ):
        return None
    reason = None
    for kw in node.keywords:
        if kw.arg == "reason":
            reason = _literal_text(kw.value)
    if reason is None and node.args:
        reason = _literal_text(node.args[0])
    return Marker(node.lineno, node.end_lineno or node.lineno, "module-level-skip", reason)


def _importorskip(node: ast.Call) -> Marker | None:
    """Match ``pytest.importorskip(...)`` anywhere in the tree.

    Task 411: this shape was not scanned at all, so "check-skip-markers: OK"
    was not evidence a newly added importorskip had been vetted — and
    importorskip is how most of this repo's optional-extra skips are actually
    written (``pytest.importorskip("sqlalchemy", reason="sqlalchemy not
    installed (sql extra)")``), which is why the inventory already carries
    ``sanctioned_when_module_absent`` entries keyed on exactly those reasons.

    Scanned even inside a function body, unlike a dynamic ``pytest.skip()``.
    The distinction is not where the call sits, it is what it branches on:
    importorskip skips on a MODULE being importable — a property of the
    environment the inventory already reasons about — while a bare
    ``pytest.skip()`` skips on a runtime condition the static scan cannot see,
    which is why it stays CI ``-rs`` gate territory.

    The reason is the ``reason=`` KEYWORD only. ``args[0]`` is the module
    name, so the positional fallback ``_marker_from_expr`` uses for
    ``pytest.mark.skip("...")`` must not be reused here — it would report
    ``"surrealdb"`` as an unsanctioned reason rather than the truth, which is
    that no reason was written down.
    """
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr == "importorskip"):
        return None
    reason = None
    for kw in node.keywords:
        if kw.arg == "reason":
            reason = _literal_text(kw.value)
    return Marker(node.lineno, node.end_lineno or node.lineno, "importorskip", reason)


def find_skip_markers(source: str) -> list[Marker]:
    """Return all skip/skipif markers + module-level skips in *source*.

    Never raises: unparseable source returns [] (ruff/pytest will surface the
    syntax error; this gate only cares about markers).
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    markers: list[Marker] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for dec in node.decorator_list:
                m = _marker_from_expr(dec)
                if m:
                    markers.append(m)
        elif isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets):
                values = (
                    node.value.elts
                    if isinstance(node.value, (ast.List, ast.Tuple))
                    else [node.value]
                )
                for v in values:
                    m = _marker_from_expr(v)
                    if m:
                        markers.append(m)
        elif isinstance(node, ast.Call):
            m = _module_level_skip(node) or _importorskip(node)
            if m:
                markers.append(m)
    markers.sort(key=lambda m: m.lineno)
    return markers


# ---------------------------------------------------------------------------
# Diff parsing
# ---------------------------------------------------------------------------

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def added_lines_from_diff(diff_text: str) -> set[int]:
    """Parse unified-diff (-U0) hunk headers into the set of added line numbers."""
    added: set[int] = set()
    for line in diff_text.splitlines():
        m = _HUNK_RE.match(line)
        if m:
            start = int(m.group(1))
            count = int(m.group(2)) if m.group(2) is not None else 1
            added.update(range(start, start + count))
    return added


# ---------------------------------------------------------------------------
# Gate core
# ---------------------------------------------------------------------------


def find_new_unsanctioned(
    source: str, added_lines: set[int], patterns: list[str]
) -> list[Violation]:
    """Return violations for NEW (added-line) markers with missing/unsanctioned reasons."""
    violations: list[Violation] = []
    for marker in find_skip_markers(source):
        if not added_lines.intersection(range(marker.lineno, marker.end_lineno + 1)):
            continue  # pre-existing marker — CI -rs gate territory
        if marker.reason is None:
            violations.append(
                Violation(
                    marker.lineno,
                    None,
                    f"new {marker.kind} marker has no literal reason= string — "
                    "a sanctioned inventory reason is required",
                )
            )
        elif not _is_sanctioned(marker.reason, patterns):
            violations.append(
                Violation(
                    marker.lineno,
                    marker.reason,
                    f"new {marker.kind} marker reason {marker.reason!r} matches no "
                    "entry in yadgar/tests/skip_inventory.json",
                )
            )
    return violations


def _staged_content(path: str) -> str | None:
    result = subprocess.run(
        ["git", "show", f":{path}"], capture_output=True, text=True, check=False
    )
    return result.stdout if result.returncode == 0 else None


def _staged_diff(path: str) -> str:
    result = subprocess.run(
        ["git", "diff", "--cached", "-U0", "--", path],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout


def main() -> int:
    files = [f for f in sys.argv[1:] if f.endswith(".py")]
    if not files:
        print("check-skip-markers: OK — no test files to scan")
        return 0
    patterns = _load_inventory()
    all_violations: list[str] = []
    for path in files:
        source = _staged_content(path)
        if source is None:
            continue  # deleted / not staged
        added = added_lines_from_diff(_staged_diff(path))
        for v in find_new_unsanctioned(source, added, patterns):
            all_violations.append(f"  {path}:{v.lineno}: {v.message}")
    if all_violations:
        print(
            "check-skip-markers: ERROR — new skip marker(s) without a sanctioned "
            "inventory reason (ADR-0087):",
            file=sys.stderr,
        )
        for v in all_violations:
            print(v, file=sys.stderr)
        print(
            "Add an entry to yadgar/tests/skip_inventory.json (with a >=40-char "
            "note) or fix the gate condition so the test runs.",
            file=sys.stderr,
        )
        return 1
    print(f"check-skip-markers: OK — {len(files)} file(s), no new unsanctioned markers")
    return 0


if __name__ == "__main__":
    sys.exit(main())
