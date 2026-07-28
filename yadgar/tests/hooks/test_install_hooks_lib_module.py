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
    _entry_command,
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


def test_resolve_python_shebang_contains_sys_executable(monkeypatch):
    # _resolve_python_shebang -> _stable_python() only trusts sys.executable
    # when it's a DURABLE path (not under .claude/worktrees/, /tmp, or any
    # linked git worktree — see _interpreter.py::_is_durable_interpreter).
    # Running this suite itself from inside an agent worktree makes the
    # real sys.executable non-durable, which would make this test assert
    # against whatever substitute path _stable_python() picks instead of
    # testing the pass-through behavior it's meant to cover. Pin a fake
    # durable path so the test is independent of where it happens to run.
    fake_durable = "/opt/durable-test-python/bin/python3"
    monkeypatch.setattr(sys, "executable", fake_durable)
    shebang = _resolve_python_shebang()
    assert shebang.startswith("#!")
    assert fake_durable in shebang
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


def test_make_hook_entry_async_true_sets_key():
    """async_=True must set entry["hooks"][0]["async"] = True."""
    entry = _make_hook_entry("drain.py", "", {}, async_=True)
    assert entry["hooks"][0].get("async") is True


def test_make_hook_entry_async_default_key_absent():
    """Default call (async_=False) must NOT add an "async" key at all."""
    entry = _make_hook_entry("drain.py", "", {})
    assert "async" not in entry["hooks"][0]


def test_make_hook_entry_async_false_explicit_key_absent():
    """Explicit async_=False must also NOT add an "async" key."""
    entry = _make_hook_entry("drain.py", "", {}, async_=False)
    assert "async" not in entry["hooks"][0]


# ── _append_if_absent ────────────────────────────────────────────────────────


def test_append_if_absent_adds_new_entry():
    cfg: dict = {}
    _append_if_absent(cfg, "PostToolUse", "myscript.py", {}, managed_basename="myscript.py")
    assert len(cfg["PostToolUse"]) == 1
    assert cfg["PostToolUse"][0]["hooks"][0]["command"] == "myscript.py"


def test_append_if_absent_deduplicates():
    cfg: dict = {}
    _append_if_absent(cfg, "PostToolUse", "myscript.py", {}, managed_basename="myscript.py")
    _append_if_absent(cfg, "PostToolUse", "myscript.py", {}, managed_basename="myscript.py")
    assert len(cfg["PostToolUse"]) == 1


def test_append_if_absent_deduplicates_on_basename_across_interpreter_drift():
    """BUG A: dedup must key on the managed basename, not the full command.

    A prior install may have registered a *different* interpreter prefix
    (bare ``python3`` vs an absolute venv path). Re-registering the same
    managed script must collapse to one entry, not accumulate a dupe.
    """
    cfg: dict = {}
    _append_if_absent(
        cfg,
        "SubagentStop",
        "python3 /home/u/.claude/hooks/yadgar-subagent-stop.py",
        {},
        managed_basename="yadgar-subagent-stop.py",
    )
    _append_if_absent(
        cfg,
        "SubagentStop",
        "/opt/venv/bin/python3 /home/u/.claude/hooks/yadgar-subagent-stop.py",
        {},
        managed_basename="yadgar-subagent-stop.py",
    )
    assert len(cfg["SubagentStop"]) == 1


def test_append_if_absent_allows_different_commands():
    cfg: dict = {}
    _append_if_absent(cfg, "PostToolUse", "script_a.py", {}, managed_basename="script_a.py")
    _append_if_absent(cfg, "PostToolUse", "script_b.py", {}, managed_basename="script_b.py")
    assert len(cfg["PostToolUse"]) == 2


def test_append_if_absent_preserves_existing_entries():
    existing = [{"matcher": "", "hooks": [{"type": "command", "command": "existing.py"}]}]
    cfg = {"PostToolUse": existing}
    _append_if_absent(cfg, "PostToolUse", "new.py", {}, managed_basename="new.py")
    assert len(cfg["PostToolUse"]) == 2


def test_append_if_absent_with_env_block():
    cfg: dict = {}
    env = {"TOKEN": "abc"}
    _append_if_absent(cfg, "SessionStart", "start.py", env, managed_basename="start.py")
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


def test_build_core_hooks_precompact_hook_is_async(tmp_path):
    """PreCompact inner hook must have async=True (fire-and-forget drain)."""
    runner = str(tmp_path / "hook_runner.py")
    db_lockdown_dst = tmp_path / "db-lockdown.py"
    cfg: dict = {}
    _build_core_hooks(cfg, runner, {}, db_lockdown_dst)
    assert cfg["PreCompact"][0]["hooks"][0].get("async") is True


