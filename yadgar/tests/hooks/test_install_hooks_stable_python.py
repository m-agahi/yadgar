"""Regression: install_hooks must NOT bake a transient interpreter into
persistent hook settings.

2026-07-01 bug: install_hooks pinned ``sys.executable`` into the global
Stop/SessionEnd hook commands. When it ran inside an agent's git worktree,
``sys.executable`` was ``<repo>/.claude/worktrees/agent-<id>/.venv/bin/python3``
— an ephemeral path that broke ("No such file or directory") once the worktree
was cleaned. ``_stable_python()`` rewrites such paths back to a durable
interpreter.

2026-07-10 bug (task #38, 3rd user-facing occurrence): the original guard only
matched ``.claude/worktrees/`` paths. Agent worktrees under ``/tmp`` (or any
linked git worktree elsewhere) escaped it and still poisoned
``~/.claude/settings.json``. ``_stable_python()`` now detects ANY non-durable
interpreter (tmp paths, linked git worktrees) and substitutes, in order:
existing healthy+durable registration → pipx venv → canonical repo .venv →
keep the existing registration unchanged (warn) → PATH ``python3``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

import yadgar.core.install_hooks_lib as ihl

# ── fixtures / helpers ───────────────────────────────────────────────────────


def _make_pipx_python(home: Path) -> Path:
    pipx_bin = home / ".local" / "pipx" / "venvs" / "yadgar" / "bin"
    pipx_bin.mkdir(parents=True)
    pipx_python = pipx_bin / "python"
    pipx_python.write_text("")
    return pipx_python


def _init_repo_with_worktree(base: Path) -> tuple[Path, Path]:
    """Create a real git repo + linked worktree; return (repo, worktree)."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(base),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    repo = base / "repo"
    repo.mkdir()

    def _git(*args: str, cwd: Path) -> None:
        subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True)

    _git("init", "-q", cwd=repo)
    _git("commit", "--allow-empty", "-m", "init", cwd=repo)
    worktree = base / "wt"
    _git("worktree", "add", "-q", str(worktree), cwd=repo)
    return repo, worktree


# ── _is_durable_interpreter ──────────────────────────────────────────────────


def test_is_durable_rejects_tmp_path():
    assert ihl._is_durable_interpreter("/tmp/wt-xyz/.venv/bin/python3") is False


def test_is_durable_rejects_claude_worktree_marker():
    exe = "/home/u/repo/.claude/worktrees/agent-abc/.venv/bin/python3"
    assert ihl._is_durable_interpreter(exe) is False


def test_is_durable_accepts_relative_name():
    # PATH-resolved names can never dangle on an absolute doomed path.
    assert ihl._is_durable_interpreter("python3") is True


def test_is_durable_accepts_system_python():
    assert ihl._is_durable_interpreter("/usr/bin/python3.14") is True


# ── _is_git_worktree_path ────────────────────────────────────────────────────


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
def test_is_git_worktree_path_detects_linked_worktree(tmp_path):
    _repo, worktree = _init_repo_with_worktree(tmp_path)
    exe = worktree / ".venv" / "bin" / "python3"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    assert ihl._is_git_worktree_path(str(exe)) is True


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
def test_is_git_worktree_path_false_for_main_checkout(tmp_path):
    repo, _worktree = _init_repo_with_worktree(tmp_path)
    exe = repo / ".venv" / "bin" / "python3"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    assert ihl._is_git_worktree_path(str(exe)) is False


def test_is_git_worktree_path_false_for_missing_dir(tmp_path):
    assert ihl._is_git_worktree_path(str(tmp_path / "nope" / "python3")) is False


# ── _stable_python: substitution chain ───────────────────────────────────────


def test_stable_python_never_returns_tmp_interpreter(monkeypatch, tmp_path):
    """A /tmp worktree python must NEVER survive into the result."""
    doomed = "/tmp/claude-doomed-wt/.venv/bin/python3"
    monkeypatch.setattr(ihl.sys, "executable", doomed)
    home = tmp_path / "home"
    home.mkdir()
    result = ihl._stable_python(home_dir=home)
    assert result != doomed
    assert result == "python3"  # nothing durable available → PATH fallback


