"""Lint guard: detect O(N²) per-pair get_relationship_between anti-pattern.

Walks the AST of every Python source file in yadgar/ and fails if
get_relationship_between() is called anywhere outside storage.py.

The method is an O(N²) anti-pattern: one HTTP roundtrip per pair.
All call sites must use get_relationships_among_entities() + dict lookup instead.
This rule is intentionally strict: even calls inside helper methods (not directly
in a for-loop body) are flagged, because those helpers are frequently called from
loops, causing the same O(N²) problem at one level of indirection.

This would have caught the bugs fixed in v4.4.8 (memify), v4.4.10
(process_episodes, structural_novelty, detect_communities, detect_gaps),
and v4.4.11 (cls_store._create_derived_link, sleep_compute._memories_connected)
at code-review time rather than requiring a live production trace.
"""

import ast
from pathlib import Path

import pytest

# Files to exclude from the check:
_SKIP = {
    # Definition of the method itself
    "storage.py",
    # This test file
    "test_no_per_pair_anti_pattern.py",
}

# Directories to exclude (relative to the yadgar/ package root):
_SKIP_DIRS = {
    # Test files legitimately call get_relationship_between() as an assertion helper
    # on storage objects — not an O(N²) anti-pattern.
    "tests",
}


def _yadgar_source_files() -> list[Path]:
    root = Path(__file__).parent.parent  # yadgar/ package root
    return [
        p
        for p in root.rglob("*.py")
        if p.name not in _SKIP
        # Exclude hidden directories relative to the package root only (e.g. .git, __pycache__).
        # Filtering on absolute path parts would misfire when the repo lives under a dotted
        # directory such as .claude/worktrees/…
        and not any(part.startswith(".") for part in p.relative_to(root).parts)
        # Exclude test directories — they legitimately call get_relationship_between()
        # as an assertion helper on storage objects, not as an O(N²) anti-pattern.
        and not any(part in _SKIP_DIRS for part in p.relative_to(root).parts)
    ]


def _get_rel_between_calls(source: str) -> list[int]:
    """Return line numbers of ALL get_relationship_between calls in the source."""
    tree = ast.parse(source)
    violations: list[int] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get_relationship_between"
        ):
            violations.append(node.lineno)
    return violations


def test_no_get_relationship_between_outside_storage():
    """Fail if any non-storage source file calls get_relationship_between() at all.

    This is an O(N²) anti-pattern: one HTTP roundtrip per pair.
    Use get_relationships_among_entities() + dict lookup instead.

    The rule is intentionally global (not just for-loop bodies): helper methods
    called from loops cause the same quadratic problem at one level of indirection,
    and were missed by the original narrower for-loop-only guard.

    Fixed in:
    - v4.4.8: curation.py
    - v4.4.10: consolidation.py, predictive_coding.py, sleep_compute.detect_communities,
               metacognition.detect_gaps
    - v4.4.11: cls_store._create_derived_link, sleep_compute._memories_connected
    """
    violations: list[str] = []
    for path in _yadgar_source_files():
        try:
            source = path.read_text()
        except OSError:
            continue
        lines = _get_rel_between_calls(source)
        for lineno in lines:
            violations.append(f"{path}:{lineno}")

    if violations:
        formatted = "\n  ".join(violations)
        pytest.fail(
            f"get_relationship_between() called outside storage.py — O(N²) anti-pattern.\n"
            f"Use get_relationships_among_entities() + dict lookup instead.\n"
            f"Violations:\n  {formatted}"
        )
