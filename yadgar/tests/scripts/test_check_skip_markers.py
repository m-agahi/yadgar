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
