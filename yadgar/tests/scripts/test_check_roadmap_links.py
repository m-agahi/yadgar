"""Meta-tests for the ROADMAP link-liveness lint (Car 0026).

`docs/plans/ROADMAP.md` drifted for over two weeks with 9 links pointing at
plans that had shipped and moved to `archive/`, one link resolving nowhere
(a plan renamed before it ever shipped), and one backticked bare-filename
reference surviving a docs-reorg move. This guard closes that gap.

Non-e2e, hermetic — never touches the real docs/plans/ tree except via a
single "real roadmap is currently clean" smoke test.
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


crl = _load("check_roadmap_links.py")


def _make_repo(tmp_path: Path, roadmap_body: str) -> Path:
    plans = tmp_path / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "ROADMAP.md").write_text(roadmap_body, encoding="utf-8")
    return tmp_path


class TestExtractReferences:
    def test_markdown_link_extracted(self) -> None:
        refs = crl.extract_references("see [foo](archive/foo.md) for detail")
        assert refs == {"archive/foo.md"}

    def test_backtick_doc_path_extracted(self) -> None:
        refs = crl.extract_references("blocked on `BEHAVIOR_CONTRACT.md` for the contract")
        assert refs == {"BEHAVIOR_CONTRACT.md"}

    def test_backtick_non_doc_extension_ignored(self) -> None:
        # A source-file mention (.py) is not a doc reference the roadmap's
        # own convention governs — must not be flagged.
        refs = crl.extract_references("see `install_hooks_lib.py` for the guard")
        assert refs == set()

    def test_http_link_ignored(self) -> None:
        refs = crl.extract_references("[docs](https://example.com/readme.md)")
        assert refs == set()

    def test_fenced_code_block_ignored(self) -> None:
        body = "prose\n```\n[fake](nonexistent.md)\n`also-fake.md`\n```\nmore prose"
        assert crl.extract_references(body) == set()

    def test_dedupes_repeated_reference(self) -> None:
        refs = crl.extract_references("[a](x.md) then later [b](x.md)")
        assert refs == {"x.md"}

    def test_angle_bracket_placeholder_ignored(self) -> None:
        refs = crl.extract_references("filename is stable identity: `docs/plans/<slug>.md`")
        assert refs == set()

    def test_glob_pattern_ignored(self) -> None:
        refs = crl.extract_references("moved `roadmap/v*.md`->`roadmap/archive/`")
        assert refs == set()


class TestCheck:
    def test_sibling_relative_link_resolves(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, "[plan](sibling.md)")
        (repo / "docs" / "plans" / "sibling.md").write_text("x", encoding="utf-8")
        assert crl.check(repo) == []

    def test_root_relative_backtick_resolves(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, "see `docs/CHANGELOG.md`")
        changelog = repo / "docs" / "CHANGELOG.md"
        changelog.write_text("x", encoding="utf-8")
        assert crl.check(repo) == []

    def test_bare_filename_referencing_only_archive_is_flagged(self, tmp_path: Path) -> None:
        # Deliberately NO archive/-prefix fallback (see script docstring): a
        # bare `shipped-plan.md` that only exists under archive/ must be
        # caught, not silently resolved — that leniency is what let 9 dead
        # links hide undetected for two weeks.
        repo = _make_repo(tmp_path, "shipped as `shipped-plan.md`")
        archive = repo / "docs" / "plans" / "archive"
        archive.mkdir()
        (archive / "shipped-plan.md").write_text("x", encoding="utf-8")
        violations = crl.check(repo)
        assert len(violations) == 1
        assert "shipped-plan.md" in violations[0]

    def test_dead_link_is_flagged(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, "[gone](nowhere-2026-07-18.md)")
        violations = crl.check(repo)
        assert len(violations) == 1
        assert "nowhere-2026-07-18.md" in violations[0]

    def test_dead_backtick_bare_filename_is_flagged(self, tmp_path: Path) -> None:
        # Reproduces the actual BEHAVIOR_CONTRACT.md incident: the file
        # exists, just not at any of the three candidate locations.
        repo = _make_repo(tmp_path, "blocked on `BEHAVIOR_CONTRACT.md`")
        contracts = repo / "docs" / "contracts"
        contracts.mkdir()
        (contracts / "BEHAVIOR_CONTRACT.md").write_text("x", encoding="utf-8")
        violations = crl.check(repo)
        assert len(violations) == 1
        assert "BEHAVIOR_CONTRACT.md" in violations[0]

    def test_moved_to_archive_without_prefix_update_is_flagged(self, tmp_path: Path) -> None:
        # The exact shape of 9 of the 11 dead refs found 2026-08-01: the
        # roadmap links docs/plans/X.md, but X.md now lives at archive/X.md.
        repo = _make_repo(tmp_path, "[shipped](moved-plan.md)")
        archive = repo / "docs" / "plans" / "archive"
        archive.mkdir()
        (archive / "moved-plan.md").write_text("x", encoding="utf-8")
        violations = crl.check(repo)
        assert len(violations) == 1
        assert "moved-plan.md" in violations[0]

    def test_missing_roadmap_file_is_reported_not_raised(self, tmp_path: Path) -> None:
        (tmp_path / "docs" / "plans").mkdir(parents=True)
        violations = crl.check(tmp_path)
        assert len(violations) == 1
        assert "not found" in violations[0]

    def test_clean_roadmap_has_no_violations(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            "[a](archive/a.md) and `docs/README.md` and `bare.md`",
        )
        (repo / "docs" / "plans" / "archive").mkdir()
        (repo / "docs" / "plans" / "archive" / "a.md").write_text("x", encoding="utf-8")
        (repo / "docs" / "README.md").write_text("x", encoding="utf-8")
        (repo / "docs" / "plans" / "bare.md").write_text("x", encoding="utf-8")
        assert crl.check(repo) == []


class TestMain:
    def test_main_returns_0_on_the_real_clean_tree(self) -> None:
        # main() has no repo_root param (it always resolves the real repo
        # from its own file location), so the exit-code contract is only
        # exercisable against the actual tree — which is the point: this IS
        # the guard as pre-commit/CI will actually invoke it.
        assert crl.main() == 0

    def test_real_roadmap_is_currently_clean(self) -> None:
        """Smoke test against the actual repo tree — the whole point of the guard."""
        violations = crl.check()
        assert violations == [], violations
