"""Tests for scripts/branch_cleanup.py (task 221 / ADR-0333).

Hermetic — every test builds its own throwaway git repo under tmp_path and
never touches the real yadgar repo or the network (`gh` is never invoked;
PR state is always injected as a plain dict).

Two things this file exists to prove:

1. The classifier can actually say "merged" for real squash-merge +
   multi-level-car-branch topology (the failure mode `deleted=0 unmerged=170`
   came from a classifier that could never say "merged" for anything whose
   base predated the default branch's tip). A mutation to a stub that always
   returns unmerged must fail these tests — see
   ``TestMutationCheck``.
2. The worktree-age sweep never deletes a branch, never force-removes a
   dirty worktree without saving its diff first, and defaults to dry-run.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.parent.parent


def _load(script_name: str):
    script_path = _REPO_ROOT / "scripts" / script_name
    spec = importlib.util.spec_from_file_location(script_name.replace(".py", ""), script_path)
    assert spec and spec.loader, f"Cannot load {script_path}"
    mod = importlib.util.module_from_spec(spec)
    # Register in sys.modules BEFORE exec: branch_cleanup.py defines
    # @dataclass classes, and dataclass's forward-ref resolution looks the
    # module up via sys.modules.get(cls.__module__) — an unregistered module
    # makes that lookup return None and crash (see test_check_ledger_chokepoint.py).
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


bc = _load("branch_cleanup.py")


# ── repo-building helpers ────────────────────────────────────────────────────


def _git(repo: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, env=full_env, check=True
    )
    return result


def _commit(repo: Path, path: str, content: str, message: str, when: float | None = None) -> str:
    f = repo / path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(content, encoding="utf-8")
    _git(repo, "add", path)
    env = None
    if when is not None:
        date_str = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(when)) + "Z"
        env = {"GIT_AUTHOR_DATE": date_str, "GIT_COMMITTER_DATE": date_str}
    _git(repo, "commit", "-m", message, env=env)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _init_repo(tmp_path: Path, name: str = "origin_repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "master")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    _commit(repo, "README.md", "hello\n", "initial commit")
    return repo


# ── phase 1: classify_branches ───────────────────────────────────────────────


class TestClassifyDirectPRState:
    def test_merged_pr_state_wins(self, tmp_path):
        repo = _init_repo(tmp_path)
        _git(repo, "branch", "feat/thing")
        result = bc.classify_branches(
            repo, ["feat/thing"], "master", pr_states={"feat/thing": "MERGED"}
        )
        assert result.merged == {"feat/thing"}
        assert result.unmerged == set()

    def test_open_pr_state_is_never_merged(self, tmp_path):
        repo = _init_repo(tmp_path)
        _git(repo, "branch", "feat/thing")
        result = bc.classify_branches(
            repo, ["feat/thing"], "master", pr_states={"feat/thing": "OPEN"}
        )
        assert result.unmerged == {"feat/thing"}
        assert result.merged == set()

    def test_closed_unmerged_pr_state_is_unmerged(self, tmp_path):
        repo = _init_repo(tmp_path)
        _git(repo, "branch", "feat/abandoned")
        result = bc.classify_branches(
            repo, ["feat/abandoned"], "master", pr_states={"feat/abandoned": "CLOSED"}
        )
        assert result.unmerged == {"feat/abandoned"}


class TestClassifyTransitiveAncestry:
    def test_zero_commit_branch_is_empty_not_merged(self, tmp_path):
        """Bug found in live review, 2026-08-19: a branch created off
        default with no commits of its own (exactly what EnterWorktree
        stamps for every fresh agent worktree, before the agent's first
        commit) is trivially `is_ancestor(branch, default)` — but it was
        never "merged" because nothing was ever contributed. The old
        behavior classified it merged, which fed straight into
        delete_branches force-removing whatever worktree happened to be
        attached — at the time, an agent's LIVE working directory."""
        repo = _init_repo(tmp_path)
        _git(repo, "branch", "feat/never-touched")
        result = bc.classify_branches(repo, ["feat/never-touched"], "master", pr_states={})
        assert result.empty == {"feat/never-touched"}
        assert result.merged == set()

    def test_car_subbranch_merged_into_train_branch_is_merged(self, tmp_path):
        """Reproduces the actual repo topology (ADR-0333): a car sub-branch
        merges via a REAL git merge into a train-integration branch, which
        alone gets squash-merged into master. The car branch itself has no
        PR — it must still be classified merged, transitively, via the
        integration branch's PR state."""
        repo = _init_repo(tmp_path)
        _git(repo, "checkout", "-b", "car/A-thing")
        _commit(repo, "a.py", "car A content\n", "car A change")
        _git(repo, "checkout", "master")
        _git(repo, "checkout", "-b", "feat/train")
        _git(repo, "merge", "--no-ff", "car/A-thing", "-m", "merge car A into train")
        # Squash the train branch into master, simulating a squash-merged PR —
        # master's tree now has the content but NOT the ancestry chain.
        _git(repo, "checkout", "master")
        _git(repo, "merge", "--squash", "feat/train")
        _git(repo, "commit", "-m", "train(squash): land car A")

        assert not bc.is_ancestor(repo, "feat/train", "master"), (
            "fixture invariant broken: squash merge must NOT be a real ancestor"
        )
        assert bc.is_ancestor(repo, "car/A-thing", "feat/train"), (
            "fixture invariant broken: car branch must be a real ancestor of "
            "the train branch it merged into"
        )

        result = bc.classify_branches(
            repo,
            ["car/A-thing", "feat/train"],
            "master",
            pr_states={"feat/train": "MERGED"},  # only the integration branch has a PR
        )
        assert result.merged == {"car/A-thing", "feat/train"}
        assert "feat/train" in result.reasons["car/A-thing"]

    def test_unrelated_branch_with_no_pr_stays_unmerged(self, tmp_path):
        """The conservative default: no PR record and not an ancestor of
        anything known-merged -> unmerged. This is the case that must NOT
        silently flip to merged."""
        repo = _init_repo(tmp_path)
        _git(repo, "checkout", "-b", "feat/still-open")
        _commit(repo, "b.py", "still in progress\n", "wip")
        _git(repo, "checkout", "master")

        result = bc.classify_branches(repo, ["feat/still-open"], "master", pr_states={})
        assert result.unmerged == {"feat/still-open"}
        assert result.merged == set()


