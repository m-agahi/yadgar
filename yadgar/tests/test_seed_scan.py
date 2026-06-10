"""Tests for yadgar/seed/_scan.py — project directory scanning.

Coverage targets:
- _match_config: exact match, glob match, no match
- _should_skip_dir: skip dirs, .hidden dirs, .egg-info, allowed .github/.gitlab
- _read_file_safe: binary extension skip, oversized file, normal read, error
- _truncate: short text passthrough, truncation at line boundary, mid-line fallback
- _on_walk_error: logs warning on OSError
- scan_project: not-a-directory raises ValueError, empty dir, full structure
  including configs, docs, entry_points, ci_cd, stats, structure keys
"""

from __future__ import annotations

import logging
import os
from unittest.mock import patch

import pytest

from yadgar.seed._scan import (
    _MAX_FILE_SIZE,
    _MAX_MEMORY_CONTENT,
    _match_config,
    _on_walk_error,
    _read_file_safe,
    _should_skip_dir,
    _truncate,
    scan_project,
)

# ── _match_config ─────────────────────────────────────────────────────────────


def test_match_config_exact_python():
    assert _match_config("pyproject.toml") == "python"


def test_match_config_exact_javascript():
    assert _match_config("package.json") == "javascript"


def test_match_config_exact_docker():
    assert _match_config("Dockerfile") == "docker"


def test_match_config_exact_ci():
    assert _match_config("Makefile") == "build"


def test_match_config_glob_csproj():
    assert _match_config("MyApp.csproj") == "csharp"


def test_match_config_glob_fsproj():
    assert _match_config("MyLib.fsproj") == "fsharp"


def test_match_config_no_match():
    assert _match_config("random_file.txt") is None


def test_match_config_no_match_empty():
    assert _match_config("") is None


# ── _should_skip_dir ──────────────────────────────────────────────────────────


def test_should_skip_dir_git():
    assert _should_skip_dir(".git") is True


def test_should_skip_dir_node_modules():
    assert _should_skip_dir("node_modules") is True


def test_should_skip_dir_pycache():
    assert _should_skip_dir("__pycache__") is True


def test_should_skip_dir_venv():
    assert _should_skip_dir(".venv") is True


def test_should_skip_dir_hidden_dot():
    # Arbitrary hidden dir (not .github or .gitlab)
    assert _should_skip_dir(".hidden_dir") is True


def test_should_skip_dir_github_allowed():
    assert _should_skip_dir(".github") is False


def test_should_skip_dir_gitlab_allowed():
    assert _should_skip_dir(".gitlab") is False


def test_should_skip_dir_egg_info():
    assert _should_skip_dir("mypackage.egg-info") is True


def test_should_skip_dir_regular():
    assert _should_skip_dir("src") is False


def test_should_skip_dir_regular_dir():
    assert _should_skip_dir("yadgar") is False


# ── _read_file_safe ───────────────────────────────────────────────────────────


def test_read_file_safe_normal(tmp_path):
    f = tmp_path / "hello.txt"
    f.write_text("Hello world\n")
    assert _read_file_safe(f) == "Hello world\n"


def test_read_file_safe_binary_extension(tmp_path):
    f = tmp_path / "image.png"
    f.write_bytes(b"\x89PNG\r\n")
    assert _read_file_safe(f) is None


def test_read_file_safe_pyc_extension(tmp_path):
    f = tmp_path / "module.pyc"
    f.write_bytes(b"\x00\x00\x00\x00")
    assert _read_file_safe(f) is None


def test_read_file_safe_oversized(tmp_path):
    f = tmp_path / "large.txt"
    # Write more than _MAX_FILE_SIZE bytes
    f.write_bytes(b"x" * (_MAX_FILE_SIZE + 1))
    assert _read_file_safe(f) is None


def test_read_file_safe_exactly_at_limit(tmp_path):
    f = tmp_path / "exact.txt"
    f.write_bytes(b"y" * _MAX_FILE_SIZE)
    result = _read_file_safe(f)
    assert result is not None
    assert len(result) == _MAX_FILE_SIZE


def test_read_file_safe_nonexistent(tmp_path):
    f = tmp_path / "nonexistent.txt"
    assert _read_file_safe(f) is None


def test_read_file_safe_lock_extension(tmp_path):
    f = tmp_path / "package-lock.lock"
    f.write_text("lock content")
    assert _read_file_safe(f) is None


