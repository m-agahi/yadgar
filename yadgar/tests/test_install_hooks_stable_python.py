"""Regression: install_hooks must NOT bake a transient worktree python into
persistent hook settings.

2026-07-01 bug: install_hooks pinned ``sys.executable`` into the global
Stop/SessionEnd hook commands. When it ran inside an agent's git worktree,
``sys.executable`` was ``<repo>/.claude/worktrees/agent-<id>/.venv/bin/python3``
— an ephemeral path that broke ("No such file or directory") once the worktree
was cleaned. ``_stable_python()`` rewrites such paths back to the canonical repo
venv.
"""

from __future__ import annotations

import yadgar.core.install_hooks_lib as ihl


def test_stable_python_rewrites_worktree_path_to_canonical_venv(monkeypatch, tmp_path):
    repo = tmp_path / "yadgar"
    canonical = repo / ".venv" / "bin" / "python3"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("")  # make it exist
    worktree_exe = str(
        repo / ".claude" / "worktrees" / "agent-abc123" / ".venv" / "bin" / "python3"
    )
    monkeypatch.setattr(ihl.sys, "executable", worktree_exe)
    assert ihl._stable_python() == str(canonical)


def test_stable_python_falls_back_to_path_when_canonical_missing(monkeypatch, tmp_path):
    repo = tmp_path / "yadgar"  # no .venv created → canonical missing
    worktree_exe = str(
        repo / ".claude" / "worktrees" / "agent-abc123" / ".venv" / "bin" / "python3"
    )
    monkeypatch.setattr(ihl.sys, "executable", worktree_exe)
    assert ihl._stable_python() == "python3"


def test_stable_python_passes_normal_interpreter_unchanged(monkeypatch):
    monkeypatch.setattr(ihl.sys, "executable", "/usr/bin/python3.14")
    assert ihl._stable_python() == "/usr/bin/python3.14"


def test_shebang_never_pins_a_worktree_path(monkeypatch, tmp_path):
    repo = tmp_path / "yadgar"
    canonical = repo / ".venv" / "bin" / "python3"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("")
    worktree_exe = str(repo / ".claude" / "worktrees" / "agent-x" / ".venv" / "bin" / "python3")
    monkeypatch.setattr(ihl.sys, "executable", worktree_exe)
    shebang = ihl._resolve_python_shebang()
    assert shebang == f"#!{canonical}\n"
    assert "worktrees" not in shebang
