"""Tests for worktree-context normalization on the memory write path (T2 fold-in).

Q1 orphaned-memories fix (docs/plans/agent-brain-learning-loop-2026-07-10.md):
memorize()/anchor()/checkpoint()/update_active_work() used to store worktree
paths verbatim as directory_context — recall's exact-match directory filter
then orphans those rows permanently once the worktree dies.

``normalize_write_context`` collapses a git-worktree write context to the
canonical repo root (the common dir's parent). Non-worktree contexts pass
through verbatim; unresolvable contexts NEVER reject — verbatim fallback.

ADR-0215 removed the branch half of the seam (throwaway contexts used to be
additionally pinned to the repo default branch); the directory normalization
this file exists for is unaffected and stays covered here.

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


# ── normalize_write_context ───────────────────────────────────────────────────


def test_normalize_worktree_resolves_to_canonical_root(worktree_repo):
    """A worktree write context collapses to the canonical repo root."""
    repo, wt = worktree_repo
    assert sh.normalize_write_context(str(wt)) == str(repo)


def test_normalize_plain_repo_unchanged(worktree_repo):
    repo, _wt = worktree_repo
    assert sh.normalize_write_context(str(repo)) == str(repo)


def test_normalize_unresolvable_falls_back_verbatim(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert sh.normalize_write_context(str(plain)) == str(plain)


def test_normalize_empty_context_passthrough():
    assert sh.normalize_write_context("") == ""


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
    assert sh.normalize_write_context(str(wt)) == str(wt)


# ── normalize_write_context: task #21 sentinel guard ────────────────────────


@pytest.mark.parametrize("sentinel", ["global", "system", "unresolved"])
def test_normalize_sentinel_identity_passthrough(sentinel, monkeypatch, tmp_path):
    """Task #21: ``normalize_write_context('global')`` (and other ADR-0227 sentinels)
    must pass through verbatim — never reach the git subprocess, never fall
    through to the path heuristic. Otherwise a CWD-coincidental ``.git`` file
    rewrites the identity into a directory on the calling process.

    The guard must trigger BEFORE any subprocess is spawned — the sentinel test
    asserts via ``_worktree_canonical_root`` that the resolver is never invoked.
    """
    import yadgar._shared.server_helpers.server_helpers as sh_mod

    called = {"n": 0}

    def _spy(_p: str):
        called["n"] += 1
        return None

    monkeypatch.setattr(sh_mod, "_worktree_canonical_root", _spy)

    # Caller CWD = inside a worktree-shaped directory. If the guard were absent,
    # the path heuristic would return the parent and rewrite the sentinel.
    cwd = tmp_path / "looks-like-a-worktree"
    cwd.mkdir()
    (cwd / ".git").write_text("gitdir: /host/proj/.git/worktrees/agent-1\n", encoding="utf-8")
    monkeypatch.chdir(cwd)

    out = sh.normalize_write_context(sentinel)
    assert out == sentinel, f"sentinel {sentinel!r} must pass through verbatim; got {out!r}"
    assert called["n"] == 0, (
        f"guard must short-circuit BEFORE _worktree_canonical_root runs; "
        f"resolver was called {called['n']} times"
    )
