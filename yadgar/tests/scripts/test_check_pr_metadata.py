"""Unit tests for scripts/check_pr_metadata.py.

Tests the pure ``validate_pr_metadata()`` function and the ``main()`` entry point.
"""

from __future__ import annotations

import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Import the script from scripts/ — not a package, use direct path injection.
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = str(Path(__file__).parent.parent.parent.parent / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from check_pr_metadata import main, validate_pr_metadata  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers — _GOOD_BODY has all 5 required sections with >=20 real chars each
# ---------------------------------------------------------------------------

_GOOD_TITLE = "feat: add viz trace view train"

# Plain H2-header form — all five sections, each with substantive content.
_GOOD_BODY = """\
## Summary

This is a substantive summary of the changes made in this pull request for review.

## What

- Changed the validator to require five sections with real content, not just headers.
- Added dual-detection so collapsible details blocks also count.

## Why

The old validator only checked two sections and did not enforce content length, so
empty template placeholders could slip through the gate unnoticed.

## Notes

No breaking changes to the public API. The migration path is straightforward: fill
in all five sections with at least twenty non-whitespace characters of real content.

## Test plan

- [ ] All unit tests pass under pytest.
- [ ] Manual smoke-check: script exits 0 with a well-formed body.
"""

# Collapsible <details> form — same five sections, same content requirement met.
_GOOD_BODY_COLLAPSIBLE = """\
## Summary

This is a substantive summary of the changes made in this pull request for review.

<details>
<summary><b>What</b></summary>

Changed the validator to require five sections with real content, not just headers.
Added dual-detection so collapsible details blocks also count as section presence.

</details>

<details>
<summary><b>Why</b></summary>

The old validator only checked two sections and did not enforce content length, so
empty template placeholders could slip through the gate unnoticed by reviewers.

</details>

<details>
<summary><b>Notes</b></summary>

No breaking changes to the public API. Migration path is straightforward: fill in
all five sections with at least twenty non-whitespace characters of real content.

</details>

<details>
<summary><b>Test plan</b></summary>

All unit tests pass under pytest. Manual smoke-check: script exits 0 with a
well-formed body containing real content in each required section.

</details>
"""


# ---------------------------------------------------------------------------
# validate_pr_metadata — valid inputs
# ---------------------------------------------------------------------------


class TestValidatePrMetadataValid:
    def test_valid_title_and_body_plain_headers_returns_empty(self) -> None:
        errors = validate_pr_metadata(_GOOD_TITLE, _GOOD_BODY)
        assert errors == []

    def test_valid_title_and_body_collapsible_returns_empty(self) -> None:
        """Dual-detection: <details><summary><b>Name</b></summary> form must pass."""
        errors = validate_pr_metadata(_GOOD_TITLE, _GOOD_BODY_COLLAPSIBLE)
        assert errors == []

    def test_case_insensitive_plain_headers(self) -> None:
        body = _GOOD_BODY.replace("## Summary", "## summary").replace("## What", "## WHAT")
        errors = validate_pr_metadata(_GOOD_TITLE, body)
        assert errors == []

    def test_case_insensitive_collapsible_summary_tag(self) -> None:
        body = _GOOD_BODY_COLLAPSIBLE.replace("<b>What</b>", "<b>WHAT</b>")
        errors = validate_pr_metadata(_GOOD_TITLE, body)
        assert errors == []

    def test_title_with_surrounding_whitespace_is_trimmed_and_valid(self) -> None:
        errors = validate_pr_metadata("  feat: refactor stuff  ", _GOOD_BODY)
        assert errors == []

    def test_exactly_20_nonwhitespace_chars_in_section_is_valid(self) -> None:
        """Boundary: exactly 20 non-ws chars in content (stripped of tags/comments)."""
        # 20 chars: "abcdefghijklmnopqrst"
        # Pad with spaces to keep body structure intact.
        content_20 = "a b c d e f g h i j k l m n o p q r s t"  # 20 letters, 19 spaces
        body = (
            "## Summary\n\n"
            + content_20
            + "\n\n## What\n\n"
            + content_20
            + "\n\n## Why\n\n"
            + content_20
            + "\n\n## Notes\n\n"
            + content_20
            + "\n\n## Test plan\n\n"
            + content_20
            + "\n"
        )
        errors = validate_pr_metadata(_GOOD_TITLE, body)
        assert errors == []


# ---------------------------------------------------------------------------
# validate_pr_metadata — title errors
# ---------------------------------------------------------------------------


class TestValidatePrMetadataTitleErrors:
    def test_short_title_returns_error(self) -> None:
        errors = validate_pr_metadata("fix bug", _GOOD_BODY)
        assert any("too short" in e for e in errors)

    def test_empty_title_returns_error(self) -> None:
        errors = validate_pr_metadata("", _GOOD_BODY)
        assert any("too short" in e for e in errors)

    def test_title_exactly_7_chars_is_too_short(self) -> None:
        errors = validate_pr_metadata("abcdefg", _GOOD_BODY)
        assert any("too short" in e for e in errors)

    def test_title_exactly_8_chars_single_word_gives_descriptive_error(self) -> None:
        errors = validate_pr_metadata("abcdefgh", _GOOD_BODY)
        assert not any("too short" in e for e in errors)
        assert any("multiple words" in e for e in errors)

    def test_single_word_title_returns_error(self) -> None:
        errors = validate_pr_metadata("refactoring", _GOOD_BODY)
        assert any("multiple words" in e for e in errors)

    def test_multiple_errors_accumulate(self) -> None:
        errors = validate_pr_metadata("fix", _GOOD_BODY)
        assert any("too short" in e for e in errors)
        assert any("multiple words" in e for e in errors)
        assert len(errors) == 2


# ---------------------------------------------------------------------------
# validate_pr_metadata — missing section errors (each of the 5 individually)
# ---------------------------------------------------------------------------


class TestValidatePrMetadataMissingSections:
    def _body_without(self, section_name: str) -> str:
        """Return _GOOD_BODY with every occurrence of one section removed."""
        lines = []
        skip = False
        for line in _GOOD_BODY.splitlines(keepends=True):
            if line.strip().lower() == f"## {section_name.lower()}":
                skip = True
                continue
            # Stop skipping at next H2
            if skip and line.startswith("## "):
                skip = False
            if not skip:
                lines.append(line)
        return "".join(lines)

    def test_missing_summary_returns_error(self) -> None:
        errors = validate_pr_metadata(_GOOD_TITLE, self._body_without("Summary"))
        assert any("Summary" in e for e in errors)

    def test_missing_what_returns_error(self) -> None:
        errors = validate_pr_metadata(_GOOD_TITLE, self._body_without("What"))
        assert any("What" in e for e in errors)

    def test_missing_why_returns_error(self) -> None:
        errors = validate_pr_metadata(_GOOD_TITLE, self._body_without("Why"))
        assert any("Why" in e for e in errors)

    def test_missing_notes_returns_error(self) -> None:
        errors = validate_pr_metadata(_GOOD_TITLE, self._body_without("Notes"))
        assert any("Notes" in e for e in errors)

    def test_missing_test_plan_returns_error(self) -> None:
        errors = validate_pr_metadata(_GOOD_TITLE, self._body_without("Test plan"))
        assert any("Test plan" in e for e in errors)

    def test_missing_section_error_references_template_path(self) -> None:
        errors = validate_pr_metadata(_GOOD_TITLE, self._body_without("What"))
        assert any("PULL_REQUEST_TEMPLATE" in e for e in errors)

    def test_empty_body_has_all_five_section_errors(self) -> None:
        errors = validate_pr_metadata(_GOOD_TITLE, "")
        section_names = {"Summary", "What", "Why", "Notes", "Test plan"}
        for name in section_names:
            assert any(name in e for e in errors), f"Expected error for section: {name}"
        # Title errors are 0 here; all 5 section errors should be present.
        assert len(errors) == 5


# ---------------------------------------------------------------------------
# validate_pr_metadata — under-length section content errors
# ---------------------------------------------------------------------------


class TestValidatePrMetadataContentLength:
    def _body_with_section_content(self, section: str, content: str) -> str:
        """Replace one section's content in _GOOD_BODY with a custom value."""
        import re

        replacement = f"## {section}\n\n{content}\n\n"
        # Replace from the section header up to the next H2 (or EOF).
        pattern = rf"## {re.escape(section)}\n.*?(?=## |\Z)"
        return re.sub(pattern, replacement, _GOOD_BODY, flags=re.DOTALL)

    def test_section_content_only_html_comment_is_under_length(self) -> None:
        """HTML comments strip to 0 chars — should fail the >=20 rule."""
        body = self._body_with_section_content("What", "<!-- describe what changed -->")
        errors = validate_pr_metadata(_GOOD_TITLE, body)
        assert any("What" in e for e in errors)

    def test_section_content_only_bare_dash_is_under_length(self) -> None:
        """A bare '-' placeholder strips to 1 non-ws char — under 20."""
        body = self._body_with_section_content("Notes", "-")
        errors = validate_pr_metadata(_GOOD_TITLE, body)
        assert any("Notes" in e for e in errors)

    def test_section_content_only_html_tags_is_under_length(self) -> None:
        """Pure HTML tags strip to 0 chars — should fail."""
        body = self._body_with_section_content("Why", "<br><hr><p></p>")
        errors = validate_pr_metadata(_GOOD_TITLE, body)
        assert any("Why" in e for e in errors)

    def test_exactly_19_nonwhitespace_chars_is_under_length(self) -> None:
        """Boundary: 19 non-ws chars must fail."""
        # 19 letters separated by spaces: "a b c d e f g h i j k l m n o p q r s"
        content_19 = "a b c d e f g h i j k l m n o p q r s"
        body = self._body_with_section_content("Summary", content_19)
        errors = validate_pr_metadata(_GOOD_TITLE, body)
        assert any("Summary" in e for e in errors)

    def test_under_length_error_includes_section_name_and_char_count(self) -> None:
        """Error message must name the section and include the actual count."""
        body = self._body_with_section_content("Why", "short")  # 5 chars
        errors = validate_pr_metadata(_GOOD_TITLE, body)
        why_errors = [e for e in errors if "Why" in e]
        assert why_errors, "Expected at least one error mentioning 'Why'"
        # Should contain a digit (the actual count)
        assert any(any(c.isdigit() for c in e) for e in why_errors)

    def test_comment_stripped_before_measuring_content(self) -> None:
        """Content that is only a comment in a collapsible block should fail."""
        body = """\
## Summary

This is a substantive summary of the changes for this pull request review.

<details>
<summary><b>What</b></summary>

<!-- describe what changed here in detail -->

</details>

<details>
<summary><b>Why</b></summary>

Motivation goes here to explain the reason behind these changes thoroughly.

</details>

<details>
<summary><b>Notes</b></summary>

No breaking changes to the public API surface; migration is straightforward.

</details>

<details>
<summary><b>Test plan</b></summary>

All tests pass; manual smoke confirmed with exit code zero on valid body.

</details>
"""
        errors = validate_pr_metadata(_GOOD_TITLE, body)
        assert any("What" in e for e in errors)

    def test_mixed_comment_and_real_content_counts_only_real(self) -> None:
        """Real content + comment: only real chars count toward 20."""
        # 15 real non-ws chars + big comment = should still fail (15 < 20)
        body = self._body_with_section_content(
            "Notes", "short real text <!-- this is a comment that should not count -->"
        )
        errors = validate_pr_metadata(_GOOD_TITLE, body)
        # "shortrealtex t" → "shortrealtext" = 13 chars — under 20
        assert any("Notes" in e for e in errors)


# ---------------------------------------------------------------------------
# main() — environment variable integration
# ---------------------------------------------------------------------------


class TestMain:
    def test_main_returns_0_for_valid_inputs(self, monkeypatch: object) -> None:
        monkeypatch.setenv("PR_TITLE", _GOOD_TITLE)
        monkeypatch.setenv("PR_BODY", _GOOD_BODY)
        assert main() == 0

    def test_main_returns_0_for_collapsible_body(self, monkeypatch: object) -> None:
        monkeypatch.setenv("PR_TITLE", _GOOD_TITLE)
        monkeypatch.setenv("PR_BODY", _GOOD_BODY_COLLAPSIBLE)
        assert main() == 0

    def test_main_returns_1_for_invalid_title(self, monkeypatch: object, capsys: object) -> None:
        monkeypatch.setenv("PR_TITLE", "bad")
        monkeypatch.setenv("PR_BODY", _GOOD_BODY)
        rc = main()
        assert rc == 1
        captured = capsys.readouterr()
        assert "ERROR:" in captured.err

    def test_main_returns_1_for_missing_body_sections(
        self, monkeypatch: object, capsys: object
    ) -> None:
        monkeypatch.setenv("PR_TITLE", _GOOD_TITLE)
        monkeypatch.setenv("PR_BODY", "Some text without required headers.")
        rc = main()
        assert rc == 1
        captured = capsys.readouterr()
        assert "ERROR:" in captured.err

    def test_main_prints_ok_for_valid_inputs(self, monkeypatch: object, capsys: object) -> None:
        monkeypatch.setenv("PR_TITLE", _GOOD_TITLE)
        monkeypatch.setenv("PR_BODY", _GOOD_BODY)
        main()
        captured = capsys.readouterr()
        assert "OK" in captured.out

    def test_main_defaults_to_empty_strings_when_env_missing(self, monkeypatch: object) -> None:
        monkeypatch.delenv("PR_TITLE", raising=False)
        monkeypatch.delenv("PR_BODY", raising=False)
        assert main() == 1