def test_build_core_hooks_blocking_hooks_have_no_async_key(tmp_path):
    """SessionStart, PostToolUse, UserPromptSubmit, PreToolUse must NOT have async key.

    These events inject stdout (prompt-recall, session-start-context) or gate
    execution (PreToolUse deny). Making them async would break injection/deny.
    """
    runner = str(tmp_path / "hook_runner.py")
    db_lockdown_dst = tmp_path / "db-lockdown.py"
    cfg: dict = {}
    _build_core_hooks(cfg, runner, {}, db_lockdown_dst)
    blocking_events = ("SessionStart", "PostToolUse", "UserPromptSubmit", "PreToolUse")
    for event in blocking_events:
        for entry in cfg.get(event, []):
            inner = entry.get("hooks", [{}])[0]
            assert "async" not in inner, (
                f"{event} hook must be blocking (no async key); found async={inner['async']!r}"
            )


# ── _build_core_hooks foreign-preservation (ADR-0161) ─────────────────────────
#
# ADR-0161 converts the 5 core events from replace-always to foreign-preserving:
# a hook entry written by a DIFFERENT tool under one of these event keys (e.g.
# nix's "caveman" SessionStart plugin hook, keyed on plugins/cache/caveman) must
# SURVIVE a yadgar install. These tests seed a foreign entry per event and assert
# it is still present alongside the fresh yadgar entries.

_FOREIGN_CMD = "/home/u/.claude/plugins/cache/caveman/session-start.sh"


def _foreign_entry(matcher: str = "") -> dict:
    return {"matcher": matcher, "hooks": [{"type": "command", "command": _FOREIGN_CMD}]}


def _has_foreign(entries: list) -> bool:
    return any("caveman" in _entry_command(e) for e in entries)


def _yadgar_marker_present(entries: list, marker: str) -> bool:
    return any(marker in _entry_command(e) for e in entries)


def test_build_core_hooks_preserves_foreign_precompact(tmp_path):
    runner = str(tmp_path / "hook_runner.py")
    db_lockdown_dst = tmp_path / "db-lockdown.py"
    cfg: dict = {"PreCompact": [_foreign_entry()]}
    _build_core_hooks(cfg, runner, {}, db_lockdown_dst)
    assert _has_foreign(cfg["PreCompact"]), "foreign PreCompact hook was clobbered"
    assert _yadgar_marker_present(cfg["PreCompact"], "pre-compact-drain")


def test_build_core_hooks_preserves_foreign_session_start(tmp_path):
    runner = str(tmp_path / "hook_runner.py")
    db_lockdown_dst = tmp_path / "db-lockdown.py"
    cfg: dict = {"SessionStart": [_foreign_entry()]}
    _build_core_hooks(cfg, runner, {}, db_lockdown_dst)
    assert _has_foreign(cfg["SessionStart"]), "foreign SessionStart (caveman) hook was clobbered"
    assert _yadgar_marker_present(cfg["SessionStart"], "session-start-context")
    assert _yadgar_marker_present(cfg["SessionStart"], "post-compact-rehydrate")


def test_build_core_hooks_preserves_foreign_post_tool_use(tmp_path):
    runner = str(tmp_path / "hook_runner.py")
    db_lockdown_dst = tmp_path / "db-lockdown.py"
    cfg: dict = {"PostToolUse": [_foreign_entry()]}
    _build_core_hooks(cfg, runner, {}, db_lockdown_dst)
    assert _has_foreign(cfg["PostToolUse"]), "foreign PostToolUse hook was clobbered"
    assert _yadgar_marker_present(cfg["PostToolUse"], "post-tool-capture")
    assert _yadgar_marker_present(cfg["PostToolUse"], "block-reflect")


def test_build_core_hooks_preserves_foreign_user_prompt_submit(tmp_path):
    runner = str(tmp_path / "hook_runner.py")
    db_lockdown_dst = tmp_path / "db-lockdown.py"
    cfg: dict = {"UserPromptSubmit": [_foreign_entry()]}
    _build_core_hooks(cfg, runner, {}, db_lockdown_dst)
    assert _has_foreign(cfg["UserPromptSubmit"]), "foreign UserPromptSubmit hook was clobbered"
    assert _yadgar_marker_present(cfg["UserPromptSubmit"], "prompt-recall")


def test_build_core_hooks_preserves_foreign_pre_tool_use(tmp_path):
    runner = str(tmp_path / "hook_runner.py")
    db_lockdown_dst = tmp_path / "db-lockdown.py"
    cfg: dict = {"PreToolUse": [_foreign_entry(matcher="Bash")]}
    _build_core_hooks(cfg, runner, {}, db_lockdown_dst)
    assert _has_foreign(cfg["PreToolUse"]), "foreign PreToolUse hook was clobbered"
    # router marker = router_dst basename (db-lockdown.py in tests).
    assert _yadgar_marker_present(cfg["PreToolUse"], "db-lockdown.py")


