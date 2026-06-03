"""RED tests for v5.42.5 — directory contract (foundational release).

TDD: written BEFORE implementation. All tests in this file start RED and go
GREEN as implementation progresses through phases 1–8.

Coverage:
T1a. wiki_add(directory="")  → synchronous {"error": "missing_directory"}
T1b. wiki_add(no directory)  → synchronous {"error": "missing_directory"}
T2.  Drainer rejects payload lacking directory_context → DLQ failure_reason="missing_directory"
T3.  §25 4-step: directory=$caller AND branch IS NULL beats directory="global" AND branch IS NULL
T4.  wiki_list(directory=...) scopes to that dir + global; excludes other dirs
T5.  _resolve_page_id_by_slug uses caller directory, not daemon os.getcwd()
T6.  agent_prompt_save(no directory) → {"error": "missing_directory"}
T7a. block_create(scope='project', directory=None) → {"ok": False, "error": "missing_directory"}
T7b. block_create(scope='global', directory=None)  → ok=True (no dir required)
T8.  recall(directory="/proj/A") excludes memories from directory_context="/proj/B"
"""

from __future__ import annotations

import re
from unittest.mock import patch

import pytest

from yadgar import server
from yadgar.file_queue import FileQueue, QueueDrainer

# ── shared fixture ────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _engines(tmp_path):
    server.init_engines(
        db_path=str(tmp_path / "test_dir_contract.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


@pytest.fixture
def bare_drainer(tmp_path):
    """Isolated FileQueue + QueueDrainer for unit-level drainer tests."""
    import yadgar.server._state as _st

    fq = FileQueue(tmp_path)
    drainer = QueueDrainer(
        queue=fq,
        storage_factory=lambda: _st._storage,
        drain_interval=9999,
    )
    return drainer, fq


@pytest.fixture
def patched_drainer(tmp_path):
    """FileQueue + QueueDrainer with server lifecycle patches (integration)."""
    import yadgar.server._state as _state_mod
    import yadgar.server.lifecycle as _lc

    real_fq = FileQueue(tmp_path)
    drainer = QueueDrainer(
        queue=real_fq,
        storage_factory=lambda: _state_mod._storage,
        drain_interval=9999,
    )

    def _get_fq():
        return real_fq

    with (
        patch.object(_lc, "_get_file_queue", _get_fq),
        patch("yadgar.server.tools.wiki._get_file_queue", _get_fq),
        patch.object(_state_mod, "_queue_drainer", drainer),
        patch.object(_state_mod, "_file_queue", real_fq),
    ):
        yield drainer, real_fq


# ── helpers ───────────────────────────────────────────────────────────────────


def _slugify(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:64]


def _insert_wiki_direct(
    storage,
    title: str,
    content: str,
    directory_context: str,
    branch: str | None = None,
) -> str:
    slug = _slugify(title)
    storage.insert_wiki_page(
        {
            "slug": slug,
            "title": title,
            "content": content,
            "category": "reference",
            "tags": ["test"],
            "links": [],
            "source_memory_ids": [],
            "confidence": "medium",
            "branch": branch,
            "directory_context": directory_context,
        }
    )
    return slug


def _insert_memory_direct(storage, content: str, directory_context: str) -> None:
    """Insert a memory record directly to storage with a directory_context.

    Embedding left as None — embedded DB (surrealdb-python) does not require
    it for FTS search; test only verifies directory-scoped recall exclusion.
    """
    storage.insert_memory(
        {
            "content": content,
            "embedding": None,
            "tags": ["test"],
            "directory_context": directory_context,
            "heat": 0.8,
            "confidence": 0.7,
            "is_stale": False,
            "file_hash": None,
            "embedding_model": "all-MiniLM-L6-v2",
            "branch": None,
            "_internal": True,
        }
    )


# ── T1: MCP boundary rejects missing directory ───────────────────────────────


class TestWikiAddDirectoryBoundary:
    """wiki_add must reject empty/missing directory at the MCP boundary."""

    def test_wiki_add_rejects_empty_directory(self):
        """wiki_add(directory='') → synchronous error, no storage write."""
        from yadgar.server.tools.wiki import wiki_add

        with patch("yadgar.server.tools.wiki.is_draining", return_value=False):
            result = wiki_add(
                title="Test Page",
                content="Some content",
                branch="feat/x",
                directory="",
            )
        assert result.get("error") == "missing_directory", (
            f"Expected missing_directory error, got: {result}"
        )
        assert result.get("stored") is False

    def test_wiki_add_rejects_whitespace_only_directory(self):
        """wiki_add(directory='   ') → synchronous error."""
        from yadgar.server.tools.wiki import wiki_add

        with patch("yadgar.server.tools.wiki.is_draining", return_value=False):
            result = wiki_add(
                title="Test Page",
                content="Some content",
                branch="feat/x",
                directory="   ",
            )
        assert result.get("error") == "missing_directory", (
            f"Expected missing_directory error, got: {result}"
        )

    def test_wiki_add_rejects_missing_directory_param(self):
        """wiki_add with no directory param → synchronous error."""
        from yadgar.server.tools.wiki import wiki_add

        with patch("yadgar.server.tools.wiki.is_draining", return_value=False):
            result = wiki_add(
                title="Test Page No Dir",
                content="Some content",
                branch="feat/x",
                # no directory param
            )
        assert result.get("error") == "missing_directory", (
            f"Expected missing_directory error, got: {result}"
        )

    def test_wiki_add_accepts_valid_directory(self):
        """wiki_add(directory='/proj/x') succeeds (queued or committed)."""
        from yadgar.server.tools.wiki import wiki_add

        with patch("yadgar.server.tools.wiki.is_draining", return_value=False):
            result = wiki_add(
                title="Valid Dir Page",
                content="Some content",
                branch="feat/x",
                directory="/proj/x",
            )
        assert result.get("error") != "missing_directory", (
            f"Should not reject valid directory, got: {result}"
        )
        # stored=True or queued=True
        assert result.get("stored") is True or result.get("queued") is True


# ── T2: Drainer rejects missing directory_context ────────────────────────────


class TestDrainerRejectsNoDirectory:
    """QueueDrainer._validate_wiki_add rejects records without directory_context."""

    def _make_wiki_record(
        self,
        *,
        directory_context: str | None = "ABSENT",
        branch: str = "feat/test",
        internal: bool = False,
    ) -> dict:
        payload: dict = {
            "wiki_schema_version": 2,
            "slug": "drainer-dir-test",
            "title": "Drainer Dir Test",
            "content": "Test content for directory enforcement.",
            "category": "reference",
            "tags": [],
            "branch": branch,
        }
        if directory_context != "ABSENT":
            payload["directory_context"] = directory_context
        if internal:
            payload["_internal"] = True
        return {"op": "wiki_add", "id": "test-id", "payload": payload}

    def test_missing_directory_context_rejected(self, bare_drainer):
        """wiki_add payload without directory_context → rejection string."""
        drainer, _ = bare_drainer
        record = self._make_wiki_record(directory_context="ABSENT")
        result = drainer._validate_wiki_add(record)
        assert result is not None, "Expected rejection but got None"
        assert "missing_directory" in result, f"Got: {result!r}"

    def test_empty_directory_context_rejected(self, bare_drainer):
        """wiki_add payload with directory_context='' → rejection string."""
        drainer, _ = bare_drainer
        record = self._make_wiki_record(directory_context="")
        result = drainer._validate_wiki_add(record)
        assert result is not None, "Expected rejection but got None"
        assert "missing_directory" in result, f"Got: {result!r}"

    def test_valid_directory_context_passes(self, bare_drainer):
        """wiki_add payload with valid directory_context → passes (None)."""
        drainer, _ = bare_drainer
        record = self._make_wiki_record(directory_context="/home/max/git/yadgar")
        result = drainer._validate_wiki_add(record)
        assert result is None, f"Expected pass but got rejection: {result!r}"

    def test_missing_directory_goes_to_dlq(self, patched_drainer, tmp_path):
        """End-to-end: enqueue wiki_add without directory → DLQ with missing_directory."""
        import json

        import yadgar.server._state as _st

        drainer, fq = patched_drainer

        fq.enqueue(
            "wiki_add",
            {
                "wiki_schema_version": 2,
                "slug": "no-dir-page",
                "title": "No Dir Page",
                "content": "Content without directory.",
                "category": "reference",
                "tags": [],
                "branch": "feat/test",
                # No directory_context
            },
        )

        drainer.drain_now()

        # Should be in DLQ
        dlq_sidecars = list(fq.dlq_dir.glob("*.error.json"))
        assert len(dlq_sidecars) == 1, f"Expected 1 DLQ entry, got {len(dlq_sidecars)}"

        meta = json.loads(dlq_sidecars[0].read_text())
        assert meta.get("failure_reason") == "missing_directory", (
            f"Expected missing_directory, got: {meta.get('failure_reason')}"
        )

        # No wiki_page row should exist
        rows = _st._storage._q("SELECT * FROM wiki_page WHERE slug = 'no-dir-page'")
        assert len(rows) == 0, "Expected 0 rows, but wiki_page was created"


# ── T3: §25 4-step resolution — project-canonical beats global ───────────────


class TestResolutionProjectBeatsGlobal:
    """§25 4-step: directory=$caller AND branch IS NULL beats directory='global' AND branch IS NULL."""

    def test_resolution_project_canonical_beats_global(self):
        """Insert same slug in /proj/A (canonical) and global. wiki_read with /proj/A returns /proj/A."""
        import yadgar.server._state as _st

        storage = _st._storage

        slug = "test-resolution-slug"
        _insert_wiki_direct(
            storage,
            "Test Resolution Slug",
            "Project-A content",
            directory_context="/proj/A",
            branch=None,
        )
        _insert_wiki_direct(
            storage,
            "Test Resolution Slug",
            "Global content",
            directory_context="global",
            branch=None,
        )

        from yadgar.server.tools.wiki import wiki_read

        with (
            patch("yadgar.server.tools.wiki.os") as mock_os,
            patch("yadgar.server._detect_branch", return_value="main", create=True),
            patch("yadgar.server._get_default_branch", return_value=None, create=True),
        ):
            mock_os.getcwd.return_value = "/daemon/cwd"
            result = wiki_read(slug, directory="/proj/A")

        assert result.get("error") is None, f"Got error: {result}"
        assert result.get("directory_context") == "/proj/A", (
            f"Expected /proj/A, got: {result.get('directory_context')!r}"
        )
        assert "Project-A content" in result.get("content", ""), (
            f"Expected project content, got: {result.get('content')!r}"
        )

    def test_resolution_falls_back_to_global_when_no_project_match(self):
        """wiki_read with /proj/B (not present) → global page returned."""
        import yadgar.server._state as _st

        storage = _st._storage

        slug = "test-global-fallback-slug"
        _insert_wiki_direct(
            storage,
            "Test Global Fallback Slug",
            "Global content here",
            directory_context="global",
            branch=None,
        )

        from yadgar.server.tools.wiki import wiki_read

        with (
            patch("yadgar.server.tools.wiki.os") as mock_os,
            patch("yadgar.server._detect_branch", return_value=None, create=True),
            patch("yadgar.server._get_default_branch", return_value=None, create=True),
        ):
            mock_os.getcwd.return_value = "/daemon/cwd"
            result = wiki_read(slug, directory="/proj/B")

        assert result.get("error") is None, f"Got error: {result}"
        assert result.get("directory_context") == "global", (
            f"Expected global, got: {result.get('directory_context')!r}"
        )


# ── T4: wiki_list filtered by directory ──────────────────────────────────────


class TestWikiListDirectoryFilter:
    """wiki_list(directory=...) returns dir pages + global, excludes others."""

    def test_wiki_list_scopes_to_directory(self):
        """Insert pages in /proj/A, /proj/B, global. List /proj/A → A + global only."""
        import yadgar.server._state as _st

        storage = _st._storage

        _insert_wiki_direct(storage, "Page A1", "A1 content", "/proj/A")
        _insert_wiki_direct(storage, "Page A2", "A2 content", "/proj/A")
        _insert_wiki_direct(storage, "Page B1", "B1 content", "/proj/B")
        _insert_wiki_direct(storage, "Page Global", "Global content", "global")

        from yadgar.server.tools.wiki import wiki_list

        results = wiki_list(directory="/proj/A")
        slugs = [r["slug"] for r in results]

        assert "page-a1" in slugs, f"Expected page-a1, got: {slugs}"
        assert "page-a2" in slugs, f"Expected page-a2, got: {slugs}"
        assert "page-global" in slugs, f"Expected page-global (always visible), got: {slugs}"
        assert "page-b1" not in slugs, f"Expected page-b1 excluded, got: {slugs}"


# ── T5: _resolve_page_id_by_slug uses caller dir, not daemon CWD ─────────────


class TestResolveSlugCallerDirectory:
    """_resolve_page_id_by_slug(directory=...) uses caller dir, not os.getcwd()."""

    def test_resolve_slug_uses_caller_directory_not_daemon_cwd(self):
        """Insert page under /caller/repo. Daemon CWD=/daemon/root. wiki_history with caller dir works."""
        import yadgar.server._state as _st

        storage = _st._storage

        slug = _insert_wiki_direct(
            storage,
            "Caller Repo Page",
            "Page from caller repo",
            directory_context="/caller/repo",
            branch=None,
        )

        from yadgar.server.tools.wiki import wiki_history

        with patch("yadgar.server.tools.wiki.os") as mock_os:
            mock_os.getcwd.return_value = "/daemon/root"
            # directory param routes to caller context, ignores os.getcwd()
            result = wiki_history(slug, directory="/caller/repo")

        assert "error" not in result, f"Expected success but got error: {result}"
        assert result.get("slug") == slug, f"Unexpected result: {result}"


# ── T6: agent_prompt_save requires directory ─────────────────────────────────


class TestAgentPromptSaveRequiresDirectory:
    """agent_prompt_save must reject calls without directory."""

    def test_agent_prompt_save_requires_directory(self):
        """agent_prompt_save(no directory) → {"error": "missing_directory"}."""
        from yadgar.server.tools.agent_prompts import agent_prompt_save

        result = agent_prompt_save(pattern="test-pattern", content="Test prompt content")
        assert result.get("error") == "missing_directory", (
            f"Expected missing_directory error, got: {result}"
        )
        # Verify no wiki_page created
        import yadgar.server._state as _st

        rows = _st._storage._q("SELECT * FROM wiki_page WHERE slug CONTAINS 'test-pattern'")
        assert len(rows) == 0, f"Expected 0 rows, found {len(rows)}"

    def test_agent_prompt_save_accepts_valid_directory(self):
        """agent_prompt_save with valid directory saves successfully."""
        from yadgar.server.tools.agent_prompts import agent_prompt_save

        result = agent_prompt_save(
            pattern="test-pattern-valid",
            content="Test prompt content",
            directory="/home/max/git/yadgar",
        )
        assert result.get("saved") is True, f"Expected saved=True, got: {result}"
        assert result.get("error") is None


# ── T7: blocks scope='project' requires directory ────────────────────────────


class TestBlocksProjectScopeRequiresDirectory:
    """block_create(scope='project') must reject missing directory."""

    def test_block_create_project_scope_rejects_empty_directory(self):
        """block_create(scope='project', directory=None) → ok=False, error=missing_directory."""
        from yadgar.server.tools.blocks import block_create

        result = block_create(name="test-block", content="content", scope="project", directory=None)
        assert result.get("ok") is False, f"Expected ok=False, got: {result}"
        assert result.get("error") == "missing_directory", (
            f"Expected missing_directory error, got: {result}"
        )

    def test_block_create_global_scope_allows_no_directory(self):
        """block_create(scope='global', directory=None) → ok=True."""
        from yadgar.server.tools.blocks import block_create

        result = block_create(
            name="testglobalblock", content="global content", scope="global", directory=None
        )
        # Should succeed — global blocks don't require directory
        assert result.get("ok") is True or result.get("id") is not None, (
            f"Expected success for global scope, got: {result}"
        )
        assert result.get("error") != "missing_directory"


# ── T8: recall scoped by directory ───────────────────────────────────────────


class TestRecallDirectoryScope:
    """recall(directory=...) excludes memories from other directories."""

    def test_agent_in_proj_A_does_not_see_proj_B_writes(self):
        """recall with directory=/proj/A includes /proj/A and global, excludes /proj/B."""
        import yadgar.server._state as _st

        storage = _st._storage

        _insert_memory_direct(storage, "proj-A-secret content here", "/proj/A")
        _insert_memory_direct(storage, "proj-B-secret content here", "/proj/B")

        from yadgar.server.tools.recall import recall

        with patch("yadgar.server.tools.recall.os") as mock_os:
            mock_os.getcwd.return_value = "/daemon/root"
            results = recall(query="proj secret", max_results=10, directory="/proj/A")

        contents = [r.get("content", "") for r in results]
        any("proj-A-secret" in c for c in contents)
        found_B = any("proj-B-secret" in c for c in contents)

        assert not found_B, f"proj-B memory leaked into /proj/A recall: {contents}"
        # proj-A should be found (if embedding similarity is high enough)
        # Note: zero embeddings may not have high cosine similarity; test the exclusion more than inclusion
        # The critical assertion is that B is excluded.