def test_stable_python_prefers_healthy_durable_existing(monkeypatch, tmp_path):
    system_python = shutil.which("python3")
    assert system_python, "test requires python3 on PATH"
    monkeypatch.setattr(ihl.sys, "executable", "/tmp/claude-doomed-wt/.venv/bin/python3")
    home = tmp_path / "home"
    _make_pipx_python(home)  # present, but existing registration must win
    result = ihl._stable_python(existing=system_python, home_dir=home)
    assert result == system_python


def test_stable_python_skips_dead_existing_falls_to_pipx(monkeypatch, tmp_path):
    monkeypatch.setattr(ihl.sys, "executable", "/tmp/claude-doomed-wt/.venv/bin/python3")
    home = tmp_path / "home"
    pipx_python = _make_pipx_python(home)
    dead = str(tmp_path / "gone" / "python3")
    result = ihl._stable_python(existing=dead, home_dir=home)
    assert result == str(pipx_python)


def test_stable_python_replaces_alive_but_nondurable_existing(monkeypatch, tmp_path):
    """An existing registration that is itself a still-alive tmp/worktree python
    is poison-in-waiting — heal it instead of keeping it."""
    monkeypatch.setattr(ihl.sys, "executable", "/tmp/claude-doomed-wt/.venv/bin/python3")
    home = tmp_path / "home"
    pipx_python = _make_pipx_python(home)
    alive_but_doomed = tmp_path / "wt" / ".venv" / "bin" / "python3"  # under /tmp (pytest)
    alive_but_doomed.parent.mkdir(parents=True)
    alive_but_doomed.write_text("")
    result = ihl._stable_python(existing=str(alive_but_doomed), home_dir=home)
    assert result == str(pipx_python)


def test_stable_python_keeps_dead_existing_when_nothing_durable(monkeypatch, tmp_path):
    """(d) fallback: never write a NEW doomed path — leave registration unchanged."""
    monkeypatch.setattr(ihl.sys, "executable", "/tmp/claude-doomed-wt/.venv/bin/python3")
    home = tmp_path / "home"
    home.mkdir()  # no pipx
    dead = str(tmp_path / "gone" / "python3")
    result = ihl._stable_python(existing=dead, home_dir=home)
    assert result == dead


def test_stable_python_pipx_beats_canonical_venv(monkeypatch, tmp_path):
    repo = tmp_path / "yadgar"
    canonical = repo / ".venv" / "bin" / "python3"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("")
    worktree_exe = str(
        repo / ".claude" / "worktrees" / "agent-abc123" / ".venv" / "bin" / "python3"
    )
    monkeypatch.setattr(ihl.sys, "executable", worktree_exe)
    home = tmp_path / "home"
    pipx_python = _make_pipx_python(home)
    assert ihl._stable_python(home_dir=home) == str(pipx_python)


def test_stable_python_rewrites_worktree_path_to_canonical_venv(monkeypatch, tmp_path):
    repo = tmp_path / "yadgar"
    canonical = repo / ".venv" / "bin" / "python3"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("")  # make it exist
    worktree_exe = str(
        repo / ".claude" / "worktrees" / "agent-abc123" / ".venv" / "bin" / "python3"
    )
    monkeypatch.setattr(ihl.sys, "executable", worktree_exe)
    home = tmp_path / "home"
    home.mkdir()  # no pipx → canonical wins
    assert ihl._stable_python(home_dir=home) == str(canonical)


def test_stable_python_falls_back_to_path_when_canonical_missing(monkeypatch, tmp_path):
    repo = tmp_path / "yadgar"  # no .venv created → canonical missing
    worktree_exe = str(
        repo / ".claude" / "worktrees" / "agent-abc123" / ".venv" / "bin" / "python3"
    )
    monkeypatch.setattr(ihl.sys, "executable", worktree_exe)
    home = tmp_path / "home"
    home.mkdir()  # no pipx
    assert ihl._stable_python(home_dir=home) == "python3"


