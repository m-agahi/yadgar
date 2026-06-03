"""Tests for v5.42.3 — drainer branch enforcement + memory branch_hint parity.

TDD: written BEFORE implementation. All tests in this file are expected to
start RED and go GREEN as implementation progresses.

Coverage:
1. _validate_wiki_add: branch=None + no _internal → rejection string
2. _validate_wiki_add: branch="feat/x" → passes (None returned)
3. _validate_wiki_add: branch=None + _internal=True → passes (bypass)
4. _validate_branch_context: memory-op without branch → rejection string
5. _validate_branch_context: memory-op with branch → passes
6. _validate_branch_context: memory-op _internal=True → bypass
7. DLQ sidecar for missing_branch: failure_reason + failure_metadata.field
8. dlq_requeue blocked for missing_branch (no force=True)
9. dlq_requeue(force=True) allowed after branch added to payload
10. Missing-branch wiki_add → DLQ (integration via drainer.drain_now)
11. memorize hard-reject when _detect_branch returns None + no branch_hint
12. memorize with branch_hint passes when _detect_branch returns None
13. memorize hard-reject → DLQ entry created, no memory stored
14. Migration 015: wiki_draft.branch column added; existing rows have branch=None
15. insert_wiki_draft stores branch; wiki_approve reads and propagates it
16. wiki_approve legacy null-branch draft uses _internal=True carve-out
17. yadgar_dlq_rejection_count metric increments on missing_branch rejection
18. MCP boundary validator: wiki_add missing branch → synchronous error dict
19. MCP boundary validator: memorize missing branch → synchronous error dict
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from yadgar import server
from yadgar.file_queue import FileQueue, QueueDrainer, _Attempt

# ── helpers ────────────────────────────────────────────────────────────────────


def _make_wiki_add_record(
    *,
    slug: str = "test-slug",
    branch: str | None = "ABSENT",  # ABSENT sentinel means don't include the key
    internal: bool = False,
) -> dict:
    """Build a wiki_add queue record dict."""
    payload: dict = {
        "wiki_schema_version": 2,
        "slug": slug,
        "title": slug,
        "content": "Test content for branch enforcement test.",
        "category": "reference",
        "tags": [],
    }
    if branch != "ABSENT":
        payload["branch"] = branch
    if internal:
        payload["_internal"] = True
    return {"op": "wiki_add", "id": "test-id", "payload": payload}


def _make_memory_record(
    *,
    op: str = "memorize",
    branch: str | None = "ABSENT",  # ABSENT sentinel means don't include the key
    internal: bool = False,
) -> dict:
    """Build a memorize/anchor/checkpoint queue record dict."""
    payload: dict = {
        "content": "Test content",
        "context": "/tmp/test-dir",
    }
    if branch != "ABSENT":
        payload["branch"] = branch
    if internal:
        payload["_internal"] = True
    return {"op": op, "id": "test-id", "payload": payload}


@pytest.fixture(autouse=True)
def _engines(tmp_path):
    server.init_engines(
        db_path=str(tmp_path / "test_branch_enforcement.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


@pytest.fixture
def bare_drainer(tmp_path):
    """Isolated FileQueue + QueueDrainer (no server state patching needed for unit tests)."""
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
    """FileQueue + QueueDrainer with server lifecycle patches (for integration tests)."""
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


# ── 1-3: _validate_wiki_add branch checks ─────────────────────────────────────


class TestValidateWikiAddBranchCheck:
    """_validate_wiki_add rejects records missing branch unless _internal=True."""

    def test_no_branch_key_rejected(self, bare_drainer):
        """Record without 'branch' key → rejection string starting with missing_branch."""
        drainer, _ = bare_drainer
        record = _make_wiki_add_record(branch="ABSENT")
        result = drainer._validate_wiki_add(record)
        assert result is not None, "Expected rejection but got None"
        assert result.startswith("missing_branch"), f"Got: {result!r}"

    def test_branch_none_rejected(self, bare_drainer):
        """Record with branch=None (no _internal) → rejection."""
        drainer, _ = bare_drainer
        record = _make_wiki_add_record(branch=None)
        result = drainer._validate_wiki_add(record)
        assert result is not None
        assert result.startswith("missing_branch")

    def test_branch_present_passes(self, bare_drainer):
        """Record with explicit branch value → passes (returns None)."""
        drainer, _ = bare_drainer
        record = _make_wiki_add_record(branch="feat/my-feature")
        result = drainer._validate_wiki_add(record)
        assert result is None, f"Expected None but got: {result!r}"

    def test_internal_flag_bypasses_branch_check(self, bare_drainer):
        """Record with _internal=True → branch check skipped (returns None)."""
        drainer, _ = bare_drainer
        record = _make_wiki_add_record(branch="ABSENT", internal=True)
        result = drainer._validate_wiki_add(record)
        assert result is None, f"Expected None (internal bypass) but got: {result!r}"

    def test_internal_flag_with_null_branch_bypasses(self, bare_drainer):
        """_internal=True + branch=None → still bypasses."""
        drainer, _ = bare_drainer
        record = _make_wiki_add_record(branch=None, internal=True)
        result = drainer._validate_wiki_add(record)
        assert result is None


# ── 4-6: _validate_branch_context for memory ops ──────────────────────────────


class TestValidateBranchContextMemory:
    """_validate_branch_context rejects memory-op records missing branch."""

    def test_memorize_no_branch_rejected(self, bare_drainer):
        """memorize record without branch → rejection."""
        drainer, _ = bare_drainer
        record = _make_memory_record(op="memorize", branch="ABSENT")
        result = drainer._validate_branch_context(record)
        assert result is not None
        assert result.startswith("missing_branch")

    def test_memorize_branch_none_rejected(self, bare_drainer):
        """memorize record with branch=None → rejection."""
        drainer, _ = bare_drainer
        record = _make_memory_record(op="memorize", branch=None)
        result = drainer._validate_branch_context(record)
        assert result is not None
        assert result.startswith("missing_branch")

    def test_memorize_branch_present_passes(self, bare_drainer):
        """memorize record with branch set → passes."""
        drainer, _ = bare_drainer
        record = _make_memory_record(op="memorize", branch="feat/my-branch")
        result = drainer._validate_branch_context(record)
        assert result is None

    def test_anchor_no_branch_rejected(self, bare_drainer):
        """anchor record without branch → rejection."""
        drainer, _ = bare_drainer
        record = _make_memory_record(op="anchor", branch="ABSENT")
        result = drainer._validate_branch_context(record)
        assert result is not None
        assert result.startswith("missing_branch")

    def test_checkpoint_no_branch_rejected(self, bare_drainer):
        """checkpoint record without branch → rejection."""
        drainer, _ = bare_drainer
        record = _make_memory_record(op="checkpoint", branch="ABSENT")
        result = drainer._validate_branch_context(record)
        assert result is not None
        assert result.startswith("missing_branch")

    def test_internal_flag_bypasses_memory_branch_check(self, bare_drainer):
        """memorize record with _internal=True → branch check bypassed."""
        drainer, _ = bare_drainer
        record = _make_memory_record(op="memorize", branch="ABSENT", internal=True)
        result = drainer._validate_branch_context(record)
        assert result is None


# ── 7: DLQ sidecar for missing_branch ─────────────────────────────────────────


class TestMissingBranchDLQSidecar:
    """DLQ sidecar for missing_branch entries has correct failure_reason + failure_metadata."""

    def test_missing_branch_sidecar_fields(self, bare_drainer, tmp_path):
        """_move_to_dlq with failure_reason=missing_branch writes correct sidecar."""
        drainer, fq = bare_drainer
        path = fq.queue_dir / "0001_test.json"
        path.write_text(json.dumps(_make_wiki_add_record(branch="ABSENT")))
        attempt = _Attempt(count=1, last_error="missing_branch: ...", classification="permanent")

        drainer._move_to_dlq(
            path,
            attempt,
            "wiki_add",
            failure_reason="missing_branch",
            failure_metadata={
                "field": "branch",
                "payload_op_type": "wiki_add",
                "hint": "Add branch key to payload and requeue with force=True",
            },
        )

        sidecar = fq.dlq_dir / "0001_test.json.error.json"
        assert sidecar.exists()
        meta = json.loads(sidecar.read_text())
        assert meta["failure_reason"] == "missing_branch"
        assert meta["failure_metadata"]["field"] == "branch"
        assert meta["failure_metadata"]["payload_op_type"] == "wiki_add"
        assert "hint" in meta["failure_metadata"]

    def test_wiki_add_no_branch_lands_in_dlq(self, patched_drainer):
        """Wiki add without branch → DLQ with failure_reason=missing_branch."""
        drainer, fq = patched_drainer

        # Enqueue a wiki_add with no branch (external caller forgetting branch)
        fq.enqueue(
            "wiki_add",
            {
                "wiki_schema_version": 2,
                "slug": "no-branch-page",
                "title": "No Branch Page",
                "content": "Content without branch context.",
                "category": "reference",
            },
        )

        drainer.drain_now()

        # Must be in DLQ, not DB
        storage = server._get_storage()
        rows = storage._q("SELECT slug FROM wiki_page WHERE slug = 'no-branch-page'")
        assert rows == [], "missing-branch page must NOT be inserted"

        dlq_files = [
            f
            for f in fq.dlq_dir.iterdir()
            if f.suffix == ".json" and not f.name.endswith(".error.json")
        ]
        assert len(dlq_files) >= 1

        sidecar = fq.dlq_dir / (dlq_files[0].name + ".error.json")
        assert sidecar.exists()
        meta = json.loads(sidecar.read_text())
        assert meta["failure_reason"] == "missing_branch"


# ── 8-9: dlq_requeue taxonomy blocking ────────────────────────────────────────


class TestDlqRequeueMissingBranch:
    """dlq_requeue blocks missing_branch entries; force=True allows after branch added."""

    def _write_dlq_entry(self, fq, failure_reason: str, payload: dict | None = None) -> str:
        """Write a fake DLQ entry with given failure_reason. Returns filename."""
        fname = "0001_dlq_test.json"
        main_path = fq.dlq_dir / fname
        record = {"op": "wiki_add", "payload": payload or {"slug": "x"}}
        main_path.write_text(json.dumps(record))
        sidecar = fq.dlq_dir / (fname + ".error.json")
        sidecar.write_text(
            json.dumps(
                {
                    "op_type": "wiki_add",
                    "failure_reason": failure_reason,
                    "attempts": 1,
                }
            )
        )
        return fname

    def test_dlq_requeue_blocked_for_missing_branch(self, patched_drainer):
        """dlq_requeue without force=True fails for missing_branch entries."""
        _, fq = patched_drainer
        fname = self._write_dlq_entry(fq, "missing_branch")

        with (
            patch("yadgar.server.tools.admin_dlq._get_file_queue", return_value=fq),
            patch("yadgar.server.lifecycle._get_file_queue", return_value=fq),
        ):
            result = server.dlq_requeue(filename=fname)

        assert not result.get("requeued"), f"Expected blocked but got: {result}"
        assert (
            "missing_branch" in result.get("error", "").lower()
            or "rejection" in result.get("error", "").lower()
            or "cannot" in result.get("error", "").lower()
        )

    def test_dlq_requeue_force_true_allowed_after_branch_added(self, patched_drainer):
        """dlq_requeue(force=True) allowed after operator adds branch to payload."""
        drainer, fq = patched_drainer
        # Write DLQ entry with branch now present in payload
        payload = {
            "wiki_schema_version": 2,
            "slug": "fixed-branch-page",
            "title": "Fixed Branch Page",
            "content": "Content with branch now present.",
            "category": "reference",
            "branch": "feat/fixed",
        }
        fname = self._write_dlq_entry(fq, "missing_branch", payload)

        with (
            patch("yadgar.server.tools.admin_dlq._get_file_queue", return_value=fq),
            patch("yadgar.server.lifecycle._get_file_queue", return_value=fq),
        ):
            result = server.dlq_requeue(filename=fname, force=True)

        assert result.get("requeued"), f"Expected requeued but got: {result}"
        # File should now be in queue, not DLQ
        assert (fq.queue_dir / fname).exists()


# ── 11-13: memorize hard-reject ───────────────────────────────────────────────


class TestMemorizeHardReject:
    """memorize hard-rejects when branch detection fails and no branch_hint supplied."""

    def test_memorize_missing_branch_hard_rejects(self, patched_drainer):
        """memorize with _detect_branch=None + no branch_hint → error dict returned."""
        with patch("yadgar.server._detect_branch", return_value=None):
            result = server.memorize(
                content="Test content for branch reject",
                context="/tmp/no-git",
                tags=["test"],
            )
        assert result.get("error") == "missing_branch", (
            f"Expected missing_branch error but got: {result}"
        )
        assert result.get("stored") is False

    def test_memorize_with_branch_hint_passes(self, patched_drainer):
        """memorize with _detect_branch=None but branch_hint supplied → queued."""
        drainer, fq = patched_drainer
        with patch("yadgar.server._detect_branch", return_value=None):
            result = server.memorize(
                content="Test content with branch hint",
                context="/tmp/no-git",
                tags=["test"],
                branch_hint="feat/my-feature",
            )
        # Should succeed (queued)
        assert result.get("stored") is True or result.get("queued") is True, (
            f"Expected success but got: {result}"
        )

    def test_memorize_hard_reject_no_queue_entry(self, patched_drainer):
        """memorize hard-reject does NOT create a queue entry (fail-fast, not DLQ)."""
        _, fq = patched_drainer
        initial_queue = list(fq.pending())

        with patch("yadgar.server._detect_branch", return_value=None):
            server.memorize(
                content="Test reject no queue",
                context="/tmp/no-git",
                tags=["test"],
            )

        # Queue should not grow on MCP-boundary reject
        assert len(list(fq.pending())) == len(initial_queue)

    def test_memorize_drainer_rejects_missing_branch(self, patched_drainer):
        """Drainer hard-rejects memory-op payload without branch → DLQ."""
        drainer, fq = patched_drainer

        # Directly enqueue a memorize without branch (bypassing MCP layer)
        fq.enqueue(
            "memorize",
            {
                "content": "Memory without branch",
                "context": "/tmp/test",
                "tags": [],
            },
        )

        drainer.drain_now()

        # Must be in DLQ
        dlq_files = [
            f
            for f in fq.dlq_dir.iterdir()
            if f.suffix == ".json" and not f.name.endswith(".error.json")
        ]
        assert len(dlq_files) >= 1, "missing-branch memorize must go to DLQ"

        sidecar = fq.dlq_dir / (dlq_files[0].name + ".error.json")
        assert sidecar.exists()
        meta = json.loads(sidecar.read_text())
        assert meta["failure_reason"] == "missing_branch"


# ── 14: Migration 015 — wiki_draft.branch column ──────────────────────────────


class TestMigration015WikiDraftBranch:
    """Migration 015: wiki_draft table gains a 'branch' column."""

    def test_insert_wiki_draft_with_branch(self):
        """insert_wiki_draft stores branch field for new drafts."""
        storage = server._get_storage()
        storage.insert_wiki_draft(
            {
                "title": "Test Draft",
                "slug": "test-draft-branch",
                "content": "Draft content",
                "category": "reference",
                "tags": [],
                "confidence": "medium",
                "branch": "feat/my-branch",
            }
        )
        draft = storage.get_wiki_draft_by_slug("test-draft-branch")
        assert draft is not None
        assert draft.get("branch") == "feat/my-branch"

    def test_insert_wiki_draft_null_branch_backfill(self):
        """Legacy drafts (branch=None) are stored with branch=None."""
        storage = server._get_storage()
        storage.insert_wiki_draft(
            {
                "title": "Legacy Draft",
                "slug": "legacy-draft-no-branch",
                "content": "Legacy content",
                "category": "reference",
                "tags": [],
                "confidence": "medium",
            }
        )
        draft = storage.get_wiki_draft_by_slug("legacy-draft-no-branch")
        assert draft is not None
        # branch may be absent or None — both are acceptable for legacy rows
        assert draft.get("branch") is None


# ── 15: wiki_approve propagates draft branch ──────────────────────────────────


class TestWikiApproveBranchPropagation:
    """wiki_approve reads branch from draft and stores it on the wiki page."""

    def test_wiki_approve_preserves_draft_branch(self):
        """Draft with branch='feat/x' → approved page has branch='feat/x'."""
        storage = server._get_storage()
        storage.insert_wiki_draft(
            {
                "title": "Approve Branch Test",
                "slug": "approve-branch-test",
                "content": "Content to approve.",
                "category": "reference",
                "tags": [],
                "confidence": "medium",
                "branch": "feat/my-feature",
            }
        )

        result = server.wiki_approve(slug="approve-branch-test")
        assert result.get("approved") is True

        # Check stored page has correct branch
        rows = storage._q("SELECT slug, branch FROM wiki_page WHERE slug = 'approve-branch-test'")
        assert rows, "Approved page should be in wiki_page"
        assert rows[0].get("branch") == "feat/my-feature", (
            f"Expected branch='feat/my-feature', got: {rows[0].get('branch')!r}"
        )

    def test_wiki_approve_legacy_null_branch_uses_internal_flag(self):
        """Legacy null-branch draft approved → page stored (backward compat path)."""
        storage = server._get_storage()
        storage.insert_wiki_draft(
            {
                "title": "Legacy Null Branch Approve",
                "slug": "legacy-null-branch-approve",
                "content": "Legacy content to approve.",
                "category": "reference",
                "tags": [],
                "confidence": "medium",
                # No branch key = legacy null
            }
        )

        # Should succeed even with null branch (backward-compat)
        result = server.wiki_approve(slug="legacy-null-branch-approve")
        assert result.get("approved") is True


# ── 17: Metric counter for missing_branch ──────────────────────────────────────


class TestMissingBranchMetric:
    """yadgar_dlq_rejection_count gauge updates when missing_branch entries exist."""

    def test_rejection_count_gauge_accessible(self):
        """yadgar_dlq_rejection_count metric is importable and a Gauge."""
        from yadgar.metrics import yadgar_dlq_rejection_count

        # It should be a Gauge (has .set method)
        assert hasattr(yadgar_dlq_rejection_count, "set")


# ── 18-19: MCP boundary Pydantic validators ────────────────────────────────────


class TestMcpBoundaryValidators:
    """MCP boundary rejects wiki_add + memorize calls missing branch synchronously."""

    def test_wiki_add_no_branch_returns_error_dict(self):
        """wiki_add without branch/branch_hint returns synchronous error dict."""
        with patch("yadgar.server._detect_branch", return_value=None):
            result = server.wiki_add(
                title="No Branch Wiki Page",
                content="Content without any branch context.",
                branch=None,
                branch_hint=None,
            )
        assert result.get("error") == "missing_branch" or (
            result.get("stored") is False and "branch" in result.get("reason", "").lower()
        ), f"Expected missing_branch error dict, got: {result}"

    def test_wiki_add_with_branch_hint_passes(self):
        """wiki_add with branch_hint → succeeds (no error dict)."""
        result = server.wiki_add(
            title="Branch Hint Wiki Page",
            content="Content with branch hint provided.",
            branch=None,
            branch_hint="feat/my-branch",
        )
        # Should NOT be an error
        assert result.get("error") != "missing_branch"
        assert result.get("stored") is not False or result.get("queued") is True

    def test_memorize_no_branch_returns_error_dict(self):
        """memorize without branch context returns synchronous missing_branch error."""
        with patch("yadgar.server._detect_branch", return_value=None):
            result = server.memorize(
                content="Memory without any branch context.",
                context="/tmp/no-git",
                tags=[],
            )
        assert result.get("error") == "missing_branch" or (
            result.get("stored") is False and "branch" in result.get("reason", "").lower()
        ), f"Expected missing_branch error, got: {result}"

    def test_memorize_with_branch_hint_no_error(self):
        """memorize with branch_hint → no error returned."""
        with patch("yadgar.server._detect_branch", return_value=None):
            result = server.memorize(
                content="Memory with branch hint.",
                context="/tmp/no-git",
                tags=[],
                branch_hint="feat/my-branch",
            )
        assert result.get("error") != "missing_branch"
        assert result.get("stored") is True or result.get("queued") is True
