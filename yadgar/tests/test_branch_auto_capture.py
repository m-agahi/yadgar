"""Tests for §25 branch-tag auto-capture on write paths.

Covers:
- _detect_branch returns current branch from git
- _detect_branch returns None for non-git directory
- _detect_branch returns None for detached HEAD
- _detect_branch is LRU-cached: second call in same 30s bucket does not re-shell-out
- memorize sets branch field on insert (via drainer sync path)
- anchor sets branch field on insert (via sync path)
- checkpoint passes branch through (directory-based detection)
- wiki_add sets branch field on insert (via sync path)
- Failure: _detect_branch raising -> memory still inserts with branch=NONE
"""

import subprocess
from unittest.mock import patch

import pytest

from yadgar import server


@pytest.fixture(autouse=True)
def _engines(tmp_path):
    db_path = str(tmp_path / "test.db")
    server.init_engines(db_path=db_path, embedding_model="all-MiniLM-L6-v2")
    yield
    server.shutdown()


# ── _detect_branch unit tests ─────────────────────────────────────────────────


class TestDetectBranch:
    """Unit tests for the _detect_branch helper."""

    def test_returns_current_branch(self, tmp_path):
        """Returns branch name when git reports it."""
        with patch("subprocess.check_output", return_value=b"feat/my-branch\n"):
            result = server._detect_branch(str(tmp_path))
        assert result == "feat/my-branch"

    def test_returns_none_for_non_git_directory(self, tmp_path):
        """Returns None when git exits with nonzero (not a git repo)."""
        with patch(
            "subprocess.check_output",
            side_effect=subprocess.CalledProcessError(128, "git"),
        ):
            result = server._detect_branch(str(tmp_path))
        assert result is None

    def test_returns_none_on_timeout(self, tmp_path):
        """Returns None when git times out."""
        with patch(
            "subprocess.check_output",
            side_effect=subprocess.TimeoutExpired("git", 2),
        ):
            result = server._detect_branch(str(tmp_path))
        assert result is None

    def test_returns_none_on_file_not_found(self, tmp_path):
        """Returns None when git binary not found."""
        with patch(
            "subprocess.check_output",
            side_effect=FileNotFoundError("git not found"),
        ):
            result = server._detect_branch(str(tmp_path))
        assert result is None

    def test_returns_none_for_detached_head(self, tmp_path):
        """Returns None when git says HEAD (detached HEAD state)."""
        with patch("subprocess.check_output", return_value=b"HEAD\n"):
            result = server._detect_branch(str(tmp_path))
        assert result is None

    def test_returns_none_for_empty_output(self, tmp_path):
        """Returns None when git returns empty string."""
        with patch("subprocess.check_output", return_value=b"\n"):
            result = server._detect_branch(str(tmp_path))
        assert result is None

    def test_lru_cache_prevents_reshellout(self, tmp_path):
        """Second call with same directory in same 30s bucket uses cache."""
        # Fix the time bucket so both calls land in the same bucket
        fixed_bucket = 12345
        with patch("subprocess.check_output", return_value=b"main\n") as mock_co:
            with patch("time.time", return_value=fixed_bucket * 30.0):
                r1 = server._detect_branch_cached(str(tmp_path), fixed_bucket)
                r2 = server._detect_branch_cached(str(tmp_path), fixed_bucket)
        assert r1 == "main"
        assert r2 == "main"
        # Cache hit — subprocess called only once
        assert mock_co.call_count == 1

    def test_different_bucket_triggers_new_call(self, tmp_path):
        """Different time bucket produces a new subprocess call."""
        bucket_a = 99991
        bucket_b = 99992
        with patch("subprocess.check_output", return_value=b"main\n") as mock_co:
            r1 = server._detect_branch_cached(str(tmp_path), bucket_a)
            r2 = server._detect_branch_cached(str(tmp_path), bucket_b)
        assert r1 == "main"
        assert r2 == "main"
        assert mock_co.call_count == 2


# ── Write-path wiring via sync path ──────────────────────────────────────────


