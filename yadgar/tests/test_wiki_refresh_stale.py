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

from yadgar.core import server


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("wiki_refresh_stale")
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


def _make_wiki_page_singular(base: Path, slug: str, source_file: str, page_hash: str) -> Path:
    """Create one wiki page using the SINGULAR `source_file:` frontmatter field.

    Real repo-wiki pages store `source_file` (singular) — a single path string,
    which may be a FILE or a DIRECTORY (e.g. architecture.md → `yadgar/`,
    overview.md → `.`). The plural `source_files:` list form is only used by the
    older test fixtures.
    """
    wiki_dir = base / ".local-review" / "wiki"
    wiki_dir.mkdir(parents=True, exist_ok=True)
    content = (
        f"---\n"
        f"wiki_schema_version: 2\n"
        f"slug: {slug}\n"
        f"title: {slug}\n"
        f"hash: {page_hash}\n"
        f"source_file: {source_file}\n"
        f"---\n\n# {slug}\n\nContent here.\n"
    )
    (wiki_dir / f"{slug}.md").write_text(content)
    return wiki_dir


# ── master-only enforcement ────────────────────────────────────────────────────


def test_returns_skipped_on_feature_branch(tmp_path):
    """Should return skipped_reason='not_default_branch' on non-default branch."""
    _make_wiki_dir(tmp_path, [])

    with (
        patch("yadgar.core.server._get_current_branch", return_value="feat/something"),
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
        patch("yadgar.core.server._get_current_branch", return_value="feat/v5.0"),
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
        patch("yadgar.core.server._get_current_branch", return_value="feat/something"),
        patch("subprocess.check_output", return_value=b"master"),
    ):
        result = server.wiki_refresh_stale(directory=str(tmp_path), force_branch=True)

    # No skip — forced through
    assert result.get("skipped_reason") is None


def test_force_branch_false_on_master_still_works(tmp_path):
    """On master with force_branch=False, should proceed normally."""
    _make_wiki_dir(tmp_path, [])

    with (
        patch("yadgar.core.server._get_current_branch", return_value="master"),
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
        patch("yadgar.core.server._get_current_branch", return_value="master"),
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
        patch("yadgar.core.server._get_current_branch", return_value="master"),
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
        patch("yadgar.core.server._get_current_branch", return_value="master"),
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
        patch("yadgar.core.server._get_current_branch", return_value="master"),
        patch("subprocess.check_output", return_value=b"refs/remotes/origin/master"),
    ):
        result = server.wiki_refresh_stale(directory=str(tmp_path))

    assert "mod-missing" in result["stale"]


def test_no_wiki_dir_returns_empty_stale(tmp_path):
    """No .local-review/wiki/ directory → returns empty stale list, no error."""
    with (
        patch("yadgar.core.server._get_current_branch", return_value="master"),
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
        patch("yadgar.core.server._get_current_branch", return_value="master"),
        patch("subprocess.check_output", return_value=b"refs/remotes/origin/master"),
    ):
        server.wiki_refresh_stale(directory=str(tmp_path))

    queue_dir = tmp_path / ".local-review" / "wiki" / "refresh-queue"
    assert queue_dir.exists(), "refresh-queue dir should be created"
    files = list(queue_dir.glob("*.json"))
    assert len(files) == 1, "exactly one refresh-queue file expected"

    data = json.loads(files[0].read_text())
    assert "mod-server" in data.get("stale", [])


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
        patch("yadgar.core.server._get_current_branch", return_value="master"),
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
        patch("yadgar.core.server._get_current_branch", return_value="master"),
        patch("subprocess.check_output", return_value=b"refs/remotes/origin/master"),
    ):
        result = server.wiki_refresh_stale(directory=str(tmp_path))

    assert isinstance(result, dict)
    assert "stale" in result


def test_return_dict_has_required_keys(tmp_path):
    """Return value must include 'stale' and 'dispatched_agent_id'."""
    with (
        patch("yadgar.core.server._get_current_branch", return_value="master"),
        patch("subprocess.check_output", return_value=b"refs/remotes/origin/master"),
    ):
        result = server.wiki_refresh_stale(directory=str(tmp_path))

    assert "stale" in result
    assert "dispatched_agent_id" in result
    assert isinstance(result["stale"], list)


# ── Bug #9: singular source_file field + directory-source hashing ──────────────