class TestClassifyEmptyBranchGate:
    """Bug found in live review, 2026-08-19: a fresh worktree branch with
    zero commits ahead of the default branch is trivially `is_ancestor`,
    so it used to be classified "merged" with no direct-PR record to
    override it -- and `delete_branches` would then force-remove whichever
    worktree happened to be attached, at the time an agent's live working
    directory. `has_unique_commits` gates the empty case out of layer 2
    entirely (both as a merge-candidate and as a transitive-merge target)
    before it can be misread as integrated content.
    """

    def test_empty_branch_with_no_pr_is_empty_not_merged(self, tmp_path):
        repo = _init_repo(tmp_path)
        _git(repo, "checkout", "-b", "worktree-agent-fresh")
        _git(repo, "checkout", "master")

        result = bc.classify_branches(repo, ["worktree-agent-fresh"], "master", pr_states={})
        assert result.empty == {"worktree-agent-fresh"}
        assert result.merged == set()
        assert result.unmerged == set()

    def test_empty_branch_with_direct_merged_pr_still_trusts_pr(self, tmp_path):
        """Layer 1 (gh PR state) is ground truth and applies regardless of
        commit count -- an empty branch with a real MERGED PR record (e.g.
        a no-op PR) should not be second-guessed by the empty gate."""
        repo = _init_repo(tmp_path)
        _git(repo, "checkout", "-b", "docs/noop-pr")
        _git(repo, "checkout", "master")

        result = bc.classify_branches(
            repo, ["docs/noop-pr"], "master", pr_states={"docs/noop-pr": "MERGED"}
        )
        assert result.merged == {"docs/noop-pr"}
        assert result.empty == set()

    def test_empty_branch_is_never_used_as_a_transitive_merge_target(self, tmp_path):
        """An empty branch must not license classifying some OTHER branch
        as merged "because it descends from" the empty branch -- it has no
        content to have integrated anything."""
        repo = _init_repo(tmp_path)
        _git(repo, "checkout", "-b", "worktree-agent-empty-target")
        _git(repo, "checkout", "master")
        _git(repo, "checkout", "-b", "feat/unrelated-work")
        _commit(repo, "w.py", "w\n", "real work")
        _git(repo, "checkout", "master")

        result = bc.classify_branches(
            repo,
            ["worktree-agent-empty-target", "feat/unrelated-work"],
            "master",
            pr_states={},
        )
        assert result.empty == {"worktree-agent-empty-target"}
        assert result.unmerged == {"feat/unrelated-work"}

    def test_delete_branches_never_deletes_an_empty_branch(self, tmp_path):
        """Empty branches never even reach delete_branches's consideration
        -- they live in .empty, not .merged, so the attached-worktree-skip
        guard is defense in depth, not the only guard."""
        repo = _init_repo(tmp_path)
        _git(repo, "checkout", "-b", "worktree-agent-live")
        _git(repo, "checkout", "master")
        wt_path = tmp_path / "wt-live"
        _git(repo, "worktree", "add", str(wt_path), "worktree-agent-live")

        result = bc.classify_branches(repo, ["worktree-agent-live"], "master", pr_states={})
        assert result.empty == {"worktree-agent-live"}

        deleted, skipped = bc.delete_branches(repo, result, dry_run=False)
        assert deleted == 0
        assert skipped == 0  # never even a merge candidate
        assert wt_path.exists(), "live worktree on the empty branch must survive"
        branches = _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/").stdout
        assert "worktree-agent-live" in branches