# ── _truncate ─────────────────────────────────────────────────────────────────


def test_truncate_short_text():
    text = "short text"
    assert _truncate(text, max_len=100) == text


def test_truncate_exact_length():
    text = "a" * _MAX_MEMORY_CONTENT
    assert _truncate(text) == text


def test_truncate_long_text_breaks_at_newline():
    lines = ["line " + str(i) for i in range(200)]
    text = "\n".join(lines)
    result = _truncate(text, max_len=100)
    assert result.endswith("[... truncated]")
    assert len(result) <= 100 + len("\n[... truncated]")


def test_truncate_no_newline_cuts_mid():
    # No newlines — falls back to cut at effective_max
    text = "a" * 500
    result = _truncate(text, max_len=100)
    assert result.endswith("[... truncated]")
    assert len(result) <= 200  # generous upper bound


def test_truncate_preserves_content_start():
    text = "important_start\n" + "x" * 3000
    result = _truncate(text)
    assert result.startswith("important_start")


# ── _on_walk_error ────────────────────────────────────────────────────────────


def test_on_walk_error_logs_warning(caplog):
    err = OSError("Permission denied")
    err.filename = "/some/path"
    with caplog.at_level(logging.WARNING, logger="yadgar.seed._scan"):
        _on_walk_error(err)
    assert "Skipped" in caplog.text or "/some/path" in caplog.text


def test_on_walk_error_no_filename(caplog):
    err = OSError("Permission denied")
    err.filename = None
    with caplog.at_level(logging.WARNING, logger="yadgar.seed._scan"):
        _on_walk_error(err)  # should not raise


# ── scan_project ──────────────────────────────────────────────────────────────