def test_singular_source_file_field_detected(tmp_path):
    """Real pages store `source_file` (SINGULAR). The staleness scan must read it.

    Prior code only read `source_files`/`sources` (plural), so a page with the
    singular field was never considered for staleness — `stale_wiki_count` was
    pinned at always-0. With a deliberately wrong stored hash, the singular-field
    page must now be detected as stale.
    """
    from yadgar.core.server.tools.project import _compute_source_hash

    src = tmp_path / "yadgar" / "server.py"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("# source code")

    # Sanity: a single FILE source is still hashed via read_bytes.
    file_hash = _compute_source_hash([str(src)], hashlib)
    assert file_hash and file_hash != ""

    _make_wiki_page_singular(tmp_path, "mod-server", str(src), "0" * 64)

    with (
        patch("yadgar.core.server._get_current_branch", return_value="master"),
        patch("subprocess.check_output", return_value=b"refs/remotes/origin/master"),
    ):
        result = server.wiki_refresh_stale(directory=str(tmp_path))

    assert "mod-server" in result["stale"], (
        "singular `source_file` page with wrong hash must be flagged stale"
    )


def test_directory_source_not_always_stale(tmp_path):
    """A page whose `source_file` is a DIRECTORY must not be always-stale.

    Real index pages use directory sources (`yadgar/`, `.`). `Path(dir).read_bytes()`
    raises IsADirectoryError → naive code returns "" → page is ALWAYS stale →
    `stale_wiki_count` flips from always-0 to always-N.

    Correct behaviour (manifest hash over dir contents):
      - unchanged dir  → stored hash == computed hash → NOT stale
      - touch/add file → manifest changes → computed != stored → STALE
    """
    from yadgar.core.server.tools.project import _compute_source_hash

    # Build a directory with a couple of files.
    src_dir = tmp_path / "yadgar"
    (src_dir / "sub").mkdir(parents=True, exist_ok=True)
    (src_dir / "a.py").write_text("# a")
    (src_dir / "sub" / "b.py").write_text("# b")

    # Baseline manifest hash computed by the SAME function — must be non-empty.
    baseline_hash = _compute_source_hash([str(src_dir)], hashlib)
    assert baseline_hash and baseline_hash != "", (
        "directory source must produce a stable non-empty manifest hash"
    )

    _make_wiki_page_singular(tmp_path, "architecture", str(src_dir), baseline_hash)

    # 1) Unchanged directory → NOT stale.
    with (
        patch("yadgar.core.server._get_current_branch", return_value="master"),
        patch("subprocess.check_output", return_value=b"refs/remotes/origin/master"),
    ):
        result = server.wiki_refresh_stale(directory=str(tmp_path))
    assert "architecture" not in result["stale"], (
        "unchanged directory-sourced page must NOT be stale (always-N bug)"
    )

    # 2) Add a new file under the directory → STALE.
    (src_dir / "sub" / "c.py").write_text("# c new file")
    new_hash = _compute_source_hash([str(src_dir)], hashlib)
    assert new_hash != baseline_hash, "manifest hash must change when a file is added"

    with (
        patch("yadgar.core.server._get_current_branch", return_value="master"),
        patch("subprocess.check_output", return_value=b"refs/remotes/origin/master"),
    ):
        result = server.wiki_refresh_stale(directory=str(tmp_path))
    assert "architecture" in result["stale"], (
        "adding a file under the directory source must mark the page stale"
    )


def test_stale_wiki_count_not_always_n_for_dir_source(tmp_path):
    """`_compute_stale_wiki_count` must report 0 for an up-to-date dir-sourced page.

    This is the metric-level regression: a naive field-only fix flips the count
    from always-0 to always-N. With the manifest hash, an unchanged dir-sourced
    page contributes 0.
    """
    from yadgar.core.server.tools.project import _compute_source_hash, _compute_stale_wiki_count

    src_dir = tmp_path / "pkg"
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "m.py").write_text("# module")

    baseline_hash = _compute_source_hash([str(src_dir)], hashlib)
    _make_wiki_page_singular(tmp_path, "overview", str(src_dir), baseline_hash)

    count = _compute_stale_wiki_count(str(tmp_path))
    assert count == 0, f"unchanged dir-sourced page must yield stale_wiki_count==0, got {count}"


def test_directory_manifest_ignores_pycache_churn(tmp_path):
    """__pycache__/*.pyc artifacts must NOT affect the directory manifest hash.

    Those files are rewritten on every interpreter run; if they fed the manifest
    the page would flip to always-stale on a live tree.
    """
    from yadgar.core.server.tools.project import _compute_source_hash

    src_dir = tmp_path / "pkg"
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "m.py").write_text("# module")

    baseline = _compute_source_hash([str(src_dir)], hashlib)

    # Simulate compiled-cache churn under the directory.
    cache = src_dir / "__pycache__"
    cache.mkdir()
    (cache / "m.cpython-313.pyc").write_bytes(b"\x00\x01compiled-bytes")

    after = _compute_source_hash([str(src_dir)], hashlib)
    assert after == baseline, "__pycache__/*.pyc must not change the directory manifest hash"

    # But a real source change still flips it.
    (src_dir / "m.py").write_text("# module CHANGED")
    changed = _compute_source_hash([str(src_dir)], hashlib)
    assert changed != baseline, "a real source edit must still change the manifest hash"