class TestClassifyPreserveAndSkip:
    def test_preserve_glob_wins_over_everything(self, tmp_path):
        repo = _init_repo(tmp_path)
        _git(repo, "branch", "feat/v5.9-stage-03")
        result = bc.classify_branches(
            repo,
            ["feat/v5.9-stage-03"],
            "master",
            pr_states={"feat/v5.9-stage-03": "MERGED"},
            preserve_glob="feat/v?.?-stage-*",
        )
        assert result.preserved == {"feat/v5.9-stage-03"}
        assert result.merged == set()

    def test_skip_set_excludes_current_branch(self, tmp_path):
        repo = _init_repo(tmp_path)
        _git(repo, "branch", "feat/checked-out")
        result = bc.classify_branches(
            repo,
            ["feat/checked-out"],
            "master",
            pr_states={"feat/checked-out": "MERGED"},
            skip={"feat/checked-out"},
        )
        assert result.merged == set()
        assert result.unmerged == set()
        assert result.preserved == set()


class TestMutationCheck:
    """Prove the classifier is capable of saying 'merged' at all.

    A stub classifier that always returns unmerged (the actual bug this car
    fixes) would pass every "stays unmerged" test above but fail here — this
    is the red/green boundary the task brief asked for.
    """

    def test_classifier_reports_merged_for_a_real_squash_topology(self, tmp_path):
        repo = _init_repo(tmp_path)
        _git(repo, "checkout", "-b", "feat/train2")
        _commit(repo, "c.py", "content\n", "change")
        _git(repo, "checkout", "master")
        _git(repo, "merge", "--squash", "feat/train2")
        _git(repo, "commit", "-m", "squash landed")

        result = bc.classify_branches(
            repo, ["feat/train2"], "master", pr_states={"feat/train2": "MERGED"}
        )
        assert result.merged, "classifier must be able to report at least one merged branch"
        assert "feat/train2" in result.merged

    def test_broken_two_dot_classifier_would_have_missed_this(self, tmp_path):
        """Sanity check that the fixture actually reproduces the original
        bug: the OLD two-dot tree-diff approach reports this exact scenario
        as unmerged, which is precisely why it was replaced."""
        repo = _init_repo(tmp_path)
        _git(repo, "checkout", "-b", "feat/train3")
        _commit(repo, "d.py", "content\n", "change")
        _git(repo, "checkout", "master")
        _git(repo, "merge", "--squash", "feat/train3")
        _git(repo, "commit", "-m", "squash landed")
        # Advance master further so the old two-dot compare sees drift.
        _commit(repo, "unrelated.py", "noise\n", "unrelated later commit")

        old_style = subprocess.run(
            ["git", "diff", "--quiet", "master..feat/train3", "--", ".", ":!*.md"],
            cwd=repo,
        )
        assert old_style.returncode != 0, (
            "fixture invariant broken: the old two-dot diff was expected to "
            "misreport this as unmerged"
        )


