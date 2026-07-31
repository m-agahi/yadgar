"""Meta-tests for the CHANGELOG `[Unreleased]` version-marker lint (Car 0102).

`docs/CHANGELOG.md` had not cut a version-numbered section since `[5.106.0]`:
55 entries across 40 shipped versions sat undifferentiated inside
`[Unreleased]`, each already carrying its own version inline as a bold
`**vX.Y.Z — ...**` marker. Nothing caught this because nothing checked
`[Unreleased]` for entries that already claim a shipped version. This guard
closes that gap.

Non-e2e, hermetic — never touches the real docs/CHANGELOG.md except via the
final "the real changelog is currently clean" smoke tests.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.parent.parent


def _load(script_name: str):  # type: ignore[return]
    script_path = _REPO_ROOT / "scripts" / script_name
    spec = importlib.util.spec_from_file_location(script_name.replace(".py", ""), script_path)
    assert spec and spec.loader, f"Cannot load {script_path}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


ccuv = _load("check_changelog_unreleased_versions.py")


def _make_repo(tmp_path: Path, changelog_body: str) -> Path:
    docs = tmp_path / "docs"
    docs.mkdir(parents=True)
    (docs / "CHANGELOG.md").write_text(changelog_body, encoding="utf-8")
    return tmp_path


class TestExtractUnreleasedBody:
    def test_extracts_body_between_unreleased_and_next_heading(self) -> None:
        text = (
            "# Changelog\n\n## [Unreleased]\n\n**fix: a.**\n\n## [1.0.0] - 2026-01-01\n\n**old.**\n"
        )
        body = ccuv.extract_unreleased_body(text)
        assert "**fix: a.**" in body
        assert "**old.**" not in body

    def test_no_unreleased_heading_returns_empty(self) -> None:
        text = "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n**old.**\n"
        assert ccuv.extract_unreleased_body(text) == ""

    def test_only_first_unreleased_heading_used(self) -> None:
        # A pre-existing historical duplicate `## [Unreleased]` heading deeper
        # in the file (real artifact in this repo's own CHANGELOG) must not
        # be scanned — only the true top-of-file section is in scope.
        text = (
            "## [Unreleased]\n\n**fix: real.**\n\n"
            "## [1.0.0] - 2026-01-01\n\n**old.**\n\n"
            "## [Unreleased]\n\n**v9.0.0 — historical duplicate, out of scope.**\n"
        )
        body = ccuv.extract_unreleased_body(text)
        assert "real." in body
        assert "historical duplicate" not in body

    def test_unreleased_at_end_of_file_returns_rest(self) -> None:
        text = "## [Unreleased]\n\n**fix: a.**\n"
        body = ccuv.extract_unreleased_body(text)
        assert "**fix: a.**" in body


class TestFindVersionMarkers:
    def test_finds_inline_version_marker(self) -> None:
        body = "**v5.167.1 — fix: something (#72).** Body text.\n"
        assert ccuv.find_version_markers(body) == ["5.167.1"]

    def test_no_marker_on_plain_bold_entry(self) -> None:
        # A genuinely unreleased entry with no version claim yet — must stay
        # unflagged (this is the correct, permanent state for in-flight work).
        body = "**fix: something not yet shipped (Car 0099).** Body text.\n"
        assert ccuv.find_version_markers(body) == []

    def test_bullet_sub_item_not_treated_as_header(self) -> None:
        # Bullets belong to the entry above them, never a new top-level marker.
        body = "**v5.166.0 — some train.** Intro.\n\n- **v-looking bullet, not a header (Car 7)** — detail.\n"
        assert ccuv.find_version_markers(body) == ["5.166.0"]

    def test_prose_mention_of_version_mid_line_not_flagged(self) -> None:
        body = "**fix: something (core 5.125.0, backend 5.37.0).** Body.\n"
        assert ccuv.find_version_markers(body) == []

    def test_multiple_markers_all_found_in_order(self) -> None:
        body = "**v5.167.1 — a.** x.\n\n**v5.166.4 — b.** y.\n\n**v5.166.4 — c.** z.\n"
        assert ccuv.find_version_markers(body) == ["5.167.1", "5.166.4", "5.166.4"]


class TestCheck:
    def test_clean_unreleased_has_no_violations(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            "## [Unreleased]\n\n**fix: not yet shipped.** Body.\n\n## [1.0.0] - 2026-01-01\n\n**v1.0.0 — old, already sectioned.** Body.\n",
        )
        assert ccuv.check(repo) == []

    def test_shipped_marker_in_unreleased_is_flagged(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            "## [Unreleased]\n\n**v5.167.1 — fix: shipped but not promoted.** Body.\n",
        )
        violations = ccuv.check(repo)
        assert len(violations) == 1
        assert "v5.167.1" in violations[0]

    def test_multiple_shipped_markers_each_flagged(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            "## [Unreleased]\n\n**v5.167.1 — a.** x.\n\n**v5.166.4 — b.** y.\n",
        )
        violations = ccuv.check(repo)
        assert len(violations) == 2

    def test_missing_changelog_file_is_reported_not_raised(self, tmp_path: Path) -> None:
        (tmp_path / "docs").mkdir()
        violations = ccuv.check(tmp_path)
        assert len(violations) == 1
        assert "not found" in violations[0]

    def test_no_unreleased_section_at_all_has_no_violations(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, "## [1.0.0] - 2026-01-01\n\n**v1.0.0 — old.** Body.\n")
        assert ccuv.check(repo) == []


class TestMain:
    def test_main_returns_0_on_the_real_clean_tree(self) -> None:
        # main() has no repo_root param (it always resolves the real repo
        # from its own file location) — this IS the guard as pre-commit/CI
        # will actually invoke it, post-Car-0102-fix.
        assert ccuv.main() == 0

    def test_real_changelog_unreleased_is_currently_clean(self) -> None:
        """Smoke test against the actual repo tree — the whole point of the guard."""
        violations = ccuv.check()
        assert violations == [], violations
