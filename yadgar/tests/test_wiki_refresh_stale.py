"""Tests for §26 wiki_refresh_stale MCP tool.

TDD — these tests are written BEFORE the implementation.
They cover:
- master-only enforcement (non-default branch blocked)
- force_branch override
- hash drift detection (stale pages found)
- refresh-queue file written on drift
- tool NEVER raises
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from yadgar import server

pytestmark = pytest.mark.xdist_group("server_globals")


@pytest.fixture(autouse=True)
def _engines(tmp_path):
    db_path = str(tmp_path / "test.db")
    server.init_engines(db_path=db_path, embedding_model="all-MiniLM-L6-v2")
    yield
    server.shutdown()


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_wiki_dir(base: Path, pages: list[dict]) -> Path:
    """Create .local-review/wiki/*.md files with frontmatter.

    Each page dict:
        slug, source_files (list of paths), hash (sha256 hex or wrong hash)
    """
    wiki_dir = base / ".local-review" / "wiki"
    wiki_dir.mkdir(parents=True, exist_ok=True)
    for page in pages:
        slug = page["slug"]
        source_files_yaml = "\n".join(f"  - {sf}" for sf in page.get("source_files", []))
        content = (
            f"---\n"
            f"wiki_schema_version: 2\n"
            f"slug: {slug}\n"
            f"title: {page.get('title', slug)}\n"
            f"hash: {page['hash']}\n"
            f"source_files:\n{source_files_yaml}\n"
            f"---\n\n# {page.get('title', slug)}\n\nContent here.\n"
        )
        (wiki_dir / f"{slug}.md").write_text(content)
    return wiki_dir


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ── master-only enforcement ────────────────────────────────────────────────────


def test_returns_skipped_on_feature_branch(tmp_path):
    """Should return skipped_reason='not_default_branch' on non-default branch."""
    _make_wiki_dir(tmp_path, [])

    with (
        patch("yadgar.server._get_current_branch", return_value="feat/something"),
        patch(
            "subprocess.check_output",
            side_effect=lambda cmd, **kw: b"master" if "symbolic-ref" in cmd else b"feat/something",
        ),
    ):
        result = server.wiki_refresh_stale(directory=str(tmp_path))

    assert result["skipped_reason"] == "not_default_branch"
    assert result["stale"] == []


def test_returns_skipped_when_branch_is_feature(tmp_path):
    """Any branch not in (master, main, default) returns skipped."""
    _make_wiki_dir(tmp_path, [])

    with (
        patch("yadgar.server._get_current_branch", return_value="feat/v5.0"),
        patch(
            "subprocess.check_output",
            side_effect=lambda cmd, **kw: b"master",
        ),
    ):
        result = server.wiki_refresh_stale(directory=str(tmp_path))

    assert result["skipped_reason"] == "not_default_branch"


# ── force_branch override ─────────────────────────────────────────────────────


def test_force_branch_overrides_enforcement(tmp_path):
    """force_branch=True should bypass the master-only check."""
    _make_wiki_dir(tmp_path, [])

    with (
        patch("yadgar.server._get_current_branch", return_value="feat/something"),
        patch("subprocess.check_output", return_value=b"master"),
    ):
        result = server.wiki_refresh_stale(directory=str(tmp_path), force_branch=True)

    # No skip — forced through
    assert result.get("skipped_reason") is None


def test_force_branch_false_on_master_still_works(tmp_path):
    """On master with force_branch=False, should proceed normally."""
    _make_wiki_dir(tmp_path, [])

    with (
        patch("yadgar.server._get_current_branch", return_value="master"),
        patch("subprocess.check_output", return_value=b"refs/remotes/origin/master"),
    ):
        result = server.wiki_refresh_stale(directory=str(tmp_path))

    assert result.get("skipped_reason") is None


# ── hash drift detection ──────────────────────────────────────────────────────


def test_no_stale_when_hashes_match(tmp_path):
    """No stale pages when all hashes match the source files."""
    # Create source file
    src = tmp_path / "yadgar" / "server.py"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("# source code")

    correct_hash = _sha256_file(src)
    _make_wiki_dir(
        tmp_path, [{"slug": "mod-server", "source_files": [str(src)], "hash": correct_hash}]
    )

    with (
        patch("yadgar.server._get_current_branch", return_value="master"),
        patch("subprocess.check_output", return_value=b"refs/remotes/origin/master"),
    ):
        result = server.wiki_refresh_stale(directory=str(tmp_path))

    assert result["stale"] == []


def test_detects_stale_when_hash_mismatch(tmp_path):
    """Stale page reported when stored hash ≠ computed SHA256."""
    src = tmp_path / "yadgar" / "server.py"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("# source code")

    wrong_hash = "0" * 64  # definitely wrong
    _make_wiki_dir(
        tmp_path,
        [{"slug": "mod-server", "source_files": [str(src)], "hash": wrong_hash}],
    )

    with (
        patch("yadgar.server._get_current_branch", return_value="master"),
        patch("subprocess.check_output", return_value=b"refs/remotes/origin/master"),
    ):
        result = server.wiki_refresh_stale(directory=str(tmp_path))

    assert "mod-server" in result["stale"]


def test_detects_multiple_stale_pages(tmp_path):
    """Multiple stale pages all reported."""
    src1 = tmp_path / "a.py"
    src2 = tmp_path / "b.py"
    src1.write_text("# a")
    src2.write_text("# b")

    _make_wiki_dir(
        tmp_path,
        [
            {"slug": "mod-a", "source_files": [str(src1)], "hash": "bad" * 20 + "bad"},
            {"slug": "mod-b", "source_files": [str(src2)], "hash": "bbb" * 21 + "b"},
        ],
    )

    with (
        patch("yadgar.server._get_current_branch", return_value="master"),
        patch("subprocess.check_output", return_value=b"refs/remotes/origin/master"),
    ):
        result = server.wiki_refresh_stale(directory=str(tmp_path))

    assert "mod-a" in result["stale"]
    assert "mod-b" in result["stale"]


def test_missing_source_file_marks_stale(tmp_path):
    """If source file listed in frontmatter doesn't exist → page is stale."""
    _make_wiki_dir(
        tmp_path,
        [
            {
                "slug": "mod-missing",
                "source_files": [str(tmp_path / "nonexistent.py")],
                "hash": "abc123",
            }
        ],
    )

    with (
        patch("yadgar.server._get_current_branch", return_value="master"),
        patch("subprocess.check_output", return_value=b"refs/remotes/origin/master"),
    ):
        result = server.wiki_refresh_stale(directory=str(tmp_path))

    assert "mod-missing" in result["stale"]


def test_no_wiki_dir_returns_empty_stale(tmp_path):
    """No .local-review/wiki/ directory → returns empty stale list, no error."""
    with (
        patch("yadgar.server._get_current_branch", return_value="master"),
        patch("subprocess.check_output", return_value=b"refs/remotes/origin/master"),
    ):
        result = server.wiki_refresh_stale(directory=str(tmp_path))

    assert result["stale"] == []
    assert result.get("skipped_reason") is None


# ── refresh-queue file written ────────────────────────────────────────────────


def test_refresh_queue_file_written_on_drift(tmp_path):
    """When stale pages found, a JSON file is written to .local-review/wiki/refresh-queue/."""
    src = tmp_path / "yadgar" / "server.py"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("# source")

    _make_wiki_dir(
        tmp_path,
        [{"slug": "mod-server", "source_files": [str(src)], "hash": "wrong" * 12 + "wron"}],
    )

    with (
        patch("yadgar.server._get_current_branch", return_value="master"),
        patch("subprocess.check_output", return_value=b"refs/remotes/origin/master"),
    ):
        server.wiki_refresh_stale(directory=str(tmp_path))

    queue_dir = tmp_path / ".local-review" / "wiki" / "refresh-queue"
    assert queue_dir.exists(), "refresh-queue dir should be created"
    files = list(queue_dir.glob("*.json"))
    assert len(files) == 1, "exactly one refresh-queue file expected"

    data = json.loads(files[0].read_text())
    assert "mod-server" in data.get("stale_slugs", [])


def test_refresh_queue_not_written_when_no_drift(tmp_path):
    """No refresh-queue file if no stale pages."""
    src = tmp_path / "src.py"
    src.write_text("code")
    correct_hash = _sha256_file(src)

    _make_wiki_dir(
        tmp_path,
        [{"slug": "mod-ok", "source_files": [str(src)], "hash": correct_hash}],
    )

    with (
        patch("yadgar.server._get_current_branch", return_value="master"),
        patch("subprocess.check_output", return_value=b"refs/remotes/origin/master"),
    ):
        server.wiki_refresh_stale(directory=str(tmp_path))

    queue_dir = tmp_path / ".local-review" / "wiki" / "refresh-queue"
    if queue_dir.exists():
        files = list(queue_dir.glob("*.json"))
        assert len(files) == 0, "no queue file when no drift"


# ── tool never raises ─────────────────────────────────────────────────────────


def test_never_raises_on_bad_directory(tmp_path):
    """wiki_refresh_stale must not raise even for nonexistent directory."""
    result = server.wiki_refresh_stale(directory="/nonexistent/path/that/does/not/exist")
    assert isinstance(result, dict)
    assert "stale" in result


def test_never_raises_on_malformed_frontmatter(tmp_path):
    """wiki_refresh_stale must not raise on corrupted frontmatter YAML."""
    wiki_dir = tmp_path / ".local-review" / "wiki"
    wiki_dir.mkdir(parents=True, exist_ok=True)
    (wiki_dir / "bad.md").write_text("---\nnot: valid: yaml: {\nbad\n---\n\ncontent")

    with (
        patch("yadgar.server._get_current_branch", return_value="master"),
        patch("subprocess.check_output", return_value=b"refs/remotes/origin/master"),
    ):
        result = server.wiki_refresh_stale(directory=str(tmp_path))

    assert isinstance(result, dict)
    assert "stale" in result


def test_return_dict_has_required_keys(tmp_path):
    """Return value must include 'stale' and 'dispatched_agent_id'."""
    with (
        patch("yadgar.server._get_current_branch", return_value="master"),
        patch("subprocess.check_output", return_value=b"refs/remotes/origin/master"),
    ):
        result = server.wiki_refresh_stale(directory=str(tmp_path))

    assert "stale" in result
    assert "dispatched_agent_id" in result
    assert isinstance(result["stale"], list)
