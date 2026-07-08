"""v5.46.7 TDD — YADGAR_CI_BRANCH env var consumed by daemon (behavioral test).

v5.46.3 set YADGAR_CI_BRANCH in workflows but daemon never READ it.
Tests here verify the env fallback is wired into branch resolution for
memorize, anchor, checkpoint, and update_active_work (all missing_branch call sites).

Resolution order (v5.46.7):
  _detect_branch(context) → branch_hint → YADGAR_CI_BRANCH env → hard-reject
"""

from __future__ import annotations

from unittest.mock import patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _memorize_with_no_git_no_hint(monkeypatch, env_branch: str | None = None):
    """Call memorize with detect_branch returning None and no branch_hint.

    If env_branch is given, YADGAR_CI_BRANCH is set to that value.
    Returns the result dict from memorize().
    """
    if env_branch is not None:
        monkeypatch.setenv("YADGAR_CI_BRANCH", env_branch)
    else:
        monkeypatch.delenv("YADGAR_CI_BRANCH", raising=False)

    import yadgar.core.server as _srv
    from yadgar.core.server.tools.memorize import memorize

    with patch.object(_srv, "_detect_branch", return_value=None):
        return memorize(
            content="test memory",
            context="/tmp/test-no-git",
            tags=["test"],
            branch_hint=None,
        )


def _anchor_with_no_git_no_hint(monkeypatch, env_branch: str | None = None):
    """Call anchor with detect_branch returning None and no branch_hint."""
    if env_branch is not None:
        monkeypatch.setenv("YADGAR_CI_BRANCH", env_branch)
    else:
        monkeypatch.delenv("YADGAR_CI_BRANCH", raising=False)

    import yadgar.core.server as _srv
    from yadgar.core.server.tools.misc import anchor

    with patch.object(_srv, "_detect_branch", return_value=None):
        return anchor(
            content="test anchor",
            context="/tmp/test-no-git",
            branch_hint=None,
        )


def _checkpoint_with_no_git_no_hint(monkeypatch, env_branch: str | None = None):
    """Call checkpoint with detect_branch returning None and no branch_hint."""
    if env_branch is not None:
        monkeypatch.setenv("YADGAR_CI_BRANCH", env_branch)
    else:
        monkeypatch.delenv("YADGAR_CI_BRANCH", raising=False)

    import yadgar.core.server as _srv
    from yadgar.core.server.tools.misc import checkpoint

    with patch.object(_srv, "_detect_branch", return_value=None):
        return checkpoint(
            directory="/tmp/test-no-git",
            current_task="working on v5.46.7",
            branch_hint=None,
        )


def _update_active_work_with_no_git_no_hint(monkeypatch, env_branch: str | None = None):
    """Call update_active_work with detect_branch returning None and no branch_hint.

    Returns either a dict result or a sentinel {'_passed_branch_gate': True} when
    the function proceeds past the branch gate but fails on storage not initialized
    (expected in unit-test context without a live StorageEngine).
    """
    if env_branch is not None:
        monkeypatch.setenv("YADGAR_CI_BRANCH", env_branch)
    else:
        monkeypatch.delenv("YADGAR_CI_BRANCH", raising=False)

    from yadgar.core.server.tools.project import update_active_work

    try:
        with patch("yadgar.core.server.tools.project._detect_branch", return_value=None):
            return update_active_work(
                directory="/tmp/test-no-git",
                content="active work content",
                branch_hint=None,
            )
    except AssertionError as exc:
        # StorageEngine not initialized — branch gate passed, execution continued.
        if "StorageEngine not initialized" in str(exc):
            return {"_passed_branch_gate": True}
        raise
    except RuntimeError as exc:
        # R3 Car 3d: the DB write now forwards via _forward_admin; without a
        # configured YADGAR_EMBED_URL it raises RuntimeError AFTER the branch gate
        # passed — same signal as the pre-forward "StorageEngine not initialized".
        if "YADGAR_EMBED_URL is not set" in str(exc):
            return {"_passed_branch_gate": True}
        raise


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMemorizeEnvBranchFallback:
    """memorize() uses YADGAR_CI_BRANCH when git detection fails and branch_hint absent."""

    def test_no_env_no_hint_returns_missing_branch(self, monkeypatch):
        """Baseline: no env + no hint → still missing_branch."""
        result = _memorize_with_no_git_no_hint(monkeypatch, env_branch=None)
        assert result.get("error") == "missing_branch", f"expected missing_branch, got: {result}"

    def test_env_branch_resolves_to_queued(self, monkeypatch):
        """YADGAR_CI_BRANCH=master + no hint → memorize should not hard-reject.

        Note: queued path returns {'stored': True, 'queued': True} when file queue
        is running, but the enqueue may fail in test context (no file queue).
        The critical assertion: error must NOT be 'missing_branch'.
        """
        result = _memorize_with_no_git_no_hint(monkeypatch, env_branch="master")
        assert result.get("error") != "missing_branch", (
            f"YADGAR_CI_BRANCH=master must prevent missing_branch rejection. Got: {result}"
        )


class TestAnchorEnvBranchFallback:
    """anchor() uses YADGAR_CI_BRANCH when git detection fails and branch_hint absent."""

    def test_no_env_no_hint_returns_missing_branch(self, monkeypatch):
        result = _anchor_with_no_git_no_hint(monkeypatch, env_branch=None)
        assert result.get("error") == "missing_branch", f"expected missing_branch, got: {result}"

    def test_env_branch_resolves_to_not_missing_branch(self, monkeypatch):
        result = _anchor_with_no_git_no_hint(monkeypatch, env_branch="master")
        assert result.get("error") != "missing_branch", (
            f"YADGAR_CI_BRANCH=master must prevent missing_branch in anchor. Got: {result}"
        )


class TestCheckpointEnvBranchFallback:
    """checkpoint() uses YADGAR_CI_BRANCH when git detection fails and branch_hint absent."""

    def test_no_env_no_hint_returns_missing_branch(self, monkeypatch):
        result = _checkpoint_with_no_git_no_hint(monkeypatch, env_branch=None)
        assert result.get("error") == "missing_branch", f"expected missing_branch, got: {result}"

    def test_env_branch_resolves_to_not_missing_branch(self, monkeypatch):
        result = _checkpoint_with_no_git_no_hint(monkeypatch, env_branch="master")
        assert result.get("error") != "missing_branch", (
            f"YADGAR_CI_BRANCH=master must prevent missing_branch in checkpoint. Got: {result}"
        )


class TestUpdateActiveWorkEnvBranchFallback:
    """update_active_work() uses YADGAR_CI_BRANCH when git detection fails."""

    def test_no_env_no_hint_returns_missing_branch(self, monkeypatch):
        result = _update_active_work_with_no_git_no_hint(monkeypatch, env_branch=None)
        assert result.get("error") == "missing_branch", f"expected missing_branch, got: {result}"

    def test_env_branch_resolves_to_not_missing_branch(self, monkeypatch):
        result = _update_active_work_with_no_git_no_hint(monkeypatch, env_branch="master")
        # Either the function succeeded, OR it passed the branch gate and failed on
        # StorageEngine not initialized (sentinel {'_passed_branch_gate': True}).
        # Both outcomes confirm the branch gate did NOT reject with missing_branch.
        assert result.get("error") != "missing_branch", (
            f"YADGAR_CI_BRANCH=master must prevent missing_branch in update_active_work. "
            f"Got: {result}"
        )