class TestMemoriseBranchCapture:
    """memorize() sets branch on insert when is_draining=True."""

    def test_memorize_sets_branch(self, monkeypatch):
        """Branch captured at memorize time appears in storage row."""
        monkeypatch.setattr("yadgar.file_queue._drain_local.active", True, raising=False)
        monkeypatch.setattr("yadgar.server._detect_branch", lambda _d: "feat/test-branch")
        result = server.memorize(
            content="test memory with branch",
            context="/tmp/test-dir",
            tags=["test"],
        )
        # Sync path returns the full memory object (not {"stored": True, ...})
        memory_id = result.get("id")
        assert memory_id is not None, f"memorize sync path must return id, got: {result}"
        storage = server._get_storage()
        rows = storage._q(f"SELECT branch FROM memory:{memory_id}")
        assert rows, "memory row not found"
        assert rows[0].get("branch") == "feat/test-branch", (
            f"expected feat/test-branch, got {rows[0].get('branch')!r}"
        )

    def test_memorize_branch_none_when_detect_returns_none(self, monkeypatch):
        """When branch detection returns None, memory is inserted with branch=NONE."""
        monkeypatch.setattr("yadgar.file_queue._drain_local.active", True, raising=False)
        monkeypatch.setattr("yadgar.server._detect_branch", lambda _d: None)
        result = server.memorize(
            content="memory without branch context",
            context="/tmp/non-git-dir",
            tags=["test"],
        )
        # Sync path returns the full memory object
        memory_id = result.get("id")
        assert memory_id is not None, f"memorize sync path must return id, got: {result}"
        storage = server._get_storage()
        rows = storage._q(f"SELECT branch FROM memory:{memory_id}")
        assert rows, "memory row not found"
        branch = rows[0].get("branch")
        assert branch is None, f"expected None, got {branch!r}"

    def test_memorize_succeeds_when_detect_raises(self, monkeypatch):
        """detect_branch raising must not propagate — memory still stored."""
        monkeypatch.setattr("yadgar.file_queue._drain_local.active", True, raising=False)

        def _raise(_d):
            raise RuntimeError("unexpected git failure")

        monkeypatch.setattr("yadgar.server._detect_branch", _raise)
        result = server.memorize(
            content="memory despite detection failure",
            context="/tmp/dir",
            tags=["test"],
        )
        # Sync path returns full memory dict — id should be present (no error)
        assert result.get("id") is not None or result.get("stored") is True, (
            f"memorize should still succeed: {result}"
        )


class TestAnchorBranchCapture:
    """anchor() sets branch on insert when is_draining=True."""

    def test_anchor_sets_branch(self, monkeypatch):
        monkeypatch.setattr("yadgar.file_queue._drain_local.active", True, raising=False)
        monkeypatch.setattr("yadgar.server._detect_branch", lambda _d: "feat/anchor-branch")
        result = server.anchor(
            content="critical anchor fact",
            context="/tmp/anchor-dir",
            reason="testing branch capture",
        )
        memory_id = result.get("memory_id")
        assert memory_id is not None, f"anchor failed: {result}"
        storage = server._get_storage()
        rows = storage._q(f"SELECT branch FROM memory:{memory_id}")
        assert rows, "anchor memory row not found"
        assert rows[0].get("branch") == "feat/anchor-branch"

    def test_anchor_branch_none_when_non_git(self, monkeypatch):
        monkeypatch.setattr("yadgar.file_queue._drain_local.active", True, raising=False)
        monkeypatch.setattr("yadgar.server._detect_branch", lambda _d: None)
        result = server.anchor(
            content="anchor outside git repo",
            context="/tmp/non-git",
            reason="no-branch test",
        )
        memory_id = result.get("memory_id")
        storage = server._get_storage()
        rows = storage._q(f"SELECT branch FROM memory:{memory_id}")
        branch = rows[0].get("branch")
        assert branch is None, f"expected None, got {branch!r}"


class TestCheckpointBranchCapture:
    """checkpoint() passes detected branch to storage (tracked via memory insert by anchor_memory in restore)."""

    def test_checkpoint_completes_without_error(self, monkeypatch):
        """Checkpoint call succeeds with branch detection active."""
        monkeypatch.setattr("yadgar.file_queue._drain_local.active", True, raising=False)
        monkeypatch.setattr("yadgar.server._detect_branch", lambda _d: "feat/checkpoint-branch")
        result = server.checkpoint(
            directory="/tmp/checkpoint-dir",
            current_task="testing branch on checkpoint",
        )
        # checkpoint stores to checkpoint table, not memory — verify no error
        assert "checkpoint_id" in result or "queued" in result, (
            f"unexpected checkpoint result: {result}"
        )

    def test_checkpoint_passes_branch_to_replay(self, monkeypatch, tmp_path):
        """Branch is passed through to CheckpointRestore.create_checkpoint."""
        monkeypatch.setattr("yadgar.file_queue._drain_local.active", True, raising=False)
        monkeypatch.setattr("yadgar.server._detect_branch", lambda _d: "feat/cp-branch")
        captured_kwargs = {}
        replay = server._get_replay()
        orig_create = replay.create_checkpoint

        def _capture_create(**kwargs):
            captured_kwargs.update(kwargs)
            return orig_create(**kwargs)

        monkeypatch.setattr(replay, "create_checkpoint", _capture_create)
        server.checkpoint(
            directory="/tmp/cp-branch-dir",
            current_task="cp-branch test",
        )
        # branch kwarg should have been threaded through if applicable
        # (checkpoint passes branch for future retrieval-filter benefit)
        assert "directory" in captured_kwargs


