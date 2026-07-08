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

from yadgar.core import server
from yadgar.tests.conftest import memorize_sync

_TEST_DIR = "/home/max/git/yadgar"


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("branch_auto_capture")
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
    """memorize() captures branch at enqueue time; drainer replays with it.

    R3 migration: _drain_local.active sync path removed. All memorize calls
    enqueue. The _unit_backend_harness autouse fixture wires a QueueDrainer
    in-process and sets YADGAR_CI_BRANCH="feat/test-branch" as fallback.
    memorize_sync() is used here to enqueue + drain + look up by content.
    """

    def test_memorize_sets_branch(self, monkeypatch):
        """Branch captured at enqueue time appears in storage row after drain."""
        # Override cwd detection to return an explicit branch — proves cwd
        # detection path still fires at the MCP API boundary (pre-enqueue).
        monkeypatch.setattr("yadgar.core.server._detect_branch", lambda _d: "feat/test-branch")
        mem = memorize_sync(
            "test memory with branch",
            "/tmp/test-dir",
            ["test"],
        )
        memory_id = mem.get("id")
        assert memory_id is not None, f"memorize_sync must return id after drain, got: {mem}"
        storage = server._get_storage()
        rows = storage._q(f"SELECT branch FROM memory:{memory_id}")
        assert rows, "memory row not found"
        assert rows[0].get("branch") == "feat/test-branch", (
            f"expected feat/test-branch, got {rows[0].get('branch')!r}"
        )

    def test_memorize_branch_none_when_detect_returns_none(self, monkeypatch):
        """R3: without any branch context, memorize returns missing_branch rejection.

        R3 write-path hard-rejects at the MCP boundary when no branch can be
        resolved (_detect_branch=None, no branch_hint, YADGAR_CI_BRANCH stripped).
        The queue never receives the write — no memory row is stored.
        """
        monkeypatch.delenv("YADGAR_CI_BRANCH", raising=False)
        monkeypatch.setattr("yadgar.core.server._detect_branch", lambda _d: None)
        result = server.memorize(
            content="memory without branch context",
            context="/tmp/non-git-dir",
            tags=["test"],
        )
        # R3: hard rejection — not stored, not queued.
        assert result.get("error") == "missing_branch", (
            f"expected missing_branch rejection, got: {result}"
        )
        assert result.get("stored") is False, f"stored should be False: {result}"

    def test_memorize_succeeds_when_detect_raises(self, monkeypatch):
        """detect_branch raising is caught; YADGAR_CI_BRANCH fallback keeps it stored."""

        def _raise(_d):
            raise RuntimeError("unexpected git failure")

        monkeypatch.setattr("yadgar.core.server._detect_branch", _raise)
        # YADGAR_CI_BRANCH is set by _unit_backend_harness → acts as fallback branch.
        mem = memorize_sync(
            "memory despite detection failure",
            "/tmp/dir",
            ["test"],
        )
        assert mem.get("id") is not None, (
            f"memorize should still succeed via CI_BRANCH fallback: {mem}"
        )


class TestAnchorBranchCapture:
    """anchor() captures branch at enqueue time; drainer replays with it.

    R3 migration: anchor() always enqueues (never returns memory_id directly).
    After drain, look up anchored memory by content to verify branch field.
    """

    def test_anchor_sets_branch(self, monkeypatch, _unit_backend_harness):
        """Branch set at enqueue time appears in storage after drain."""
        monkeypatch.setattr("yadgar.core.server._detect_branch", lambda _d: "feat/anchor-branch")
        result = server.anchor(
            content="critical anchor fact for branch test",
            context="/tmp/anchor-dir",
            reason="testing branch capture",
        )
        # R3: anchor() enqueues and returns {queued: True, status: "anchored", ...}
        assert result.get("queued") is True, f"anchor should be queued, got: {result}"
        assert result.get("status") == "anchored", f"unexpected status: {result}"

        # Drain the queue so the write reaches SurrealDB.
        drainer = _unit_backend_harness
        drainer.drain_now()

        storage = server._get_storage()
        rows = storage._q(
            "SELECT id, branch FROM memory WHERE content = 'critical anchor fact for branch test'"
        )
        assert rows, "anchored memory row not found after drain"
        assert rows[0].get("branch") == "feat/anchor-branch", (
            f"expected feat/anchor-branch, got {rows[0].get('branch')!r}"
        )

    def test_anchor_branch_none_when_non_git(self, monkeypatch):
        """R3: without any branch context, anchor returns missing_branch rejection.

        anchor() hard-rejects at the MCP boundary — no queue write, no storage row.
        """
        monkeypatch.delenv("YADGAR_CI_BRANCH", raising=False)
        monkeypatch.setattr("yadgar.core.server._detect_branch", lambda _d: None)
        result = server.anchor(
            content="anchor outside git repo",
            context="/tmp/non-git",
            reason="no-branch test",
        )
        # R3: hard rejection — not stored, not queued.
        assert result.get("error") == "missing_branch", (
            f"expected missing_branch rejection, got: {result}"
        )
        assert result.get("stored") is False, f"stored should be False: {result}"


