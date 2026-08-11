"""Write-path integration tests: worktree contexts land canonical (T2 fold-in).

Q1 orphaned-memories fix: every memory writer (memorize, anchor, checkpoint,
update_active_work) must normalize a git-worktree ``context``/``directory``
to the canonical repo root before enqueue, and pin throwaway contexts
(.claude/worktrees/*) to the default branch. The SubagentStop footer path
calls the same memorize() tool, so fixing here covers it too.

Uses a stub file queue (no engines) — payloads are asserted at the enqueue
seam. This also proves the normalization touches no storage (X1
MagicMock-storage safety: nothing on this path dereferences storage).

TDD: written before the implementation.
"""

from __future__ import annotations

import subprocess

import pytest

from yadgar.tests.core.conftest import TEST_PROJECT_ID


def _git(*args: str, cwd) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture()
def worktree_repo(tmp_path):
    repo = tmp_path / "canonical"
    repo.mkdir()
    _git("init", "-b", "master", cwd=repo)
    _git("commit", "--allow-empty", "-m", "init", cwd=repo)
    wt = repo / ".claude" / "worktrees" / "agent-test"
    wt.parent.mkdir(parents=True)
    _git("worktree", "add", str(wt), "-b", "feat/car-x", cwd=repo)
    return repo.resolve(), wt.resolve()


class _StubQueue:
    def __init__(self):
        self.jobs: list[tuple[str, dict]] = []

    def enqueue(self, op_type: str, payload: dict) -> str:
        self.jobs.append((op_type, payload))
        return f"stub-job-{len(self.jobs)}"


@pytest.fixture()
def stub_queue(monkeypatch):
    # Resolve the module objects via importlib — the tools package PEP-562 shim
    # maps "memorize" attribute lookups to the tool FUNCTION, so both string-based
    # monkeypatch paths and `import ... as` binding resolve to the wrong object.
    import importlib

    memorize_mod = importlib.import_module("yadgar.core.server.tools.memorize")
    misc_mod = importlib.import_module("yadgar.core.server.tools.misc")

    q = _StubQueue()
    monkeypatch.setattr(memorize_mod, "_get_file_queue", lambda: q)
    monkeypatch.setattr(misc_mod, "_get_file_queue", lambda: q)
    return q


# ── memorize ──────────────────────────────────────────────────────────────────


def test_memorize_from_worktree_lands_canonical(worktree_repo, stub_queue):
    from yadgar.core.server.tools.memorize import memorize

    repo, wt = worktree_repo
    result = memorize("worktree finding xyz", str(wt), ["test"], project=TEST_PROJECT_ID)
    assert result["queued"] is True
    op, payload = stub_queue.jobs[-1]
    assert op == "memorize"
    assert payload["context"] == str(repo)


def test_memorize_from_plain_repo_unchanged(worktree_repo, stub_queue):
    from yadgar.core.server.tools.memorize import memorize

    repo, _wt = worktree_repo
    result = memorize("canonical repo finding", str(repo), ["test"], project=TEST_PROJECT_ID)
    assert result["queued"] is True
    _op, payload = stub_queue.jobs[-1]
    assert payload["context"] == str(repo)


def test_memorize_non_git_context_verbatim(tmp_path, stub_queue):
    """Unresolvable context stores verbatim (current behavior preserved)."""
    from yadgar.core.server.tools.memorize import memorize

    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    result = memorize("plain dir finding", str(plain), ["test"], project=TEST_PROJECT_ID)
    assert result["queued"] is True
    _op, payload = stub_queue.jobs[-1]
    assert payload["context"] == str(plain)


# ── anchor ────────────────────────────────────────────────────────────────────


def test_anchor_from_worktree_lands_canonical(worktree_repo, stub_queue):
    from yadgar.core.server.tools.misc import anchor

    repo, wt = worktree_repo
    result = anchor("critical worktree fact", str(wt), reason="test", project=TEST_PROJECT_ID)
    assert result["queued"] is True
    op, payload = stub_queue.jobs[-1]
    assert op == "anchor"
    assert payload["context"] == str(repo)


# ── checkpoint ────────────────────────────────────────────────────────────────


def test_checkpoint_from_worktree_lands_canonical(worktree_repo, stub_queue):
    from yadgar.core.server.tools.misc import checkpoint

    repo, wt = worktree_repo
    result = checkpoint(directory=str(wt), current_task="car work", project=TEST_PROJECT_ID)
    assert result["queued"] is True
    op, payload = stub_queue.jobs[-1]
    assert op == "checkpoint"
    assert payload["directory"] == str(repo)


# ── update_active_work ────────────────────────────────────────────────────────


def test_update_active_work_from_worktree_forwards_canonical(worktree_repo, monkeypatch):
    from yadgar.core.server.tools import project as project_mod

    repo, wt = worktree_repo
    forwarded: dict = {}

    def _fake_forward(op: str, payload: dict) -> dict:
        forwarded["op"] = op
        forwarded["payload"] = payload
        return {"previous_content": None, "new_memory": {}}

    monkeypatch.setattr(project_mod, "_forward_admin", _fake_forward)
    monkeypatch.setattr(project_mod, "_register_active_work_directory", lambda _d: None)

    result = project_mod.update_active_work(str(wt), "working on car", project=TEST_PROJECT_ID)
    assert "error" not in result
    assert forwarded["op"] == "update_active_work"
    assert forwarded["payload"]["resolved"] == str(repo)
