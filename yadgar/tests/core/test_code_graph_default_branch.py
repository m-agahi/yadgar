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


class TestDeterministicProjectName:
    """task:0067 — the indexer project name is keyed to the REAL repo identity.

    Found by the real-binary e2e (AC-5), invisible to any mocked test: without
    an explicit ``--name`` the indexer derives the project from the indexed
    PATH, which on this flow is a random ``tempfile.mkdtemp`` worktree.  So the
    name differed on EVERY refresh — the cached index was unaddressable by any
    later run (the stale re-render could never find one, i.e. the marker would
    have shipped dead a second time) and each refresh leaked a fresh orphan
    project into the indexer's SQLite.  ADR-0162 already specified
    canonical_root + subdir as the project key; this pins it.
    """

    def test_name_is_stable_across_calls(self):
        from yadgar.core.code_graph import default_branch

        first = default_branch._project_name("/real/repo", "svc/api")
        second = default_branch._project_name("/real/repo", "svc/api")
        assert first == second, "a later offline run must recompute the SAME name"

    def test_monorepo_leaves_get_distinct_names(self):
        from yadgar.core.code_graph import default_branch

        root = "/real/monorepo"
        assert default_branch._project_name(root, "svc/a") != default_branch._project_name(
            root, "svc/b"
        )
        # …and the bare root is distinct from any leaf.
        assert default_branch._project_name(root, "") != default_branch._project_name(root, "svc/a")

    def test_name_is_indexer_safe(self):
        """Sanitise on OUR side so the indexer's ``--name`` normalisation is a no-op.

        The round-trip must be an identity: the skip path RECOMPUTES the name
        and has to match what a past index stored.
        """
        import re

        from yadgar.core.code_graph import default_branch

        name = default_branch._project_name("/real/repo", "svc/wei rd:name!")
        assert re.fullmatch(r"[A-Za-z0-9._-]+", name), name
        # The '-' separator sits outside the defang char class, so the name can
        # never form one long alphanumeric run in the digest header.
        assert "-" in name

    def test_index_is_named_after_real_identity_not_temp_path(self, tmp_path):
        from yadgar.core.code_graph import default_branch

        captured = {}

        def _fake_index(path, **kw):
            captured["name"] = kw.get("name")
            captured["path"] = path
            return {"project": kw.get("name")}

        with (
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
            patch("yadgar.core.code_graph.runner.index_repository", _fake_index),
        ):
            result = default_branch.refresh_index("/real/repo")

        expected = default_branch._project_name("/real/repo", "")
        assert captured["name"] == expected
        assert result["project"] == expected
        # The indexed PATH is still the temp worktree — only the NAME is real.
        assert captured["path"] != "/real/repo"

    def test_fetch_failure_skip_carries_the_project_name(self):
        """The skip path computes it with no index — that is what makes it usable."""
        from yadgar.core.code_graph import default_branch

        def _side(argv, **kw):
            if argv[:1] == ["fetch"]:
                raise subprocess.CalledProcessError(1, "git fetch")
            return _git_ok("deadbeefcafe0000deadbeefcafe0000deadbeef\n")

        with (
            patch("yadgar.core.code_graph.config.is_enabled", return_value=True),
            patch(
                "yadgar.core.code_graph.default_branch.resolve_default_branch",
                return_value="master",
            ),
            patch(
                "yadgar.core.code_graph.default_branch._canonical_identity",
                return_value=("/real/repo", "svc"),
            ),
            patch("yadgar.core.code_graph.default_branch._git", side_effect=_side),
            patch("yadgar.core.code_graph.runner.index_repository") as mock_idx,
        ):
            result = default_branch.refresh_index("/real/repo/svc")

        assert result["reason"] == "fetch_failed"
        assert result["project"] == default_branch._project_name("/real/repo", "svc")
        mock_idx.assert_not_called()


