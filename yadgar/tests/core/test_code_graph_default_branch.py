"""TDD tests for code_graph default-branch temp-worktree indexing (Car B).

THE HARD CONSTRAINT: index latest origin/<default>, NEVER the working tree.
git + the binary are mocked; no real fetch/worktree/index happens.

Coverage
--------
1. Default-branch resolution: symbolic-ref → name; fallback remote show origin.
2. Temp worktree created (git worktree add --detach) + indexed with
   CBM_ALLOWED_ROOT = temp path.
3. Temp worktree ALWAYS cleaned (git worktree remove --force) — success AND error.
4. Identity key = real canonical_root + subdir, NOT the temp path.
5. Offline / no-remote / fetch-fails → SKIP (not fallback to working tree).
6. Opt-out (marker or flag) → skip, no index call.
"""

from __future__ import annotations

import subprocess
import tempfile
from unittest.mock import MagicMock, patch

import pytest


def _git_ok(stdout=""):
    cp = MagicMock()
    cp.returncode = 0
    cp.stdout = stdout
    cp.stderr = ""
    return cp


class TestDefaultBranchResolution:
    def test_resolves_via_symbolic_ref(self):
        from yadgar.core.code_graph import default_branch

        with patch("subprocess.run", return_value=_git_ok("refs/remotes/origin/main\n")):
            assert default_branch.resolve_default_branch("/repo") == "main"

    def test_falls_back_to_remote_show(self):
        from yadgar.core.code_graph import default_branch

        def _side(argv, **kw):
            if "symbolic-ref" in argv:
                raise subprocess.CalledProcessError(1, argv)
            # git remote show origin
            return _git_ok("  HEAD branch: master\n")

        with patch("subprocess.run", side_effect=_side):
            assert default_branch.resolve_default_branch("/repo") == "master"

    def test_no_remote_returns_none(self):
        from yadgar.core.code_graph import default_branch

        with patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "git")):
            assert default_branch.resolve_default_branch("/repo") is None


class TestRefreshFlow:
    """refresh_index materializes origin/<default> in a temp worktree, indexes it."""

    def _patches(self, tmp_path, index_side=None):
        """Common patch set: enabled, default branch resolves, binary present."""
        idx = MagicMock(return_value={"project": "p"}) if index_side is None else index_side
        return (
            # ADR-0163: enable via the runtime-config resolver, not env — patch
            # config.is_enabled True (is_opted_out reads it).
            patch("yadgar.core.code_graph.config.is_enabled", return_value=True),
            patch(
                "yadgar.core.code_graph.default_branch.resolve_default_branch",
                return_value="master",
            ),
            patch(
                "yadgar.core.code_graph.default_branch._canonical_identity",
                return_value=("/real/repo", ""),
            ),
            patch("yadgar.core.code_graph.default_branch._git", return_value=_git_ok()),
            patch("yadgar.core.code_graph.runner.index_repository", idx),
        )

    def test_temp_worktree_created_and_indexed_with_allowed_root(self, tmp_path):
        from yadgar.core.code_graph import default_branch

        captured = {}

        def _fake_index(path, **kw):
            captured["indexed_path"] = path
            captured["allowed_root"] = kw.get("allowed_root")
            return {"project": "p"}

        env_patch, res_patch, ident_patch, git_patch, idx_patch = self._patches(
            tmp_path, index_side=_fake_index
        )
        with env_patch, res_patch, ident_patch, git_patch as mock_git, idx_patch:
            result = default_branch.refresh_index("/real/repo")

        # git worktree add was called
        add_calls = [c for c in mock_git.call_args_list if "add" in c.args[0]]
        assert add_calls, "expected git worktree add"
        wt_path = captured["indexed_path"]
        # indexed the TEMP worktree, not the real repo
        assert wt_path != "/real/repo"
        # THE HARD CONSTRAINT ∩ containment: CBM_ALLOWED_ROOT perimeter == the
        # indexed temp path, and that path lives under the system temp dir.
        assert captured["allowed_root"] == wt_path
        assert wt_path.startswith(tempfile.gettempdir())
        assert result["indexed"] is True

    def test_temp_worktree_cleaned_on_success(self, tmp_path):
        from yadgar.core.code_graph import default_branch

        env_patch, res_patch, ident_patch, git_patch, idx_patch = self._patches(tmp_path)
        with env_patch, res_patch, ident_patch, git_patch as mock_git, idx_patch:
            default_branch.refresh_index("/real/repo")

        remove_calls = [
            c for c in mock_git.call_args_list if "worktree" in c.args[0] and "remove" in c.args[0]
        ]
        assert remove_calls, "expected git worktree remove --force in cleanup"

    def test_temp_worktree_cleaned_on_index_error(self, tmp_path):
        from yadgar.core.code_graph import default_branch

        def _boom(path, **kw):
            raise RuntimeError("index blew up")

        env_patch, res_patch, ident_patch, git_patch, idx_patch = self._patches(
            tmp_path, index_side=_boom
        )
        with env_patch, res_patch, ident_patch, git_patch as mock_git, idx_patch:
            with pytest.raises(RuntimeError):
                default_branch.refresh_index("/real/repo")

        remove_calls = [
            c for c in mock_git.call_args_list if "worktree" in c.args[0] and "remove" in c.args[0]
        ]
        assert remove_calls, "cleanup must run even when index raises"

    def test_identity_key_is_real_repo_not_temp(self, tmp_path):
        from yadgar.core.code_graph import default_branch

        env_patch, res_patch, ident_patch, git_patch, idx_patch = self._patches(tmp_path)
        with env_patch, res_patch, ident_patch, git_patch, idx_patch:
            result = default_branch.refresh_index("/real/repo")

        assert result["canonical_root"] == "/real/repo"
        assert "/tmp" not in result["canonical_root"]