# ── DB-path staleness tests (car #36 store bridge) ────────────────────────────


class TestCheckerDbPath:
    """DB-backed staleness path for built-in page_type='code' pages."""

    def test_checker_db_path_not_stale_when_match(self, tmp_path):
        """DB page with hash matching live file → NOT stale (no .local-review file)."""
        import hashlib

        from yadgar.core.server.tools.project import _scan_stale_wiki_slugs_db

        # Write a source file
        src = tmp_path / "pkg" / "mod.py"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"# module content")

        correct_hash = hashlib.sha256(src.read_bytes()).hexdigest()

        # Insert a page into DB with page_type="code", hash, source_file
        from yadgar._shared.runtime.lifecycle import _get_storage

        storage = _get_storage()
        storage.insert_wiki_page(
            {
                "slug": "mod-pkg-mod",
                "title": "pkg.mod",
                "content": "# pkg.mod",
                "tags": ["code-structure", "module"],
                "category": "code",
                "page_type": "code",
                "directory_context": str(tmp_path),
                "hash": correct_hash,
                "source_file": str(src),
            }
        )

        stale = _scan_stale_wiki_slugs_db(str(tmp_path))
        assert "mod-pkg-mod" not in stale, "DB page with matching hash must NOT be stale"

    def test_checker_db_path_stale_on_drift(self, tmp_path):
        """DB page with hash not matching live file → stale."""
        from yadgar._shared.runtime.lifecycle import _get_storage
        from yadgar.core.server.tools.project import _scan_stale_wiki_slugs_db

        src = tmp_path / "pkg" / "mod2.py"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"# original content")

        storage = _get_storage()
        storage.insert_wiki_page(
            {
                "slug": "mod-pkg-mod2",
                "title": "pkg.mod2",
                "content": "# pkg.mod2",
                "tags": ["code-structure", "module"],
                "category": "code",
                "page_type": "code",
                "directory_context": str(tmp_path),
                "hash": "0" * 64,  # deliberately wrong
                "source_file": str(src),
            }
        )

        stale = _scan_stale_wiki_slugs_db(str(tmp_path))
        assert "mod-pkg-mod2" in stale, "DB page with wrong hash must be stale"

    def test_checker_db_path_stale_when_source_changes(self, tmp_path):
        """Mutate source file → DB page becomes stale."""
        import hashlib

        from yadgar._shared.runtime.lifecycle import _get_storage
        from yadgar.core.server.tools.project import _scan_stale_wiki_slugs_db

        src = tmp_path / "pkg" / "mod3.py"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"# original")

        original_hash = hashlib.sha256(src.read_bytes()).hexdigest()

        storage = _get_storage()
        storage.insert_wiki_page(
            {
                "slug": "mod-pkg-mod3",
                "title": "pkg.mod3",
                "content": "# pkg.mod3",
                "tags": ["code-structure"],
                "category": "code",
                "page_type": "code",
                "directory_context": str(tmp_path),
                "hash": original_hash,
                "source_file": str(src),
            }
        )

        # Not stale yet
        assert "mod-pkg-mod3" not in _scan_stale_wiki_slugs_db(str(tmp_path))

        # Mutate source
        src.write_bytes(b"# CHANGED")
        assert "mod-pkg-mod3" in _scan_stale_wiki_slugs_db(str(tmp_path))

    def test_external_fn_page_not_stale_not_tracked(self, tmp_path):
        """External fn pages (entity_id: fn:*) on disk must NOT be flagged stale.

        The checker can't reproduce SHA256(sig+body) per-function hashing, so
        these pages are classified 'external-sourced, not tracked' — not stale.
        """
        # Create a disk fn page with entity_id: fn:* (wrong hash would normally trigger)
        wiki_dir = tmp_path / ".local-review" / "wiki"
        wiki_dir.mkdir(parents=True, exist_ok=True)
        src = tmp_path / "pkg" / "fn.py"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"def foo(): pass")

        content = (
            f"---\n"
            f"wiki_schema_version: 2\n"
            f"slug: fn-pkg-foo\n"
            f"entity_id: fn:pkg.foo\n"
            f"hash: {'0' * 64}\n"
            f"source_file: {src}\n"
            f"---\n\n# fn:pkg.foo\n"
        )
        (wiki_dir / "fn-pkg-foo.md").write_text(content)

        # Run the full stale scan (disk path) — fn pages must NOT appear
        from yadgar.core.server.tools.project import _scan_stale_wiki_slugs

        stale = _scan_stale_wiki_slugs(str(tmp_path))
        assert "fn-pkg-foo" not in stale, (
            "External fn page (entity_id: fn:*) must not be flagged stale — "
            "checker cannot reproduce per-function hash"
        )