class TestHeadShaCapture:
    """task:0067 — ``refresh_index`` reports the sha of ``origin/<default>``.

    The sha is what ``cli/code_graph._cmd_refresh`` stamps into the digest's
    ``stale @ <sha>`` marker.  Source is ``git rev-parse origin/<default>``
    (cheap, local, needs no indexer binary) — deliberately NOT ADR-0162's
    nominal ``list_projects``/``detect_changes`` signature, which would require
    the 259 MB host-side binary and make the CI-visible seam test un-runnable.
    """

    _SHA = "0123456789abcdef0123456789abcdef01234567"

    def _dispatch(self, *, fetch_fails=False, rev_parse_fails=False):
        """Build a ``_git`` side-effect that discriminates on the git subcommand."""

        def _side(argv, **kw):
            if argv[:1] == ["fetch"] and fetch_fails:
                raise subprocess.CalledProcessError(1, "git fetch")
            if argv[:1] == ["rev-parse"]:
                if rev_parse_fails:
                    raise subprocess.CalledProcessError(128, "git rev-parse")
                return _git_ok(self._SHA + "\n")
            return _git_ok()

        return _side

    def _common(self, side):
        return (
            patch("yadgar.core.code_graph.config.is_enabled", return_value=True),
            patch(
                "yadgar.core.code_graph.default_branch.resolve_default_branch",
                return_value="master",
            ),
            patch(
                "yadgar.core.code_graph.default_branch._canonical_identity",
                return_value=("/real/repo", ""),
            ),
            patch("yadgar.core.code_graph.default_branch._git", side_effect=side),
        )

    def test_head_sha_captured_on_success(self, tmp_path):
        """AC-1: a successful index reports ``head_sha`` = rev-parse origin/<default>."""
        from yadgar.core.code_graph import default_branch

        enabled, resolve, ident, git = self._common(self._dispatch())
        with (
            enabled,
            resolve,
            ident,
            git,
            patch("yadgar.core.code_graph.runner.index_repository", return_value={"project": "p"}),
        ):
            result = default_branch.refresh_index("/real/repo")

        assert result["indexed"] is True
        assert result["head_sha"] == self._SHA

    def test_head_sha_captured_on_fetch_failure(self, tmp_path):
        """AC-2: the fetch-fail skip carries the STALE local remote-tracking sha.

        That is the honest value — it is precisely the commit the cached index
        describes.  We are offline; there is no fresher remote head to read.
        """
        from yadgar.core.code_graph import default_branch

        enabled, resolve, ident, git = self._common(self._dispatch(fetch_fails=True))
        with (
            enabled,
            resolve,
            ident,
            git,
            patch("yadgar.core.code_graph.runner.index_repository") as mock_idx,
        ):
            result = default_branch.refresh_index("/real/repo")

        assert result["skipped"] is True
        assert result["reason"] == "fetch_failed"
        assert result["head_sha"] == self._SHA
        mock_idx.assert_not_called()

    def test_no_head_sha_when_ref_missing(self, tmp_path):
        """AC-2 (negative): no resolvable ref ⇒ no sha ⇒ the CLI hard-skips."""
        from yadgar.core.code_graph import default_branch

        enabled, resolve, ident, git = self._common(
            self._dispatch(fetch_fails=True, rev_parse_fails=True)
        )
        with (
            enabled,
            resolve,
            ident,
            git,
            patch("yadgar.core.code_graph.runner.index_repository"),
        ):
            result = default_branch.refresh_index("/real/repo")

        assert result["skipped"] is True
        assert result["reason"] == "fetch_failed"
        assert not result.get("head_sha")

    def test_no_remote_skip_carries_no_head_sha(self, tmp_path):
        """``no_remote_or_default_branch`` has no ``<default>`` to interpolate."""
        from yadgar.core.code_graph import default_branch

        with (
            patch("yadgar.core.code_graph.config.is_enabled", return_value=True),
            patch(
                "yadgar.core.code_graph.default_branch.resolve_default_branch",
                return_value=None,
            ),
            patch("yadgar.core.code_graph.runner.index_repository"),
        ):
            result = default_branch.refresh_index("/real/repo")

        assert result["reason"] == "no_remote_or_default_branch"
        assert not result.get("head_sha")


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

        # Explicit is_enabled=False (e.g. an opted-out row) → skip. NOTE: since
        # ADR-0163's flip (2026-07-27) an absent row / daemon-down instead
        # defaults to enabled (True) — this test exercises the explicit-off path.
        with (
            patch("yadgar.core.code_graph.config.is_enabled", return_value=False),
            patch("yadgar.core.code_graph.runner.index_repository") as mock_idx,
        ):
            result = default_branch.refresh_index(str(tmp_path))

        assert result["skipped"] is True
        assert result["reason"] == "opted_out"
        mock_idx.assert_not_called()