# ── delete_branches ───────────────────────────────────────────────────────────


class TestDeleteBranches:
    def test_dry_run_deletes_nothing(self, tmp_path):
        repo = _init_repo(tmp_path)
        _git(repo, "branch", "feat/gone")
        classification = bc.ClassificationResult(merged={"feat/gone"})
        bc.delete_branches(repo, classification, dry_run=True)
        branches = _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/").stdout
        assert "feat/gone" in branches

    def test_apply_deletes_merged_branches_with_no_worktree(self, tmp_path):
        repo = _init_repo(tmp_path)
        _git(repo, "branch", "feat/gone")
        classification = bc.ClassificationResult(merged={"feat/gone"})
        bc.delete_branches(repo, classification, dry_run=False)
        branches = _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/").stdout
        assert "feat/gone" not in branches

    def test_git_refusal_is_counted_accurately_not_as_success(self, tmp_path, capsys):
        """Found while mutation-testing the attached-worktree guard: with
        that guard disabled, `git branch -D` on a branch still checked out
        elsewhere fails (git's own protection), yet the old code
        incremented `deleted` unconditionally right after the `_run` call
        regardless of its return code — a no-op counted as a success.
        `deleted` must reflect what git actually did."""
        repo = _init_repo(tmp_path)
        _git(repo, "branch", "feat/refused")
        classification = bc.ClassificationResult(merged={"feat/refused"})

        real_run = bc._run

        def fake_run(args, cwd, timeout=None):
            if args[:3] == ["git", "branch", "-D"]:
                # Simulate git refusing — do NOT actually run the command.
                return subprocess.CompletedProcess(
                    args, returncode=1, stdout="", stderr="simulated git refusal"
                )
            return real_run(args, cwd, timeout)

        import unittest.mock

        with unittest.mock.patch.object(bc, "_run", side_effect=fake_run):
            deleted, _ = bc.delete_branches(repo, classification, dry_run=False)

        assert deleted == 0, "a git-refused delete must not be counted as deleted"
        branches = _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/").stdout
        assert "feat/refused" in branches, "branch must survive a refused delete"
        captured = capsys.readouterr()
        assert "WARNING" in captured.err

    def test_branch_with_attached_worktree_is_never_deleted_clean(self, tmp_path):
        """Bug found in live review, 2026-08-19: a CLEAN worktree can still
        be a running agent's live working directory. delete_branches must
        not touch it or its branch, full stop, independent of dirty state
        — that decision belongs to sweep_worktree_age (age-gated), not to
        merge classification."""
        repo = _init_repo(tmp_path)
        _git(repo, "checkout", "-b", "feat/clean-merged")
        _commit(repo, "y.py", "y\n", "change")
        _git(repo, "checkout", "master")
        wt_path = tmp_path / "wt-clean-merged"
        _git(repo, "worktree", "add", str(wt_path), "feat/clean-merged")

        classification = bc.ClassificationResult(merged={"feat/clean-merged"})
        deleted, skipped_attached = bc.delete_branches(repo, classification, dry_run=False)

        assert wt_path.exists(), "worktree must survive untouched"
        branches = _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/").stdout
        assert "feat/clean-merged" in branches, "branch must survive untouched"
        assert deleted == 0
        assert skipped_attached == 1

    def test_branch_with_attached_worktree_is_never_deleted_dirty(self, tmp_path):
        """Same guard, but with uncommitted scratch in the worktree — the
        realistic case that motivated this fix: an agent mid-task, PR for
        its branch already merged, worktree still live."""
        repo = _init_repo(tmp_path)
        _git(repo, "checkout", "-b", "feat/just-merged")
        _commit(repo, "x.py", "x\n", "change")
        _git(repo, "checkout", "master")
        wt_path = tmp_path / "wt-just-merged"
        _git(repo, "worktree", "add", str(wt_path), "feat/just-merged")
        (wt_path / "x.py").write_text("uncommitted scratch\n", encoding="utf-8")
        (wt_path / "new_scratch.txt").write_text("new file\n", encoding="utf-8")

        classification = bc.ClassificationResult(merged={"feat/just-merged"})
        bc.delete_branches(repo, classification, dry_run=False)

        assert wt_path.exists(), "worktree (and its uncommitted scratch) must survive"
        assert (wt_path / "x.py").read_text(encoding="utf-8") == "uncommitted scratch\n"
        assert (wt_path / "new_scratch.txt").exists()
        branches = _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/").stdout
        assert "feat/just-merged" in branches

    def test_dry_run_previews_the_skip_too(self, tmp_path, capsys):
        repo = _init_repo(tmp_path)
        _git(repo, "checkout", "-b", "feat/attached")
        _commit(repo, "z.py", "z\n", "change")
        _git(repo, "checkout", "master")
        wt_path = tmp_path / "wt-attached"
        _git(repo, "worktree", "add", str(wt_path), "feat/attached")

        classification = bc.ClassificationResult(merged={"feat/attached"})
        bc.delete_branches(repo, classification, dry_run=True)

        captured = capsys.readouterr()
        assert "skip (worktree attached" in captured.out
        assert wt_path.exists()