def test_stable_python_passes_normal_interpreter_unchanged(monkeypatch):
    monkeypatch.setattr(ihl.sys, "executable", "/usr/bin/python3.14")
    assert ihl._stable_python() == "/usr/bin/python3.14"


# ── shebang resolution ───────────────────────────────────────────────────────


def test_shebang_never_pins_a_worktree_path(monkeypatch, tmp_path):
    repo = tmp_path / "yadgar"
    canonical = repo / ".venv" / "bin" / "python3"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("")
    worktree_exe = str(repo / ".claude" / "worktrees" / "agent-x" / ".venv" / "bin" / "python3")
    monkeypatch.setattr(ihl.sys, "executable", worktree_exe)
    home = tmp_path / "home"
    home.mkdir()
    shebang = ihl._resolve_python_shebang(ihl._stable_python(home_dir=home))
    assert shebang == f"#!{canonical}\n"
    assert "worktrees" not in shebang


def test_shebang_relative_python_uses_env_form():
    """A PATH-relative fallback must not produce an invalid absolute shebang."""
    assert ihl._resolve_python_shebang("python3") == "#!/usr/bin/env python3\n"


# ── install_hooks_impl integration ───────────────────────────────────────────


def test_install_hooks_impl_never_writes_tmp_interpreter(monkeypatch, tmp_path):
    doomed = "/tmp/claude-doomed-wt/.venv/bin/python3"
    monkeypatch.setattr(ihl.sys, "executable", doomed)
    home = tmp_path / "home"
    home.mkdir()
    proj = tmp_path / "proj"
    proj.mkdir()

    result = ihl.install_hooks_impl(home, "project", str(proj))
    assert result["status"] == "installed"

    for settings_file in (
        proj / ".claude" / "settings.json",
        home / ".claude" / "settings.json",
    ):
        assert settings_file.exists()
        assert doomed not in settings_file.read_text()

    # Installed hook scripts must not carry the doomed shebang either.
    for hooks_dir in (proj / ".claude" / "hooks", home / ".claude" / "hooks"):
        for script in hooks_dir.glob("*.py"):
            first_line = script.read_text().splitlines()[0]
            assert doomed not in first_line


def test_install_hooks_impl_preserves_existing_healthy_registration(monkeypatch, tmp_path):
    system_python = shutil.which("python3")
    assert system_python, "test requires python3 on PATH"
    doomed = "/tmp/claude-doomed-wt/.venv/bin/python3"
    monkeypatch.setattr(ihl.sys, "executable", doomed)
    home = tmp_path / "home"
    home.mkdir()
    proj = tmp_path / "proj"
    claude_dir = proj / ".claude"
    claude_dir.mkdir(parents=True)
    existing_settings = {
        "hooks": {
            "PreCompact": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{system_python} /old/hooks/hook_runner.py pre-compact-drain",
                        }
                    ],
                }
            ]
        }
    }
    (claude_dir / "settings.json").write_text(__import__("json").dumps(existing_settings))

    result = ihl.install_hooks_impl(home, "project", str(proj))
    assert result["status"] == "installed"

    import json

    written = json.loads((claude_dir / "settings.json").read_text())
    pre_compact_cmd = written["hooks"]["PreCompact"][0]["hooks"][0]["command"]
    assert pre_compact_cmd.startswith(system_python)
    assert doomed not in json.dumps(written)


def test_registered_python_extracts_interpreter():
    settings = {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "/opt/venv/bin/python3 /h/hook_runner.py session-start-context",
                        }
                    ],
                }
            ]
        }
    }
    assert ihl._registered_python(settings) == "/opt/venv/bin/python3"


def test_registered_python_ignores_non_yadgar_commands():
    settings = {
        "hooks": {
            "PostToolUse": [
                {
                    "matcher": "",
                    "hooks": [{"type": "command", "command": "/opt/venv/bin/python3 other.py"}],
                }
            ]
        }
    }
    assert ihl._registered_python(settings) is None


def test_registered_python_empty_settings():
    assert ihl._registered_python({}) is None
