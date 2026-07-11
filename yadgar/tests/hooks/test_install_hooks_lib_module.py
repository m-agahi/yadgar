"""Tests for yadgar/install_hooks_lib.py — hook installation helpers.

Coverage targets:
- is_running_in_container
- _resolve_python_shebang
- _copy_hook (dry_run + real copy)
- _make_hook_entry (with and without env_block)
- _append_if_absent (deduplication)
- _resolve_scope_paths (global + project scope)
- _load_settings (missing file + malformed JSON)
- _build_core_hooks (output structure)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from yadgar.core.install.install_hooks_lib import (
    _append_if_absent,
    _build_core_hooks,
    _load_settings,
    _make_hook_entry,
    _resolve_python_shebang,
    _resolve_scope_paths,
    is_running_in_container,
)

# ── is_running_in_container ──────────────────────────────────────────────────


def test_not_in_container_by_default(monkeypatch):
    monkeypatch.delenv("YADGAR_IN_CONTAINER", raising=False)
    assert is_running_in_container() is False


def test_in_container_when_env_set(monkeypatch):
    monkeypatch.setenv("YADGAR_IN_CONTAINER", "1")
    assert is_running_in_container() is True


def test_not_in_container_other_values(monkeypatch):
    for val in ("0", "true", "yes", ""):
        monkeypatch.setenv("YADGAR_IN_CONTAINER", val)
        assert is_running_in_container() is False


# ── _resolve_python_shebang ──────────────────────────────────────────────────


def test_resolve_python_shebang_contains_sys_executable():
    shebang = _resolve_python_shebang()
    assert shebang.startswith("#!")
    assert sys.executable in shebang
    assert shebang.endswith("\n")


# ── _make_hook_entry ─────────────────────────────────────────────────────────


def test_make_hook_entry_basic():
    entry = _make_hook_entry("echo hello", "", {})
    assert entry["matcher"] == ""
    assert entry["hooks"][0]["type"] == "command"
    assert entry["hooks"][0]["command"] == "echo hello"
    assert "env" not in entry["hooks"][0]


def test_make_hook_entry_with_matcher():
    entry = _make_hook_entry("myscript.py", "mcp__yadgar__.*", {})
    assert entry["matcher"] == "mcp__yadgar__.*"


def test_make_hook_entry_with_env_block():
    env = {"YADGAR_PORT": "8765", "DEBUG": "1"}
    entry = _make_hook_entry("run.py", "", env)
    assert entry["hooks"][0]["env"] == env


def test_make_hook_entry_empty_env_block_omitted():
    entry = _make_hook_entry("run.py", "", {})
    assert "env" not in entry["hooks"][0]


# ── _append_if_absent ────────────────────────────────────────────────────────


def test_append_if_absent_adds_new_entry():
    cfg: dict = {}
    _append_if_absent(cfg, "PostToolUse", "myscript.py", {})
    assert len(cfg["PostToolUse"]) == 1
    assert cfg["PostToolUse"][0]["hooks"][0]["command"] == "myscript.py"


def test_append_if_absent_deduplicates():
    cfg: dict = {}
    _append_if_absent(cfg, "PostToolUse", "myscript.py", {})
    _append_if_absent(cfg, "PostToolUse", "myscript.py", {})
    assert len(cfg["PostToolUse"]) == 1


def test_append_if_absent_allows_different_commands():
    cfg: dict = {}
    _append_if_absent(cfg, "PostToolUse", "script_a.py", {})
    _append_if_absent(cfg, "PostToolUse", "script_b.py", {})
    assert len(cfg["PostToolUse"]) == 2


def test_append_if_absent_preserves_existing_entries():
    existing = [{"matcher": "", "hooks": [{"type": "command", "command": "existing.py"}]}]
    cfg = {"PostToolUse": existing}
    _append_if_absent(cfg, "PostToolUse", "new.py", {})
    assert len(cfg["PostToolUse"]) == 2


def test_append_if_absent_with_env_block():
    cfg: dict = {}
    env = {"TOKEN": "abc"}
    _append_if_absent(cfg, "SessionStart", "start.py", env)
    assert cfg["SessionStart"][0]["hooks"][0]["env"] == env


# ── _resolve_scope_paths ─────────────────────────────────────────────────────


def test_resolve_scope_paths_global():
    home = Path("/home/user")
    project_dir = Path("/home/user/myproject")
    global_claude_dir, global_hooks_dir, hooks_dir, settings_target_dir = _resolve_scope_paths(
        home, "global", project_dir
    )
    assert global_claude_dir == Path("/home/user/.claude")
    assert global_hooks_dir == Path("/home/user/.claude/hooks")
    assert hooks_dir == Path("/home/user/.claude/hooks")  # same as global for global scope
    assert settings_target_dir == Path("/home/user/.claude")


def test_resolve_scope_paths_project():
    home = Path("/home/user")
    project_dir = Path("/home/user/myproject")
    global_claude_dir, global_hooks_dir, hooks_dir, settings_target_dir = _resolve_scope_paths(
        home, "project", project_dir
    )
    assert global_claude_dir == Path("/home/user/.claude")
    assert hooks_dir == Path("/home/user/myproject/.claude/hooks")
    assert settings_target_dir == Path("/home/user/myproject/.claude")


# ── _load_settings ───────────────────────────────────────────────────────────


def test_load_settings_missing_file(tmp_path):
    p = tmp_path / "nonexistent.json"
    result = _load_settings(p)
    assert result == {}


def test_load_settings_valid_file(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"hooks": {"PostToolUse": []}}))
    result = _load_settings(p)
    assert "hooks" in result


def test_load_settings_malformed_json(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text("{not valid json...")
    result = _load_settings(p)
    assert result == {}


def test_load_settings_empty_file(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text("")
    result = _load_settings(p)
    assert result == {}


# ── _build_core_hooks ────────────────────────────────────────────────────────


def test_build_core_hooks_sets_required_events(tmp_path):
    runner = str(tmp_path / "hook_runner.py")
    db_lockdown_dst = tmp_path / "db-lockdown.py"
    cfg: dict = {}
    _build_core_hooks(cfg, runner, {}, db_lockdown_dst)
    assert "PreCompact" in cfg
    assert "SessionStart" in cfg
    assert "PostToolUse" in cfg
    assert "UserPromptSubmit" in cfg
    assert "PreToolUse" in cfg


def test_build_core_hooks_session_start_has_two_entries(tmp_path):
    runner = str(tmp_path / "hook_runner.py")
    db_lockdown_dst = tmp_path / "db-lockdown.py"
    cfg: dict = {}
    _build_core_hooks(cfg, runner, {}, db_lockdown_dst)
    assert len(cfg["SessionStart"]) == 2


def test_build_core_hooks_post_tool_use_has_two_entries(tmp_path):
    runner = str(tmp_path / "hook_runner.py")
    db_lockdown_dst = tmp_path / "db-lockdown.py"
    cfg: dict = {}
    _build_core_hooks(cfg, runner, {}, db_lockdown_dst)
    assert len(cfg["PostToolUse"]) == 2


def test_build_core_hooks_pre_tool_use_matcher_is_bash(tmp_path):
    runner = str(tmp_path / "hook_runner.py")
    db_lockdown_dst = tmp_path / "db-lockdown.py"
    cfg: dict = {}
    _build_core_hooks(cfg, runner, {}, db_lockdown_dst)
    assert cfg["PreToolUse"][0]["matcher"] == "Bash"


def test_build_core_hooks_runner_in_commands(tmp_path):
    runner = str(tmp_path / "hook_runner.py")
    db_lockdown_dst = tmp_path / "db-lockdown.py"
    cfg: dict = {}
    _build_core_hooks(cfg, runner, {}, db_lockdown_dst)
    # At least one command should reference the runner path
    all_commands = [
        entry["hooks"][0]["command"]
        for entries in cfg.values()
        for entry in entries
        if isinstance(entry, dict) and entry.get("hooks")
    ]
    assert any("hook_runner.py" in cmd for cmd in all_commands)


# ── _copy_hook (via tmp_path) ─────────────────────────────────────────────────


def test_copy_hook_dry_run_does_nothing(tmp_path):
    from yadgar.core.install.install_hooks_lib import _copy_hook

    src = tmp_path / "src.py"
    src.write_text("#!/usr/bin/env python3\nprint('hello')\n")
    dst = tmp_path / "dst.py"
    _copy_hook(src, dst, dry_run=True)
    assert not dst.exists()


def test_copy_hook_copies_file(tmp_path):
    from yadgar.core.install.install_hooks_lib import _copy_hook

    src = tmp_path / "src.py"
    src.write_text("#!/usr/bin/env python3\nprint('hello')\n")
    dst = tmp_path / "dst.py"
    _copy_hook(src, dst, dry_run=False)
    assert dst.exists()
    assert dst.stat().st_mode & 0o111  # executable


def test_copy_hook_rewrites_shebang(tmp_path):
    from yadgar.core.install.install_hooks_lib import _copy_hook

    src = tmp_path / "src.py"
    src.write_text("#!/usr/bin/env python3\nprint('hello')\n")
    dst = tmp_path / "dst.py"
    _copy_hook(src, dst, dry_run=False)
    first_line = dst.read_text().splitlines()[0]
    assert first_line == f"#!{sys.executable}"


def test_copy_hook_nonpython_shebang_preserved(tmp_path):
    from yadgar.core.install.install_hooks_lib import _copy_hook

    src = tmp_path / "hook.sh"
    src.write_text("#!/bin/bash\necho hello\n")
    dst = tmp_path / "dst.sh"
    _copy_hook(src, dst, dry_run=False)
    first_line = dst.read_text().splitlines()[0]
    assert first_line == "#!/bin/bash"


def test_copy_hook_missing_src_no_error(tmp_path):
    from yadgar.core.install.install_hooks_lib import _copy_hook

    src = tmp_path / "nonexistent.py"
    dst = tmp_path / "dst.py"
    _copy_hook(src, dst, dry_run=False)
    assert not dst.exists()


# ── _atomic_write ─────────────────────────────────────────────────────────────


def test_atomic_write_creates_file(tmp_path):
    from yadgar.core.install.install_hooks_lib import _atomic_write

    target = tmp_path / "settings.json"
    _atomic_write(tmp_path, target, {"key": "value"})
    assert target.exists()
    loaded = json.loads(target.read_text())
    assert loaded["key"] == "value"


def test_atomic_write_creates_parent_dir(tmp_path):
    from yadgar.core.install.install_hooks_lib import _atomic_write

    subdir = tmp_path / "new" / "nested"
    target = subdir / "settings.json"
    _atomic_write(subdir, target, {"x": 1})
    assert target.exists()


# ── _write_global_stop_hooks ──────────────────────────────────────────────────


def test_write_global_stop_hooks_creates_settings(tmp_path):
    from yadgar.core.install.install_hooks_lib import _write_global_stop_hooks

    global_claude_dir = tmp_path / ".claude"
    global_claude_dir.mkdir()

    stop_entry = [{"matcher": "", "hooks": [{"type": "command", "command": "stop.py"}]}]
    session_end_entry = [{"matcher": "", "hooks": [{"type": "command", "command": "end.py"}]}]
    _write_global_stop_hooks(global_claude_dir, stop_entry, session_end_entry)

    settings_path = global_claude_dir / "settings.json"
    assert settings_path.exists()
    data = json.loads(settings_path.read_text())
    assert "Stop" in data["hooks"]
    assert "SessionEnd" in data["hooks"]


def test_write_global_stop_hooks_merges_existing_settings(tmp_path):
    from yadgar.core.install.install_hooks_lib import _write_global_stop_hooks

    global_claude_dir = tmp_path / ".claude"
    global_claude_dir.mkdir()

    # Pre-existing settings with some hooks
    existing = {"theme": "dark", "hooks": {"PreCompact": [{"matcher": "", "hooks": []}]}}
    (global_claude_dir / "settings.json").write_text(json.dumps(existing))

    stop_entry: list = []
    session_end_entry: list = []
    _write_global_stop_hooks(global_claude_dir, stop_entry, session_end_entry)

    data = json.loads((global_claude_dir / "settings.json").read_text())
    # Existing hooks preserved, Stop + SessionEnd added
    assert "PreCompact" in data["hooks"]
    assert "Stop" in data["hooks"]
    # Non-hook keys preserved
    assert data.get("theme") == "dark"


def test_write_global_stop_hooks_missing_settings_file(tmp_path):
    from yadgar.core.install.install_hooks_lib import _write_global_stop_hooks

    global_claude_dir = tmp_path / ".claude"
    global_claude_dir.mkdir()
    # No settings.json pre-existing

    _write_global_stop_hooks(global_claude_dir, [], [])
    assert (global_claude_dir / "settings.json").exists()


# ── _copy_scope_scripts dry_run ───────────────────────────────────────────────


def test_copy_scope_scripts_dry_run_no_files(tmp_path):
    from yadgar.core.install.install_hooks_lib import _copy_scope_scripts

    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    package_hooks = tmp_path / "package_hooks"
    # dry_run=True → no files written
    _copy_scope_scripts(package_hooks, hooks_dir, dry_run=True)
    assert len(list(hooks_dir.iterdir())) == 0
