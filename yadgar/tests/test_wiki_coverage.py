"""Q3 tests — wiki_coverage() MCP tool (v5.3.5).

Tests:
1. Empty directory → coverage 0%.
2. Directory with 5 files, 3 wiki-covered → coverage 60%, uncovered list has 2 entries.
3. Tool registered + callable via MCP (import path works).
4. Excludes .venv, tests/, __pycache__.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── helpers ───────────────────────────────────────────────────────────────────


def _make_wiki_page(slug: str, tags: list[str]) -> dict:
    return {
        "slug": slug,
        "title": slug.replace("-", " ").title(),
        "tags": tags,
        "category": "reference",
        "_retrieval_score": 0.8,
        "branch": None,
    }


# ── Test 1: empty directory → coverage 0% ────────────────────────────────────


def test_empty_directory_gives_zero_coverage(tmp_path):
    """Empty directory (no .py files) → total=0, coverage=0.0."""
    from yadgar.server.tools.wiki_coverage import wiki_coverage

    with patch("yadgar.server._state._wiki", None):
        result = wiki_coverage(directory=str(tmp_path))

    assert result["total_modules"] == 0
    assert result["covered_modules"] == []
    assert result["uncovered_modules"] == []
    assert result["coverage_pct"] == pytest.approx(0.0)


def test_directory_with_no_py_files_zero_coverage(tmp_path):
    """Directory with only non-.py files → coverage 0%."""
    (tmp_path / "README.md").write_text("# readme")
    (tmp_path / "config.yaml").write_text("key: value")

    from yadgar.server.tools.wiki_coverage import wiki_coverage

    with patch("yadgar.server._state._wiki", None):
        result = wiki_coverage(directory=str(tmp_path))

    assert result["total_modules"] == 0
    assert result["coverage_pct"] == pytest.approx(0.0)


# ── Test 2: 5 files, 3 covered → 60% coverage ────────────────────────────────


def test_five_files_three_covered(tmp_path):
    """5 .py files, 3 with wiki pages tagged mod → 60% coverage, 2 uncovered."""
    # Create 5 .py files
    files = ["alpha.py", "beta.py", "gamma.py", "delta.py", "epsilon.py"]
    for f in files:
        (tmp_path / f).write_text(f"# {f}")

    # Mock wiki with pages covering alpha, beta, gamma via source_file tags
    alpha_path = str(tmp_path / "alpha.py")
    beta_path = str(tmp_path / "beta.py")
    gamma_path = str(tmp_path / "gamma.py")

    mod_pages = [
        _make_wiki_page("alpha-module", ["mod", f"source_file:{alpha_path}"]),
        _make_wiki_page("beta-module", ["mod", f"source_file:{beta_path}"]),
        _make_wiki_page("gamma-module", ["mod", f"source_file:{gamma_path}"]),
    ]

    mock_wiki = MagicMock()
    mock_wiki.query.side_effect = lambda q, tags=None, max_results=500: (
        mod_pages if tags == ["mod"] else []
    )

    from yadgar.server.tools.wiki_coverage import wiki_coverage

    with patch("yadgar.server._state._wiki", mock_wiki):
        result = wiki_coverage(directory=str(tmp_path))

    assert result["total_modules"] == 5
    assert len(result["covered_modules"]) == 3
    assert len(result["uncovered_modules"]) == 2
    assert result["coverage_pct"] == pytest.approx(0.6)

    # Verify the right files are uncovered
    uncovered_basenames = {Path(p).name for p in result["uncovered_modules"]}
    assert uncovered_basenames == {"delta.py", "epsilon.py"}


def test_coverage_with_fn_tagged_pages(tmp_path):
    """wiki_coverage counts pages tagged 'fn' (functions) as coverage."""
    (tmp_path / "utils.py").write_text("def foo(): pass")
    (tmp_path / "core.py").write_text("def bar(): pass")

    utils_path = str(tmp_path / "utils.py")
    fn_pages = [_make_wiki_page("utils-foo-fn", ["fn", f"source_file:{utils_path}"])]

    mock_wiki = MagicMock()
    mock_wiki.query.side_effect = lambda q, tags=None, max_results=500: (
        fn_pages if tags == ["fn"] else []
    )

    from yadgar.server.tools.wiki_coverage import wiki_coverage

    with patch("yadgar.server._state._wiki", mock_wiki):
        result = wiki_coverage(directory=str(tmp_path))

    assert result["total_modules"] == 2
    assert len(result["covered_modules"]) == 1
    assert len(result["uncovered_modules"]) == 1
    assert result["coverage_pct"] == pytest.approx(0.5)


def test_coverage_via_slug_basename_match(tmp_path):
    """Coverage also matches when wiki page slug contains the module basename."""
    (tmp_path / "recall.py").write_text("# recall module")
    (tmp_path / "unknown.py").write_text("# unknown")

    # No source_file tag — matching by slug basename
    mod_pages = [_make_wiki_page("recall-module-overview", ["mod"])]

    mock_wiki = MagicMock()
    mock_wiki.query.side_effect = lambda q, tags=None, max_results=500: (
        mod_pages if tags == ["mod"] else []
    )

    from yadgar.server.tools.wiki_coverage import wiki_coverage

    with patch("yadgar.server._state._wiki", mock_wiki):
        result = wiki_coverage(directory=str(tmp_path))

    assert result["total_modules"] == 2
    assert len(result["covered_modules"]) == 1
    covered_basenames = {Path(p).name for p in result["covered_modules"]}
    assert "recall.py" in covered_basenames


# ── Test 3: tool registered + callable via import ────────────────────────────


def test_wiki_coverage_importable_from_tools():
    """wiki_coverage is importable from yadgar.server.tools."""
    from yadgar.server.tools import wiki_coverage  # noqa: F401

    assert callable(wiki_coverage)


def test_wiki_coverage_importable_from_server():
    """wiki_coverage is importable from yadgar.server (public API)."""
    import yadgar.server as srv

    assert hasattr(srv, "wiki_coverage")
    assert callable(srv.wiki_coverage)


def test_wiki_coverage_in_tools_all():
    """wiki_coverage appears in yadgar.server.tools.__all__."""
    from yadgar.server import tools

    assert "wiki_coverage" in tools.__all__


# ── Test 4: excludes .venv, tests/, __pycache__ ───────────────────────────────


def test_excludes_venv_directory(tmp_path):
    """Files inside .venv/ are not counted as modules."""
    (tmp_path / "mymodule.py").write_text("# real module")
    venv_dir = tmp_path / ".venv" / "lib" / "python3"
    venv_dir.mkdir(parents=True)
    (venv_dir / "site.py").write_text("# venv site.py — should be excluded")

    from yadgar.server.tools.wiki_coverage import wiki_coverage

    with patch("yadgar.server._state._wiki", None):
        result = wiki_coverage(directory=str(tmp_path))

    assert result["total_modules"] == 1
    assert Path(result["uncovered_modules"][0]).name == "mymodule.py"


def test_excludes_tests_directory(tmp_path):
    """Files inside tests/ subdirectory are excluded from scan."""
    (tmp_path / "app.py").write_text("# application")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_app.py").write_text("# test file — excluded")

    from yadgar.server.tools.wiki_coverage import wiki_coverage

    with patch("yadgar.server._state._wiki", None):
        result = wiki_coverage(directory=str(tmp_path))

    assert result["total_modules"] == 1
    paths = result["uncovered_modules"] + result["covered_modules"]
    assert not any("test_app" in p for p in paths)


def test_excludes_pycache_directory(tmp_path):
    """Files inside __pycache__/ are excluded from scan."""
    (tmp_path / "module.py").write_text("# real")
    cache_dir = tmp_path / "__pycache__"
    cache_dir.mkdir()
    (cache_dir / "module.cpython-312.pyc").write_bytes(b"")
    # Also create a .py there just to be sure it's excluded
    (cache_dir / "cached.py").write_text("# should be excluded")

    from yadgar.server.tools.wiki_coverage import wiki_coverage

    with patch("yadgar.server._state._wiki", None):
        result = wiki_coverage(directory=str(tmp_path))

    assert result["total_modules"] == 1


def test_no_wiki_store_returns_all_uncovered(tmp_path):
    """When wiki store is None, all modules are uncovered."""
    for name in ["a.py", "b.py", "c.py"]:
        (tmp_path / name).write_text("# module")

    from yadgar.server.tools.wiki_coverage import wiki_coverage

    with patch("yadgar.server._state._wiki", None):
        result = wiki_coverage(directory=str(tmp_path))

    assert result["total_modules"] == 3
    assert result["covered_modules"] == []
    assert len(result["uncovered_modules"]) == 3
    assert result["coverage_pct"] == pytest.approx(0.0)