class TestSkipGuards:
    def test_no_remote_skips_not_fallback(self, tmp_path):
        from yadgar.core.code_graph import default_branch

        with (
            patch("yadgar.core.code_graph.config.is_enabled", return_value=True),
            patch(
                "yadgar.core.code_graph.default_branch.resolve_default_branch",
                return_value=None,
            ),
            patch("yadgar.core.code_graph.runner.index_repository") as mock_idx,
        ):
            result = default_branch.refresh_index("/real/repo")

        assert result["skipped"] is True
        assert result["reason"] == "no_remote_or_default_branch"
        mock_idx.assert_not_called()

    def test_fetch_failure_skips_not_fallback(self, tmp_path):
        from yadgar.core.code_graph import default_branch

        with (
            patch("yadgar.core.code_graph.config.is_enabled", return_value=True),
            patch(
                "yadgar.core.code_graph.default_branch.resolve_default_branch",
                return_value="master",
            ),
            patch(
                "yadgar.core.code_graph.default_branch._git",
                side_effect=subprocess.CalledProcessError(1, "git fetch"),
            ),
            patch("yadgar.core.code_graph.runner.index_repository") as mock_idx,
        ):
            result = default_branch.refresh_index("/real/repo")

        assert result["skipped"] is True
        assert result["reason"] == "fetch_failed"
        mock_idx.assert_not_called()

    def test_per_dir_opt_out_skips(self, tmp_path):
        """ADR-0163: per-repo ``code_graph.enabled=false`` (store) → opted out for that dir."""
        from yadgar.core.code_graph import default_branch

        # is_opted_out(repo)=True when the resolver yields False for that dir.
        with (
            patch("yadgar.core.code_graph.config.is_enabled", return_value=False),
            patch("yadgar.core.code_graph.runner.index_repository") as mock_idx,
        ):
            result = default_branch.refresh_index(str(tmp_path))

        assert result["skipped"] is True
        assert result["reason"] == "opted_out"
        mock_idx.assert_not_called()

    def test_disabled_flag_skips(self, tmp_path):
        from yadgar.core.code_graph import default_branch

        # No stored row / daemon down → resolver returns default False → disabled.
        with (
            patch("yadgar.core.code_graph.config.is_enabled", return_value=False),
            patch("yadgar.core.code_graph.runner.index_repository") as mock_idx,
        ):
            result = default_branch.refresh_index(str(tmp_path))

        assert result["skipped"] is True
        assert result["reason"] == "opted_out"
        mock_idx.assert_not_called()