class TestCheckpointBranchCapture:
    """checkpoint() passes detected branch to storage (tracked via memory insert by anchor_memory in restore)."""

    def test_checkpoint_completes_without_error(self, monkeypatch):
        """Checkpoint call succeeds with branch detection active.

        R3 migration: checkpoint() always enqueues (no sync path). Returns
        {"queued": True, "directory": ...}. _drain_local.active no longer used.
        """
        monkeypatch.setattr(
            "yadgar.core.server._detect_branch", lambda _d: "feat/checkpoint-branch"
        )
        result = server.checkpoint(
            directory="/tmp/checkpoint-dir",
            current_task="testing branch on checkpoint",
        )
        # R3: checkpoint enqueues — returns {"queued": True, "directory": ...}
        assert "queued" in result, f"unexpected checkpoint result: {result}"

    def test_checkpoint_passes_branch_to_replay(self, monkeypatch, tmp_path):
        """Branch is captured in the queue payload at enqueue time.

        R3 migration: checkpoint enqueues to FileQueue. Verify branch appears
        in the captured queue payload (drainer will replay it later).
        """
        monkeypatch.setattr("yadgar.core.server._detect_branch", lambda _d: "feat/cp-branch")
        captured_payloads = []
        fq = server._get_file_queue()
        orig_enqueue = fq.enqueue

        def _capture_enqueue(op, payload):
            captured_payloads.append((op, dict(payload)))
            return orig_enqueue(op, payload)

        monkeypatch.setattr(fq, "enqueue", _capture_enqueue)
        server.checkpoint(
            directory="/tmp/cp-branch-dir",
            current_task="cp-branch test",
        )
        checkpoint_payloads = [p for op, p in captured_payloads if op == "checkpoint"]
        assert checkpoint_payloads, "no checkpoint op enqueued"
        assert checkpoint_payloads[0].get("branch") == "feat/cp-branch", (
            f"branch missing from checkpoint payload: {checkpoint_payloads[0]}"
        )


class TestWikiAddBranchCapture:
    """wiki_add() passes branch to wiki store when called via queue path.

    R3 migration: wiki_add() always enqueues (wait=False default). After
    enqueue, drain the queue to verify the branch appears in the stored page.
    """

    def test_wiki_add_sets_branch(self, monkeypatch, _unit_backend_harness):
        """branch_hint supplied → branch appears in wiki page after drain.

        v5.4 W1: _detect_branch fallback removed from wiki_add.
        Callers must supply branch or branch_hint explicitly.
        """
        drainer = _unit_backend_harness
        result = server.wiki_add(
            title="Branch Test Wiki Page",
            content="wiki content for branch test",
            category="reference",
            directory="/home/user/project",
            branch_hint="feat/wiki-branch",
        )
        assert "slug" in result, f"wiki_add failed: {result}"
        slug = result["slug"]

        # R3: drain so the write reaches wiki storage.
        drainer.drain_now()

        wiki = server._wiki
        assert wiki is not None
        page = wiki._storage.get_wiki_page_by_slug(slug)
        assert page is not None, f"wiki page not found after drain (slug={slug!r})"
        assert page.get("branch") == "feat/wiki-branch", (
            f"expected feat/wiki-branch, got {page.get('branch')!r}"
        )

    def test_wiki_add_branch_none_for_non_git(self, monkeypatch, _unit_backend_harness):
        """No branch supplied → page stored with branch=None when enforcement is OFF.

        R3 migration: wiki_add does not call _detect_branch. Without branch or
        branch_hint and YADGAR_BRANCH_ENFORCEMENT=false, _check_wiki_add_context
        logs a warning and proceeds. Page is stored with branch=None after drain.
        """
        drainer = _unit_backend_harness
        # Ensure no branch env fallback, no explicit branch, enforcement OFF.
        monkeypatch.delenv("YADGAR_CI_BRANCH", raising=False)
        monkeypatch.setenv("YADGAR_BRANCH_ENFORCEMENT", "false")
        result = server.wiki_add(
            title="Wiki Page No Branch",
            content="wiki content without branch",
            category="reference",
            directory="/home/user/project",
        )
        assert "slug" in result, f"wiki_add failed or rejected unexpectedly: {result}"
        slug = result["slug"]

        # R3: drain so the write reaches wiki storage.
        drainer.drain_now()

        wiki = server._wiki
        page = wiki._storage.get_wiki_page_by_slug(slug)
        assert page is not None, f"wiki page not found after drain (slug={slug!r})"
        branch = page.get("branch")
        assert branch is None, f"expected None, got {branch!r}"


# ── Queue payload includes branch ─────────────────────────────────────────────


class TestQueuePayloadBranch:
    """Branch captured at enqueue time so drainer can replay with correct branch."""

    def test_memorize_queue_payload_contains_branch(self, monkeypatch):
        """When not draining, memorize enqueues branch in payload."""
        monkeypatch.setattr("yadgar.core.server._detect_branch", lambda _d: "feat/queue-branch")
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
        monkeypatch.setattr("yadgar.core.server._detect_branch", lambda _d: "feat/anchor-queue")
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
            directory=_TEST_DIR,
        )
        for op, payload in captured_payloads:
            if op == "wiki_add":
                assert payload.get("branch") == "feat/wiki-queue", (
                    f"branch missing from wiki_add payload: {payload}"
                )
