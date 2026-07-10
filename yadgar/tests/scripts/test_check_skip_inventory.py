"""TDD tests for scripts/check_skip_inventory.py — skip-inventory gate.

Covers:
  - Sanctioned skip passes
  - Unsanctioned skip fails
  - Multiple skips, all sanctioned
  - Multiple skips, one unsanctioned
  - Empty output passes
  - Reason pattern is substring match (case-insensitive)
  - Mis-gated regression case: a new skip reason without inventory entry fails
"""

from __future__ import annotations

import json

# Import the module under test
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent / "scripts"))

from check_skip_inventory import (
    _extract_skip_reasons,
    _is_sanctioned,
    check,
    validate_inventory,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_inventory(patterns: list[str], tmp_path: Path) -> Path:
    """Write a minimal inventory JSON with the given reason_patterns."""
    data = {
        "_meta": {"version": "1", "description": "test inventory"},
        "entries": [
            {
                "id": f"e{i}",
                "file": "x.py",
                "verdict": "LEGIT-CONDITIONAL",
                "reason_pattern": p,
                "note": "test",
            }
            for i, p in enumerate(patterns)
        ],
    }
    inv = tmp_path / "skip_inventory.json"
    inv.write_text(json.dumps(data), encoding="utf-8")
    return inv


def _skip_line(reason: str, count: int = 1, loc: str = "yadgar/tests/foo.py:42") -> str:
    return f"SKIPPED [{count}] {loc}: {reason}"


# ---------------------------------------------------------------------------
# _extract_skip_reasons
# ---------------------------------------------------------------------------


class TestExtractSkipReasons:
    def test_empty_input(self):
        assert _extract_skip_reasons([]) == []

    def test_non_skip_line_ignored(self):
        lines = ["PASSED tests/foo.py::test_bar", "= 1 passed in 0.1s ="]
        assert _extract_skip_reasons(lines) == []

    def test_single_skip_extracted(self):
        line = _skip_line("macOS only")
        result = _extract_skip_reasons([line])
        assert len(result) == 1
        assert result[0][1] == "macOS only"

    def test_reason_with_colon_in_reason(self):
        line = _skip_line("duckdb not installed: missing optional dep")
        result = _extract_skip_reasons([line])
        assert result[0][1] == "duckdb not installed: missing optional dep"

    def test_count_field_ignored(self):
        line = _skip_line("some reason", count=10)
        result = _extract_skip_reasons([line])
        assert result[0][1] == "some reason"

    def test_multiple_skips_extracted(self):
        lines = [
            _skip_line("macOS only", loc="tests/a.py:1"),
            "PASSED tests/b.py::test_ok",
            _skip_line("duckdb not installed", loc="tests/c.py:99"),
        ]
        result = _extract_skip_reasons(lines)
        assert len(result) == 2
        assert result[0][1] == "macOS only"
        assert result[1][1] == "duckdb not installed"


# ---------------------------------------------------------------------------
# _is_sanctioned
# ---------------------------------------------------------------------------


class TestIsSanctioned:
    def test_exact_match(self):
        assert _is_sanctioned("macOS only", ["macOS only"])

    def test_substring_match(self):
        assert _is_sanctioned("macOS only — launchd", ["macOS only"])

    def test_case_insensitive(self):
        assert _is_sanctioned("MACOS ONLY", ["macOS only"])

    def test_no_match(self):
        assert not _is_sanctioned("unrecognised reason", ["macOS only", "duckdb not installed"])

    def test_multiple_patterns_first_matches(self):
        assert _is_sanctioned("duckdb not installed", ["macOS only", "duckdb not installed"])

    def test_empty_patterns(self):
        assert not _is_sanctioned("anything", [])


# ---------------------------------------------------------------------------
# check() — integration
# ---------------------------------------------------------------------------


class TestCheck:
    def test_empty_lines_ok(self, tmp_path):
        inv = _make_inventory(["macOS only"], tmp_path)
        ok, offenders = check([], inv)
        assert ok
        assert offenders == []

    def test_no_skip_lines_ok(self, tmp_path):
        inv = _make_inventory(["macOS only"], tmp_path)
        lines = ["PASSED tests/foo.py::test_bar", "= 1 passed ="]
        ok, offenders = check(lines, inv)
        assert ok

    def test_sanctioned_skip_passes(self, tmp_path):
        inv = _make_inventory(["macOS only"], tmp_path)
        lines = [_skip_line("macOS only")]
        ok, offenders = check(lines, inv)
        assert ok
        assert offenders == []

    def test_unsanctioned_skip_fails(self, tmp_path):
        inv = _make_inventory(["macOS only"], tmp_path)
        lines = [_skip_line("new feature not implemented yet")]
        ok, offenders = check(lines, inv)
        assert not ok
        assert len(offenders) == 1
        assert "UNSANCTIONED" in offenders[0]

    def test_all_sanctioned_multiple(self, tmp_path):
        inv = _make_inventory(["macOS only", "duckdb not installed"], tmp_path)
        lines = [
            _skip_line("macOS only", loc="tests/a.py:1"),
            _skip_line("duckdb not installed", loc="tests/b.py:2"),
        ]
        ok, offenders = check(lines, inv)
        assert ok

    def test_one_unsanctioned_in_mix_fails(self, tmp_path):
        inv = _make_inventory(["macOS only"], tmp_path)
        lines = [
            _skip_line("macOS only", loc="tests/a.py:1"),
            _skip_line("BRAND NEW UNSANCTIONED REASON", loc="tests/b.py:99"),
        ]
        ok, offenders = check(lines, inv)
        assert not ok
        assert len(offenders) == 1
        assert "BRAND NEW UNSANCTIONED REASON" in offenders[0]

    def test_mis_gated_regression(self, tmp_path):
        """A skip that was mis-gated (patch target stale) and now fixed must no
        longer appear in -rs output. This test verifies the gate correctly fails
        when a NEW skip (not in inventory) slips in — simulating a regression.

        Scenario: developer adds a new pytest.skip() without updating inventory.
        Gate must catch it.
        """
        inv = _make_inventory(["macOS only"], tmp_path)
        # Developer adds a skip for a new feature without updating inventory
        lines = [_skip_line("feature X not implemented — will fix in Q3")]
        ok, offenders = check(lines, inv)
        assert not ok, "Gate must FAIL for skip not in inventory"
        assert "feature X not implemented" in offenders[0]

    def test_substring_match_in_long_reason(self, tmp_path):
        inv = _make_inventory(["duckdb not installed"], tmp_path)
        lines = [_skip_line("duckdb not installed: pip install duckdb to enable")]
        ok, offenders = check(lines, inv)
        assert ok

    def test_case_insensitive_pattern_match(self, tmp_path):
        inv = _make_inventory(["MACOS ONLY"], tmp_path)
        lines = [_skip_line("macOS only")]
        ok, offenders = check(lines, inv)
        assert ok


# ---------------------------------------------------------------------------
# validate_inventory — ADR-0087 governance (reason length, stale entries,
# wildcard patterns)
# ---------------------------------------------------------------------------

_LONG_NOTE = "justification long enough to satisfy the forty-char governance floor"


def _entry(**overrides):
    e = {
        "id": "e1",
        "file": "yadgar/tests/foo.py",
        "verdict": "LEGIT-CONDITIONAL",
        "reason_pattern": "requires macOS host",
        "note": _LONG_NOTE,
    }
    e.update(overrides)
    return e


def _write_test_file(tmp_path, relpath, content):
    p = tmp_path / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


class TestValidateInventory:
    def test_valid_entry_passes(self, tmp_path):
        _write_test_file(
            tmp_path,
            "yadgar/tests/foo.py",
            '@pytest.mark.skipif(IS_LINUX, reason="requires macOS host")\n',
        )
        errors = validate_inventory({"entries": [_entry()]}, repo_root=tmp_path)
        assert errors == []

    def test_missing_required_field_fails(self, tmp_path):
        e = _entry()
        del e["verdict"]
        errors = validate_inventory({"entries": [e]}, repo_root=tmp_path)
        assert any("missing fields" in err for err in errors)

    def test_invalid_verdict_fails(self, tmp_path):
        errors = validate_inventory({"entries": [_entry(verdict="MAYBE")]}, repo_root=tmp_path)
        assert any("invalid verdict" in err for err in errors)

    def test_short_note_fails(self, tmp_path):
        """ADR-0087: required reason/justification >= 40 chars."""
        _write_test_file(tmp_path, "yadgar/tests/foo.py", 'reason="requires macOS host"\n')
        errors = validate_inventory({"entries": [_entry(note="too short")]}, repo_root=tmp_path)
        assert any("note" in err and "40" in err for err in errors)

    def test_stale_entry_missing_file_fails(self, tmp_path):
        """ADR-0087: entry whose test file no longer exists hard-fails."""
        errors = validate_inventory(
            {"entries": [_entry(file="yadgar/tests/gone.py")]}, repo_root=tmp_path
        )
        assert any("stale" in err.lower() for err in errors)

    def test_stale_entry_pattern_not_in_file_fails(self, tmp_path):
        """ADR-0087: reason_pattern that matches nothing in the file hard-fails."""
        _write_test_file(
            tmp_path, "yadgar/tests/foo.py", 'reason="a completely different reason"\n'
        )
        errors = validate_inventory({"entries": [_entry()]}, repo_root=tmp_path)
        assert any("stale" in err.lower() for err in errors)

    def test_pattern_match_is_case_insensitive(self, tmp_path):
        _write_test_file(tmp_path, "yadgar/tests/foo.py", 'reason="REQUIRES MACOS HOST"\n')
        errors = validate_inventory({"entries": [_entry()]}, repo_root=tmp_path)
        assert errors == []

    def test_wildcard_pattern_fails(self, tmp_path):
        _write_test_file(tmp_path, "yadgar/tests/foo.py", "x = 1\n")
        errors = validate_inventory({"entries": [_entry(reason_pattern="*")]}, repo_root=tmp_path)
        assert any("wildcard" in err.lower() for err in errors)

    def test_empty_pattern_fails(self, tmp_path):
        _write_test_file(tmp_path, "yadgar/tests/foo.py", "x = 1\n")
        errors = validate_inventory({"entries": [_entry(reason_pattern="")]}, repo_root=tmp_path)
        assert errors  # empty pattern is never acceptable

    def test_too_short_pattern_fails(self, tmp_path):
        """Substring matching makes very short patterns act as wildcards."""
        _write_test_file(tmp_path, "yadgar/tests/foo.py", 'reason="not so bad"\n')
        errors = validate_inventory({"entries": [_entry(reason_pattern="not")]}, repo_root=tmp_path)
        assert any("short" in err.lower() for err in errors)

    def test_real_inventory_is_valid(self):
        """The committed inventory must satisfy its own governance rules."""
        repo_root = Path(__file__).resolve().parent.parent.parent.parent
        data = json.loads(
            (repo_root / "yadgar" / "tests" / "skip_inventory.json").read_text(encoding="utf-8")
        )
        assert validate_inventory(data, repo_root=repo_root) == []