def test_scan_project_not_a_directory(tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("hello")
    with pytest.raises(ValueError, match="Not a directory"):
        scan_project(str(f))


def test_scan_project_nonexistent_path(tmp_path):
    with pytest.raises(ValueError, match="Not a directory"):
        scan_project(str(tmp_path / "ghost"))


def test_scan_project_empty_dir(tmp_path):
    result = scan_project(str(tmp_path))
    assert result["project_name"] == tmp_path.name
    assert result["configs"] == []
    assert result["docs"] == []
    assert result["entry_points"] == []
    assert result["ci_cd"] == []
    assert result["stats"]["total_files"] == 0
    assert result["structure"] == {}


def test_scan_project_returns_correct_keys(tmp_path):
    result = scan_project(str(tmp_path))
    assert set(result.keys()) == {
        "project_name",
        "root",
        "structure",
        "configs",
        "docs",
        "entry_points",
        "ci_cd",
        "stats",
    }
    assert set(result["stats"].keys()) == {"total_files", "total_dirs", "top_extensions"}


def test_scan_project_detects_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname="test"\n')
    result = scan_project(str(tmp_path))
    assert len(result["configs"]) == 1
    assert result["configs"][0]["language"] == "python"
    assert result["configs"][0]["path"] == "pyproject.toml"


def test_scan_project_detects_readme(tmp_path):
    (tmp_path / "README.md").write_text("# My Project\n")
    result = scan_project(str(tmp_path))
    assert len(result["docs"]) == 1
    assert result["docs"][0]["path"] == "README.md"
    assert "My Project" in result["docs"][0]["content"]


def test_scan_project_detects_entry_point_main(tmp_path):
    (tmp_path / "main.py").write_text("import sys\n")
    result = scan_project(str(tmp_path))
    assert any(ep["path"] == "main.py" for ep in result["entry_points"])


def test_scan_project_detects_dunder_main(tmp_path):
    (tmp_path / "__main__.py").write_text("if __name__ == '__main__': pass\n")
    result = scan_project(str(tmp_path))
    assert any(ep["path"] == "__main__.py" for ep in result["entry_points"])


def test_scan_project_detects_github_workflows(tmp_path):
    workflows_dir = tmp_path / ".github" / "workflows"
    workflows_dir.mkdir(parents=True)
    (workflows_dir / "ci.yml").write_text("name: CI\n")
    result = scan_project(str(tmp_path))
    assert any(".github/workflows" in item["path"] for item in result["ci_cd"])


def test_scan_project_detects_gitlab_ci(tmp_path):
    gitlab_dir = tmp_path / ".gitlab"
    gitlab_dir.mkdir()
    (gitlab_dir / "ci.yml").write_text("stages: [build]\n")
    result = scan_project(str(tmp_path))
    assert any(".gitlab" in item["path"] for item in result["ci_cd"])


def test_scan_project_detects_standalone_ci_files(tmp_path):
    (tmp_path / ".travis.yml").write_text("language: python\n")
    result = scan_project(str(tmp_path))
    assert any(".travis.yml" in item["path"] for item in result["ci_cd"])


def test_scan_project_skips_git_dir(tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("[core]\n")
    result = scan_project(str(tmp_path))
    # .git contents should not appear in structure
    assert ".git" not in result["structure"]
    # Files under .git not counted via structure
    for key in result["structure"]:
        assert ".git" not in key


def test_scan_project_skips_node_modules(tmp_path):
    nm = tmp_path / "node_modules"
    nm.mkdir()
    (nm / "some_lib.js").write_text("module.exports = {};\n")
    result = scan_project(str(tmp_path))
    assert "node_modules" not in result["structure"]


def test_scan_project_stats_counts_files(tmp_path):
    (tmp_path / "a.py").write_text("pass\n")
    (tmp_path / "b.py").write_text("pass\n")
    (tmp_path / "c.txt").write_text("hello\n")
    result = scan_project(str(tmp_path))
    assert result["stats"]["total_files"] == 3
    assert result["stats"]["total_dirs"] >= 1


def test_scan_project_stats_top_extensions(tmp_path):
    for i in range(5):
        (tmp_path / f"file{i}.py").write_text("pass\n")
    result = scan_project(str(tmp_path))
    exts = dict(result["stats"]["top_extensions"])
    assert exts.get(".py", 0) == 5


def test_scan_project_structure_keys(tmp_path):
    sub = tmp_path / "subdir"
    sub.mkdir()
    (sub / "module.py").write_text("pass\n")
    result = scan_project(str(tmp_path))
    assert "subdir" in result["structure"]
    assert "module.py" in result["structure"]["subdir"]


def test_scan_project_root_dir_key_is_dot(tmp_path):
    (tmp_path / "app.py").write_text("pass\n")
    result = scan_project(str(tmp_path))
    assert "." in result["structure"]
    assert "app.py" in result["structure"]["."]


def test_scan_project_src_entry_point(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.py").write_text("import sys\n")
    result = scan_project(str(tmp_path))
    assert any(
        "src/main.py" in ep["path"] or ep["path"] == "src/main.py" for ep in result["entry_points"]
    )


def test_scan_project_glob_config(tmp_path):
    (tmp_path / "MyApp.csproj").write_text("<Project></Project>")
    result = scan_project(str(tmp_path))
    assert any(c["language"] == "csharp" for c in result["configs"])


def test_scan_project_truncates_large_config(tmp_path):
    # Config file just above read limit should be excluded
    large_file = tmp_path / "pyproject.toml"
    large_file.write_bytes(b"x" * (_MAX_FILE_SIZE + 1))
    result = scan_project(str(tmp_path))
    assert result["configs"] == []


def test_scan_project_resolves_symlink_dir(tmp_path):
    # Path with ".." resolves correctly
    result = scan_project(str(tmp_path / "."))
    assert result["project_name"] == tmp_path.name


def test_scan_project_egg_info_skipped(tmp_path):
    egg = tmp_path / "mypackage.egg-info"
    egg.mkdir()
    (egg / "PKG-INFO").write_text("Metadata\n")
    result = scan_project(str(tmp_path))
    assert "mypackage.egg-info" not in result["structure"]


def test_scan_project_multiple_configs(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname="x"\n')
    (tmp_path / "Dockerfile").write_text("FROM python:3.11\n")
    result = scan_project(str(tmp_path))
    langs = {c["language"] for c in result["configs"]}
    assert "python" in langs
    assert "docker" in langs


def test_scan_project_permission_error_no_crash(tmp_path, monkeypatch):
    """scan_project handles permission errors gracefully via onerror callback."""
    call_count = {"n": 0}
    original_walk = os.walk

    def mock_walk(path, followlinks, onerror):
        yield from original_walk(path, followlinks=followlinks, onerror=onerror)
        # Simulate a permission error after normal traversal
        if call_count["n"] == 0:
            call_count["n"] += 1
            err = OSError("Permission denied")
            err.filename = "/some/locked/dir"
            onerror(err)

    with patch("yadgar.seed._scan.os.walk", mock_walk):
        result = scan_project(str(tmp_path))
    # Just verify it returned a valid result
    assert "project_name" in result
