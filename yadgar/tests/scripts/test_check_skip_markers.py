"""TDD tests for scripts/check_skip_markers.py — commit-time skip-marker gate (ADR-0087).

Scope (per ADR-0087 consequences):
  - Scans skip/skipif MARKER decorators + module-level skips in STAGED test files.
  - Only NEW markers (lines added in the staged diff) are gated.
  - Dynamic pytest.skip() calls inside test bodies are NOT scanned (CI -rs gate
    territory) — the static scan must not false-positive on them.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent / "scripts"))

from check_skip_markers import (
    added_lines_from_diff,
    find_new_unsanctioned,
    find_skip_markers,
)

# ---------------------------------------------------------------------------
# find_skip_markers — AST extraction
# ---------------------------------------------------------------------------


class TestFindSkipMarkers:
    def test_no_markers(self):
        src = "def test_ok():\n    assert True\n"
        assert find_skip_markers(src) == []

    def test_skip_decorator_with_reason(self):
        src = (
            "import pytest\n"
            "\n"
            '@pytest.mark.skip(reason="requires macOS host")\n'
            "def test_mac():\n"
            "    pass\n"
        )
        markers = find_skip_markers(src)
        assert len(markers) == 1
        assert markers[0].reason == "requires macOS host"
        assert markers[0].lineno == 3

    def test_skip_decorator_positional_reason(self):
        src = (
            'import pytest\n@pytest.mark.skip("duckdb not installed")\ndef test_duck():\n    pass\n'
        )
        markers = find_skip_markers(src)
        assert len(markers) == 1
        assert markers[0].reason == "duckdb not installed"

    def test_skipif_decorator_reason_kwarg(self):
        src = (
            "import pytest, sys\n"
            "@pytest.mark.skipif(sys.platform != 'darwin', reason='requires macOS host')\n"
            "def test_mac():\n"
            "    pass\n"
        )
        markers = find_skip_markers(src)
        assert len(markers) == 1
        assert markers[0].reason == "requires macOS host"

    def test_skip_decorator_no_reason(self):
        src = "import pytest\n@pytest.mark.skip\ndef test_x():\n    pass\n"
        markers = find_skip_markers(src)
        assert len(markers) == 1
        assert markers[0].reason is None

    def test_skip_decorator_call_no_args(self):
        src = "import pytest\n@pytest.mark.skip()\ndef test_x():\n    pass\n"
        markers = find_skip_markers(src)
        assert len(markers) == 1
        assert markers[0].reason is None

    def test_skipif_no_reason(self):
        src = "import pytest\n@pytest.mark.skipif(True)\ndef test_x():\n    pass\n"
        markers = find_skip_markers(src)
        assert len(markers) == 1
        assert markers[0].reason is None

    def test_class_level_decorator(self):
        src = (
            "import pytest\n"
            '@pytest.mark.skip(reason="nix not in PATH")\n'
            "class TestNix:\n"
            "    def test_a(self):\n"
            "        pass\n"
        )
        markers = find_skip_markers(src)
        assert len(markers) == 1
        assert markers[0].reason == "nix not in PATH"

    def test_pytestmark_assignment(self):
        src = 'import pytest\npytestmark = pytest.mark.skip(reason="requires macOS host")\n'
        markers = find_skip_markers(src)
        assert len(markers) == 1
        assert markers[0].reason == "requires macOS host"

    def test_pytestmark_list(self):
        src = (
            "import pytest\n"
            "pytestmark = [pytest.mark.xdist_group('g'), "
            'pytest.mark.skipif(True, reason="shellcheck not in PATH")]\n'
        )
        markers = find_skip_markers(src)
        assert len(markers) == 1
        assert markers[0].reason == "shellcheck not in PATH"

    def test_module_level_skip_call(self):
        src = (
            "import pytest\n"
            "if True:\n"
            '    pytest.skip("cyclonedx-bom not installed", allow_module_level=True)\n'
        )
        markers = find_skip_markers(src)
        assert len(markers) == 1
        assert markers[0].reason == "cyclonedx-bom not installed"

    def test_dynamic_skip_in_body_not_scanned(self):
        """pytest.skip() inside a test body (no allow_module_level) is CI-gate territory."""
        src = 'import pytest\ndef test_x():\n    pytest.skip("runtime condition not met at all")\n'
        assert find_skip_markers(src) == []

    def test_fstring_reason_constant_parts(self):
        src = (
            "import pytest\n"
            "V = 1\n"
            '@pytest.mark.skipif(True, reason=f"graphviz `dot` not installed v{V}")\n'
            "def test_x():\n"
            "    pass\n"
        )
        markers = find_skip_markers(src)
        assert len(markers) == 1
        assert "graphviz `dot` not installed" in markers[0].reason

    def test_syntax_error_returns_empty(self):
        assert find_skip_markers("def broken(:\n") == []


class TestImportOrSkipMarkers:
    """Task 411 — ``pytest.importorskip`` silences a module exactly like a marker.

    The scan covered marker decorators, ``pytestmark``, and module-level
    ``pytest.skip(allow_module_level=True)``. It did NOT cover
    ``pytest.importorskip``, which is how 40-odd of this repo's skips are
    actually expressed (``yadgar/tests/_shared/test_mariadb_engine.py:23``
    and friends) — so "check-skip-markers: OK" was not evidence a newly added
    importorskip had been vetted against the inventory.
    """

    def test_module_level_importorskip_with_reason(self):
        src = (
            "import pytest\n"
            'pytest.importorskip("sqlalchemy", reason="sqlalchemy not installed (sql extra)")\n'
        )
        markers = find_skip_markers(src)
        assert len(markers) == 1
        assert markers[0].kind == "importorskip"
        assert markers[0].reason == "sqlalchemy not installed (sql extra)"
        assert markers[0].lineno == 2

    def test_importorskip_inside_a_test_body_is_scanned(self):
        """Unlike a dynamic ``pytest.skip()``, this one is statically decidable.

        ``pytest.importorskip("duckdb")`` skips on a MODULE being absent — a
        property of the environment the inventory already reasons about via
        ``sanctioned_when_module_absent`` — not on a runtime condition the
        static scan would have to guess at.
        """
        src = (
            "import pytest\n"
            "def test_x():\n"
            '    pytest.importorskip("duckdb", reason="duckdb not installed")\n'
        )
        markers = find_skip_markers(src)
        assert len(markers) == 1
        assert markers[0].kind == "importorskip"
        assert markers[0].reason == "duckdb not installed"

    def test_importorskip_module_name_is_not_read_as_a_reason(self):
        """``args[0]`` is the MODULE, not a reason — the positional fallback must not apply.

        ``pytest.mark.skip("some reason")`` takes its reason positionally, so
        ``_marker_from_expr`` falls back to ``args[0]``. Reusing that fallback
        here would make ``"surrealdb"`` the reason and fail the commit with
        "matches no entry in skip_inventory.json" instead of the correct
        "has no literal reason= string".
        """
        src = 'import pytest\npytest.importorskip("surrealdb")\n'
        markers = find_skip_markers(src)
        assert len(markers) == 1
        assert markers[0].reason is None

    def test_new_unsanctioned_importorskip_fails(self):
        src = (
            "import pytest\n"
            'pytest.importorskip("leftpad", reason="leftpad not installed anywhere")\n'
        )
        violations = find_new_unsanctioned(src, {2}, ["duckdb not installed"])
        assert len(violations) == 1
        assert "leftpad not installed anywhere" in violations[0].message

    def test_new_sanctioned_importorskip_passes(self):
        src = 'import pytest\npytest.importorskip("duckdb", reason="duckdb not installed")\n'
        assert find_new_unsanctioned(src, {2}, ["duckdb not installed"]) == []

    def test_new_bare_importorskip_fails_for_missing_reason(self):
        src = 'import pytest\npytest.importorskip("surrealdb")\n'
        violations = find_new_unsanctioned(src, {2}, ["duckdb not installed"])
        assert len(violations) == 1
        assert "no literal reason" in violations[0].message

    def test_preexisting_importorskip_not_flagged(self):
        """Only ADDED lines are gated — the 40-odd shipped calls stay untouched."""
        src = 'import pytest\npytest.importorskip("surrealdb")\n'
        assert find_new_unsanctioned(src, set(), ["duckdb not installed"]) == []

    def test_unrelated_import_call_not_matched(self):
        src = 'import importlib\nimportlib.import_module("duckdb")\n'
        assert find_skip_markers(src) == []

    def test_shipped_inventory_sanctions_a_real_importorskip_reason(self):
        """Judged against the SHIPPED inventory, not a stub pattern list.

        Every other test here passes a hand-made ``patterns`` list, which
        proves the matching logic and nothing about whether the reasons this
        repo's importorskips actually carry are reachable. The reason below is
        copied verbatim from ``yadgar/tests/_shared/test_mariadb_engine.py:23``.
        """
        from check_skip_inventory import _load_inventory

        patterns = _load_inventory()
        src = (
            "import pytest\n"
            'pytest.importorskip("sqlalchemy", reason="sqlalchemy not installed (sql extra)")\n'
        )
        assert find_new_unsanctioned(src, {2}, patterns) == []
        bogus = 'import pytest\npytest.importorskip("leftpad", reason="leftpad is missing")\n'
        assert len(find_new_unsanctioned(bogus, {2}, patterns)) == 1


# ---------------------------------------------------------------------------
# added_lines_from_diff — unified-diff hunk parsing
# ---------------------------------------------------------------------------


class TestAddedLinesFromDiff:
    def test_empty_diff(self):
        assert added_lines_from_diff("") == set()

    def test_single_added_line(self):
        diff = "diff --git a/t.py b/t.py\n--- a/t.py\n+++ b/t.py\n@@ -0,0 +3 @@\n+x = 1\n"
        assert added_lines_from_diff(diff) == {3}

    def test_added_range(self):
        diff = "@@ -10,0 +11,3 @@\n+a\n+b\n+c\n"
        assert added_lines_from_diff(diff) == {11, 12, 13}

    def test_multiple_hunks(self):
        diff = "@@ -1,0 +2,2 @@\n+a\n+b\n@@ -50,0 +60 @@\n+z\n"
        assert added_lines_from_diff(diff) == {2, 3, 60}

    def test_zero_length_add_ignored(self):
        # pure deletion hunk: +start,0
        diff = "@@ -5,2 +4,0 @@\n-a\n-b\n"
        assert added_lines_from_diff(diff) == set()


# ---------------------------------------------------------------------------
# find_new_unsanctioned — integration of the three pieces
# ---------------------------------------------------------------------------

_PATTERNS = ["requires macos host", "duckdb not installed"]


class TestFindNewUnsanctioned:
    def test_new_sanctioned_marker_passes(self):
        src = (
            "import pytest\n"
            '@pytest.mark.skip(reason="requires macOS host")\n'
            "def test_mac():\n"
            "    pass\n"
        )
        violations = find_new_unsanctioned(src, added_lines={2}, patterns=_PATTERNS)
        assert violations == []

    def test_new_unsanctioned_marker_fails(self):
        src = (
            "import pytest\n"
            '@pytest.mark.skip(reason="brand new unexplained skip")\n'
            "def test_x():\n"
            "    pass\n"
        )
        violations = find_new_unsanctioned(src, added_lines={2}, patterns=_PATTERNS)
        assert len(violations) == 1
        assert violations[0].lineno == 2

    def test_old_unsanctioned_marker_not_flagged(self):
        """A marker whose lines are NOT in the staged diff is pre-existing — skip it."""
        src = (
            "import pytest\n"
            '@pytest.mark.skip(reason="brand new unexplained skip")\n'
            "def test_x():\n"
            "    pass\n"
        )
        violations = find_new_unsanctioned(src, added_lines={4}, patterns=_PATTERNS)
        assert violations == []

    def test_new_marker_without_reason_fails(self):
        src = "import pytest\n@pytest.mark.skip\ndef test_x():\n    pass\n"
        violations = find_new_unsanctioned(src, added_lines={2}, patterns=_PATTERNS)
        assert len(violations) == 1
        assert "reason" in violations[0].message.lower()

    def test_multiline_marker_intersects_added_lines(self):
        src = (
            "import pytest\n"
            "@pytest.mark.skipif(\n"
            "    True,\n"
            '    reason="totally new and unexplained",\n'
            ")\n"
            "def test_x():\n"
            "    pass\n"
        )
        # only an interior line of the decorator was added
        violations = find_new_unsanctioned(src, added_lines={4}, patterns=_PATTERNS)
        assert len(violations) == 1

    def test_no_added_lines_no_violations(self):
        src = (
            "import pytest\n"
            '@pytest.mark.skip(reason="brand new unexplained skip")\n'
            "def test_x():\n"
            "    pass\n"
        )
        assert find_new_unsanctioned(src, added_lines=set(), patterns=_PATTERNS) == []