def test_build_core_hooks_foreign_preserved_is_idempotent(tmp_path):
    """Second install must not duplicate yadgar entries and must keep the foreign one.

    Regression guard (green on both old and new code for the yadgar-entry counts,
    but proves the strip-then-append marker round-trips with a foreign entry present).
    """
    runner = str(tmp_path / "hook_runner.py")
    db_lockdown_dst = tmp_path / "db-lockdown.py"
    cfg: dict = {
        "PreCompact": [_foreign_entry()],
        "SessionStart": [_foreign_entry()],
        "PostToolUse": [_foreign_entry()],
        "UserPromptSubmit": [_foreign_entry()],
        "PreToolUse": [_foreign_entry(matcher="Bash")],
    }
    _build_core_hooks(cfg, runner, {}, db_lockdown_dst)
    _build_core_hooks(cfg, runner, {}, db_lockdown_dst)
    # 1 foreign + N yadgar per event; no yadgar duplication on the second run.
    assert len(cfg["PreCompact"]) == 2
    assert len(cfg["SessionStart"]) == 3
    assert len(cfg["PostToolUse"]) == 3
    assert len(cfg["UserPromptSubmit"]) == 2
    assert len(cfg["PreToolUse"]) == 2
    for event in ("PreCompact", "SessionStart", "PostToolUse", "UserPromptSubmit", "PreToolUse"):
        assert _has_foreign(cfg[event]), f"foreign {event} hook lost after two installs"


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


def test_copy_hook_rewrites_shebang(tmp_path, monkeypatch):
    from yadgar.core.install.install_hooks_lib import _copy_hook

    # Same durability concern as test_resolve_python_shebang_contains_sys_executable
    # above — pin a fake durable sys.executable so this test doesn't depend on
    # whether it's run from inside an agent worktree.
    fake_durable = "/opt/durable-test-python/bin/python3"
    monkeypatch.setattr(sys, "executable", fake_durable)

    src = tmp_path / "src.py"
    src.write_text("#!/usr/bin/env python3\nprint('hello')\n")
    dst = tmp_path / "dst.py"
    _copy_hook(src, dst, dry_run=False)
    first_line = dst.read_text().splitlines()[0]
    assert first_line == f"#!{fake_durable}"


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


# ── _sweep_stale_hook_scripts dry_run ─────────────────────────────────────────


def test_sweep_stale_hook_scripts_dry_run_noop(tmp_path):
    from yadgar.core.install.install_hooks_lib import _sweep_stale_hook_scripts

    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    package_hooks = tmp_path / "package_hooks"
    # Seed a db-lockdown orphan; dry_run=True must NOT unlink it.
    orphan = hooks_dir / "yadgar-db-lockdown-check.py"
    orphan.write_text("# stale\n")
    _sweep_stale_hook_scripts(package_hooks, hooks_dir, dry_run=True)
    assert orphan.exists()


# ── import-surface characterization (Car C5 module split) ─────────────────────

# Every symbol external code + tests import from the canonical module path.
# Pins the public API across the C5 split into cohesive sibling modules
# (_interpreter / _hook_scripts / _settings). If a moved symbol stops being
# re-exported from install_hooks_lib, this fails.
_CANONICAL_SURFACE = (
    # public
    "install_hooks_impl",
    "is_running_in_container",
    # interpreter resolution
    "_is_git_worktree_path",
    "_is_durable_interpreter",
    "_existing_registration_ok",
    "_pipx_python",
    "_main_repo_root",
    "_canonical_repo_python",
    "_stable_python",
    "_registered_python",
    "_entry_interpreter",
    "_resolve_python_shebang",
    # hook-script copy/sweep
    "_copy_hook",
    "_is_nix_symlink",
    "_sha256_file",
    "_sweep_stale_hook_scripts",
    # settings.json manipulation
    "_make_hook_entry",
    "_entry_command",
    "_append_if_absent",
    "_build_core_hooks",
    "_install_append_hooks",
    "_install_global_scripts",
    "_write_global_stop_hooks",
    "_resolve_scope_paths",
    "_load_settings",
    "_resolve_env_block",
    "_atomic_write",
    # module-level constants patched/read by tests
    "_WORKTREE_MARKER",
    "_MANAGED_NONPREFIXED",
    "_TEST_FIXTURE_TOKENS",
    # the module object still carries `sys` (tests patch ihl.sys.executable)
    "sys",
    "logger",
)


def test_canonical_import_surface_preserved():
    import yadgar.core.install.install_hooks_lib as ihl

    missing = [name for name in _CANONICAL_SURFACE if not hasattr(ihl, name)]
    assert not missing, f"canonical module lost re-exports: {missing}"
