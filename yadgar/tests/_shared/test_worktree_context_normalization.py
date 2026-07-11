"""Tests for worktree-context normalization on the memory write path (T2 fold-in).

Q1 orphaned-memories fix (docs/plans/agent-brain-learning-loop-2026-07-10.md):
memorize()/anchor()/checkpoint()/update_active_work() used to store worktree
paths verbatim as directory_context — recall's exact-match directory filter
then orphans those rows permanently once the worktree dies.

``normalize_write_context`` collapses a git-worktree write context to the
canonical repo root (the common dir's parent) and pins THROWAWAY contexts
(``.claude/worktrees/*``, ``/tmp/*``) to the repo default branch so findings
outlive the ephemeral car branch. Non-worktree contexts pass through verbatim;
unresolvable contexts NEVER reject — verbatim fallback.

TDD: written before the implementation.
"""

from __future__ import annotations

import subprocess

import pytest

from yadgar._shared import server_helpers as sh


def _git(*args: str, cwd) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture()
def worktree_repo(tmp_path):
    """Real git repo + a linked worktree under .claude/worktrees/ (the prod shape)."""
    repo = tmp_path / "canonical"
    repo.mkdir()
    _git("init", "-b", "master", cwd=repo)
    _git("commit", "--allow-empty", "-m", "init", cwd=repo)
    wt = repo / ".claude" / "worktrees" / "agent-test"
    wt.parent.mkdir(parents=True)
    _git("worktree", "add", str(wt), "-b", "feat/car-x", cwd=repo)
    return repo.resolve(), wt.resolve()


# ── _worktree_canonical_root: git-based detection ────────────────────────────


def test_worktree_path_resolves_to_canonical_root(worktree_repo):
    repo, wt = worktree_repo
    assert sh._worktree_canonical_root(str(wt)) == str(repo)


def test_plain_repo_is_not_a_worktree(worktree_repo):
    repo, _wt = worktree_repo
    assert sh._worktree_canonical_root(str(repo)) is None


def test_non_git_directory_is_not_a_worktree(tmp_path):
    plain = tmp_path / "no-git-here"
    plain.mkdir()
    assert sh._worktree_canonical_root(str(plain)) is None


def test_subdirectory_of_worktree_resolves_to_canonical_root(worktree_repo):
    repo, wt = worktree_repo
    sub = wt / "deep" / "nested"
    sub.mkdir(parents=True)
    assert sh._worktree_canonical_root(str(sub)) == str(repo)


# ── _worktree_canonical_root: path heuristics (git unavailable) ──────────────


def test_claude_worktrees_heuristic_when_git_cannot_run():
    """Nonexistent path (daemon can't run git there) still collapses via marker."""
    path = "/nonexistent-host-path/proj/.claude/worktrees/agent-1"
    assert sh._worktree_canonical_root(path) == "/nonexistent-host-path/proj"


def test_gitdir_file_heuristic_resolves_registration(tmp_path):
    """A .git FILE with a gitdir pointing under <repo>/.git/worktrees/ resolves."""
    wt = tmp_path / "throwaway-clone"
    (wt / "sub").mkdir(parents=True)
    (wt / ".git").write_text(
        "gitdir: /nonexistent-host-path/proj/.git/worktrees/agent-2\n", encoding="utf-8"
    )
    # gitdir target doesn't exist → git itself fails → filesystem heuristic
    assert sh._worktree_canonical_root(str(wt / "sub")) == "/nonexistent-host-path/proj"


def test_submodule_gitdir_file_is_not_normalized(tmp_path):
    """Submodule .git files (gitdir: .../.git/modules/...) must NOT collapse."""
    sub = tmp_path / "submodule-checkout"
    sub.mkdir()
    (sub / ".git").write_text(
        "gitdir: /nonexistent-host-path/proj/.git/modules/libfoo\n", encoding="utf-8"
    )
    assert sh._worktree_canonical_root(str(sub)) is None


# ── throwaway-context predicate ───────────────────────────────────────────────


def test_is_throwaway_context():
    assert sh._is_throwaway_context("/home/u/proj/.claude/worktrees/agent-1")
    assert sh._is_throwaway_context("/tmp/xyz/checkout")
    assert not sh._is_throwaway_context("/home/u/proj")
    assert not sh._is_throwaway_context("/home/u/worktrees/proj-wt")


# ── normalize_write_context ───────────────────────────────────────────────────


def test_normalize_throwaway_worktree_pins_default_branch(worktree_repo):
    """Throwaway worktree: canonical root + default branch (fallback 'master')."""
    repo, wt = worktree_repo
    context, branch = sh.normalize_write_context(str(wt), "feat/car-x")
    assert context == str(repo)
    assert branch == "master"


def test_normalize_non_throwaway_worktree_keeps_branch(worktree_repo, monkeypatch):
    """Intentional (long-lived) worktrees keep their branch-scoping.

    T2 Car D packaged server_helpers into a subpackage: ``sh`` is now the PEP-562
    package shim (``yadgar._shared.server_helpers``), but ``normalize_write_context``
    lives in — and calls its ``_is_throwaway_context`` sibling from — the
    ``server_helpers.server_helpers`` submodule namespace. Patching the shim attr
    never reaches the code under test, so the /tmp-hosted fixture path trips the
    real throwaway predicate and pins to master. Re-point the patch at the
    submodule seam (the actual call target) to exercise the non-throwaway branch.
    """
    repo, wt = worktree_repo
    monkeypatch.setattr(
        "yadgar._shared.server_helpers.server_helpers._is_throwaway_context",
        lambda _p: False,
    )
    context, branch = sh.normalize_write_context(str(wt), "feat/car-x")
    assert context == str(repo)
    assert branch == "feat/car-x"


def test_normalize_plain_repo_unchanged(worktree_repo):
    repo, _wt = worktree_repo
    assert sh.normalize_write_context(str(repo), "master") == (str(repo), "master")


def test_normalize_unresolvable_falls_back_verbatim(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert sh.normalize_write_context(str(plain), "feat/x") == (str(plain), "feat/x")


def test_normalize_empty_context_passthrough():
    assert sh.normalize_write_context("", "feat/x") == ("", "feat/x")


def test_normalize_never_raises(monkeypatch, worktree_repo):
    """Internal failure → verbatim fallback, never an exception (contract: NEVER reject)."""
    _repo, wt = worktree_repo

    def _boom(_p):
        raise RuntimeError("synthetic failure")

    # T2 Car D subpackage split: patch the submodule seam that
    # normalize_write_context actually calls, not the package shim attr (which
    # the internal call never resolves through).
    monkeypatch.setattr(
        "yadgar._shared.server_helpers.server_helpers._worktree_canonical_root",
        _boom,
    )
    assert sh.normalize_write_context(str(wt), "feat/car-x") == (str(wt), "feat/car-x")