# ── phase 2: worktree-age sweep ───────────────────────────────────────────────


def _add_worktree(repo: Path, wt_path: Path, branch: str, base: str = "master") -> None:
    _git(repo, "worktree", "add", str(wt_path), "-b", branch, base)


class TestWorktreeAgeSweep:
    def test_dry_run_default_removes_nothing(self, tmp_path):
        repo = _init_repo(tmp_path)
        old_epoch = time.time() - 30 * 86400
        _git(repo, "checkout", "-b", "wt/old")
        _commit(repo, "old.py", "x\n", "old change", when=old_epoch)
        _git(repo, "checkout", "master")
        wt_path = tmp_path / "wt-old"
        _git(repo, "worktree", "add", str(wt_path), "wt/old")

        actions = bc.sweep_worktree_age(
            repo, max_age_days=14, dry_run=True, patches_dir=tmp_path / "patches"
        )
        assert wt_path.is_dir(), "dry-run must not remove the worktree directory"
        assert any(a["branch_name"] == "wt/old" and not a["removed"] for a in actions)

    def test_apply_removes_old_clean_worktree_but_keeps_branch(self, tmp_path):
        repo = _init_repo(tmp_path)
        old_epoch = time.time() - 30 * 86400
        _git(repo, "checkout", "-b", "wt/old2")
        _commit(repo, "old2.py", "x\n", "old change", when=old_epoch)
        _git(repo, "checkout", "master")
        wt_path = tmp_path / "wt-old2"
        _git(repo, "worktree", "add", str(wt_path), "wt/old2")

        actions = bc.sweep_worktree_age(
            repo, max_age_days=14, dry_run=False, patches_dir=tmp_path / "patches"
        )
        assert not wt_path.exists(), "old clean worktree must be removed on apply"
        branches = _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/").stdout
        assert "wt/old2" in branches, "branch must always survive worktree removal"
        assert any(a["branch_name"] == "wt/old2" and a["removed"] for a in actions)

    def test_recent_worktree_is_never_touched(self, tmp_path):
        repo = _init_repo(tmp_path)
        _git(repo, "checkout", "-b", "wt/fresh")
        _commit(repo, "fresh.py", "x\n", "fresh change")  # now
        _git(repo, "checkout", "master")
        wt_path = tmp_path / "wt-fresh"
        _git(repo, "worktree", "add", str(wt_path), "wt/fresh")

        actions = bc.sweep_worktree_age(
            repo, max_age_days=14, dry_run=False, patches_dir=tmp_path / "patches"
        )
        assert wt_path.is_dir()
        assert not any(a["branch_name"] == "wt/fresh" for a in actions)

    def test_dirty_worktree_patch_saved_before_removal(self, tmp_path):
        repo = _init_repo(tmp_path)
        old_epoch = time.time() - 30 * 86400
        _git(repo, "checkout", "-b", "wt/dirty")
        _commit(repo, "dirty.py", "x\n", "old change", when=old_epoch)
        _git(repo, "checkout", "master")
        wt_path = tmp_path / "wt-dirty"
        _git(repo, "worktree", "add", str(wt_path), "wt/dirty")
        # uncommitted tracked-file change + a new untracked file
        (wt_path / "dirty.py").write_text("uncommitted edit\n", encoding="utf-8")
        (wt_path / "scratch.txt").write_text("new file content\n", encoding="utf-8")

        patches_dir = tmp_path / "patches"
        actions = bc.sweep_worktree_age(
            repo, max_age_days=14, dry_run=False, patches_dir=patches_dir
        )
        action = next(a for a in actions if a["branch_name"] == "wt/dirty")
        assert action["dirty"] is True
        assert action["patch_saved"] is not None
        patch_path = Path(action["patch_saved"])
        assert patch_path.exists()
        assert "uncommitted edit" in patch_path.read_text(encoding="utf-8")
        # untracked file content preserved alongside the patch
        untracked_matches = list(patches_dir.glob("wt_dirty-*-untracked/scratch.txt"))
        assert untracked_matches, "untracked file content must be preserved"
        assert untracked_matches[0].read_text(encoding="utf-8") == "new file content\n"
        assert not wt_path.exists()
        branches = _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/").stdout
        assert "wt/dirty" in branches

    def test_venv_noise_does_not_count_as_dirty(self, tmp_path):
        repo = _init_repo(tmp_path)
        old_epoch = time.time() - 30 * 86400
        _git(repo, "checkout", "-b", "wt/venv-noise")
        _commit(repo, "clean.py", "x\n", "old change", when=old_epoch)
        _git(repo, "checkout", "master")
        wt_path = tmp_path / "wt-venv-noise"
        _git(repo, "worktree", "add", str(wt_path), "wt/venv-noise")
        (wt_path / ".venv" / "lib").mkdir(parents=True)
        (wt_path / ".venv" / "lib" / "junk.txt").write_text("noise\n", encoding="utf-8")

        dirty, untracked = bc.is_worktree_dirty(wt_path)
        assert dirty is False
        assert untracked == []

        actions = bc.sweep_worktree_age(
            repo, max_age_days=14, dry_run=False, patches_dir=tmp_path / "patches"
        )
        action = next(a for a in actions if a["branch_name"] == "wt/venv-noise")
        assert action["dirty"] is False
        assert action["removed"] is True

    def test_main_worktree_is_never_a_candidate(self, tmp_path):
        repo = _init_repo(tmp_path)
        old_epoch = time.time() - 400 * 86400
        _commit(repo, "ancient.py", "x\n", "ancient change", when=old_epoch)

        actions = bc.sweep_worktree_age(
            repo, max_age_days=14, dry_run=False, patches_dir=tmp_path / "patches"
        )
        assert repo.exists()
        assert not any(Path(a["path"]) == repo for a in actions)


class TestMutationCheckWorktreeSweep:
    """A sweep that never removes anything (e.g. dry_run hardcoded True, or
    the age comparison inverted) would pass every 'nothing happens' test
    above but fail here."""

    def test_sweep_is_capable_of_actually_removing_a_worktree(self, tmp_path):
        repo = _init_repo(tmp_path)
        old_epoch = time.time() - 60 * 86400
        _git(repo, "checkout", "-b", "wt/must-be-removable")
        _commit(repo, "x.py", "x\n", "old", when=old_epoch)
        _git(repo, "checkout", "master")
        wt_path = tmp_path / "wt-removable"
        _git(repo, "worktree", "add", str(wt_path), "wt/must-be-removable")

        actions = bc.sweep_worktree_age(
            repo, max_age_days=14, dry_run=False, patches_dir=tmp_path / "patches"
        )
        assert any(a["removed"] for a in actions), (
            "sweep must be capable of actually removing an old, clean worktree"
        )
