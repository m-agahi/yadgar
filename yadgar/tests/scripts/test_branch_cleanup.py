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
    def test_true_merge_into_default_is_merged_with_no_pr_data(self, tmp_path):
        """A branch that is a real ancestor of default (rare true-merge case,
        or a branch never diverged) is merged even with zero PR data."""
        repo = _init_repo(tmp_path)
        _git(repo, "branch", "feat/already-in-master")
        result = bc.classify_branches(repo, ["feat/already-in-master"], "master", pr_states={})
        assert result.merged == {"feat/already-in-master"}

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

    def test_apply_deletes_merged_branches(self, tmp_path):
        repo = _init_repo(tmp_path)
        _git(repo, "branch", "feat/gone")
        classification = bc.ClassificationResult(merged={"feat/gone"})
        bc.delete_branches(repo, classification, dry_run=False, patches_dir=tmp_path / "patches")
        branches = _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/").stdout
        assert "feat/gone" not in branches

    def test_dirty_worktree_attached_to_merged_branch_gets_patch_saved(self, tmp_path):
        """The realistic reachable case now that the classifier can say
        'merged': a worktree still checked out on a branch whose PR just
        landed, holding uncommitted scratch. Must not be force-removed
        blind — same protection sweep_worktree_age gives dirty worktrees."""
        repo = _init_repo(tmp_path)
        _git(repo, "checkout", "-b", "feat/just-merged")
        _commit(repo, "x.py", "x\n", "change")
        _git(repo, "checkout", "master")
        wt_path = tmp_path / "wt-just-merged"
        _git(repo, "worktree", "add", str(wt_path), "feat/just-merged")
        (wt_path / "x.py").write_text("uncommitted scratch\n", encoding="utf-8")
        (wt_path / "new_scratch.txt").write_text("new file\n", encoding="utf-8")

        patches_dir = tmp_path / "patches"
        classification = bc.ClassificationResult(merged={"feat/just-merged"})
        bc.delete_branches(repo, classification, dry_run=False, patches_dir=patches_dir)

        patch_matches = list(patches_dir.glob("feat_just-merged-*.patch"))
        assert patch_matches, "dirty worktree's diff must be saved before force-removal"
        assert "uncommitted scratch" in patch_matches[0].read_text(encoding="utf-8")
        untracked_matches = list(patches_dir.glob("feat_just-merged-*-untracked/new_scratch.txt"))
        assert untracked_matches, "untracked file content must be preserved"
        assert not wt_path.exists()
        branches = _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/").stdout
        assert "feat/just-merged" not in branches, "branch deletion itself is unaffected"

    def test_clean_worktree_attached_to_merged_branch_removed_without_patch(self, tmp_path):
        repo = _init_repo(tmp_path)
        _git(repo, "checkout", "-b", "feat/clean-merged")
        _commit(repo, "y.py", "y\n", "change")
        _git(repo, "checkout", "master")
        wt_path = tmp_path / "wt-clean-merged"
        _git(repo, "worktree", "add", str(wt_path), "feat/clean-merged")

        patches_dir = tmp_path / "patches"
        classification = bc.ClassificationResult(merged={"feat/clean-merged"})
        bc.delete_branches(repo, classification, dry_run=False, patches_dir=patches_dir)

        assert not wt_path.exists()
        assert not patches_dir.exists() or not list(patches_dir.iterdir())

    def test_dry_run_previews_dirty_worktree_correctly(self, tmp_path, capsys):
        """A dry-run over a dirty worktree must say so — the whole point of
        --dry-run is letting a reviewer see what a live run would actually
        do, and a dirty worktree gets different (patch-saving) treatment
        than a clean one on a live run."""
        repo = _init_repo(tmp_path)
        _git(repo, "checkout", "-b", "feat/dirty-preview")
        _commit(repo, "z.py", "z\n", "change")
        _git(repo, "checkout", "master")
        wt_path = tmp_path / "wt-dirty-preview"
        _git(repo, "worktree", "add", str(wt_path), "feat/dirty-preview")
        (wt_path / "z.py").write_text("uncommitted\n", encoding="utf-8")

        classification = bc.ClassificationResult(merged={"feat/dirty-preview"})
        bc.delete_branches(repo, classification, dry_run=True, patches_dir=tmp_path / "patches")

        captured = capsys.readouterr()
        assert "would save patch + remove dirty worktree" in captured.out
        assert wt_path.exists(), "dry-run must not touch the worktree"


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