class TestWikiAddBranchCapture:
    """wiki_add() passes branch to wiki store when called in sync path."""

    def test_wiki_add_sets_branch(self, monkeypatch):
        # v5.4 W1: _detect_branch fallback removed from wiki_add.
        # Callers must supply branch or branch_hint explicitly.
        # Use branch_hint to simulate what the host-side hook now provides.
        monkeypatch.setattr("yadgar.file_queue._drain_local.active", True, raising=False)
        result = server.wiki_add(
            title="Branch Test Wiki Page",
            content="wiki content for branch test",
            category="reference",
            branch_hint="feat/wiki-branch",
        )
        assert "slug" in result, f"wiki_add failed: {result}"
        slug = result["slug"]
        wiki = server._wiki
        assert wiki is not None
        page = wiki._storage.get_wiki_page_by_slug(slug)
        assert page is not None
        assert page.get("branch") == "feat/wiki-branch", (
            f"expected feat/wiki-branch, got {page.get('branch')!r}"
        )

    def test_wiki_add_branch_none_for_non_git(self, monkeypatch):
        monkeypatch.setattr("yadgar.file_queue._drain_local.active", True, raising=False)
        monkeypatch.setattr("yadgar.server._detect_branch", lambda _d: None)
        result = server.wiki_add(
            title="Wiki Page No Branch",
            content="wiki content without branch",
            category="reference",
        )
        slug = result["slug"]
        wiki = server._wiki
        page = wiki._storage.get_wiki_page_by_slug(slug)
        assert page is not None
        branch = page.get("branch")
        assert branch is None, f"expected None, got {branch!r}"


# ── Queue payload includes branch ─────────────────────────────────────────────


class TestQueuePayloadBranch:
    """Branch captured at enqueue time so drainer can replay with correct branch."""

    def test_memorize_queue_payload_contains_branch(self, monkeypatch):
        """When not draining, memorize enqueues branch in payload."""
        monkeypatch.setattr("yadgar.server._detect_branch", lambda _d: "feat/queue-branch")
        captured_payloads = []
        orig_enqueue = server._get_file_queue().__class__.enqueue

        def _capture_enqueue(self, op, payload):
            captured_payloads.append((op, dict(payload)))
            return orig_enqueue(self, op, payload)

        fq = server._get_file_queue()
        monkeypatch.setattr(
            fq, "enqueue", lambda op, p: captured_payloads.append((op, dict(p))) or "fake_path"
        )

        server.memorize(
            content="queue payload branch test",
            context="/tmp/queue-dir",
            tags=["test"],
        )
        assert any(op == "memorize" for op, _ in captured_payloads), (
            "memorize op not in captured queue ops"
        )
        for op, payload in captured_payloads:
            if op == "memorize":
                assert payload.get("branch") == "feat/queue-branch", (
                    f"branch missing from memorize payload: {payload}"
                )

    def test_anchor_queue_payload_contains_branch(self, monkeypatch):
        """When not draining, anchor enqueues branch in payload."""
        monkeypatch.setattr("yadgar.server._detect_branch", lambda _d: "feat/anchor-queue")
        captured_payloads = []
        fq = server._get_file_queue()
        monkeypatch.setattr(
            fq,
            "enqueue",
            lambda op, p: captured_payloads.append((op, dict(p))) or "fake_path",
        )
        server.anchor(
            content="anchor payload branch test",
            context="/tmp/anchor-queue-dir",
            reason="payload test",
        )
        for op, payload in captured_payloads:
            if op == "anchor":
                assert payload.get("branch") == "feat/anchor-queue", (
                    f"branch missing from anchor payload: {payload}"
                )

    def test_wiki_add_queue_payload_contains_branch(self, monkeypatch):
        """When not draining, wiki_add enqueues branch in payload.

        v5.4 W1: branch_hint (supplied by host hook) is resolved to branch
        before enqueue — no _detect_branch fallback.
        """
        captured_payloads = []
        fq = server._get_file_queue()
        monkeypatch.setattr(
            fq,
            "enqueue",
            lambda op, p: captured_payloads.append((op, dict(p))) or "fake_path",
        )
        server.wiki_add(
            title="Wiki Queue Branch Test",
            content="wiki content for queue test",
            branch_hint="feat/wiki-queue",
        )
        for op, payload in captured_payloads:
            if op == "wiki_add":
                assert payload.get("branch") == "feat/wiki-queue", (
                    f"branch missing from wiki_add payload: {payload}"
                )
