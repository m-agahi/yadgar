"""The PreToolUse router-guard is retired: install and upgrade clean it up.

Ruling (Max, 2026-10-26): yadgar is a memory system, and the PreToolUse agent
guard is harness-level control, so the engine no longer ships it. A host that
ran an older install still carries the router's ``PreToolUse`` entry and the
``yadgar-pretooluse-router.py`` script. ``install_hooks_impl`` is the single
path for install and upgrade (nix runs it on every switch, ADR-0462), so it
must remove both:

* the yadgar router entry leaves ``PreToolUse``; a foreign entry stays; a key
  the strip empties is deleted;
* the stale script is deleted only AFTER the settings write, and only when no
  global ``settings.json`` still names it. A registration that points at a
  missing script fails closed and denies every Bash call (2026-07-13
  incident), so the order matters;
* a nix-store symlink is never deleted;
* the exceptions file is a user file: it is never created and never deleted.
"""

from __future__ import annotations

import json
from pathlib import Path

from yadgar.core.install._hook_scripts import _sweep_retired_router_script
from yadgar.core.install.install_hooks_lib import install_hooks_impl

_ROUTER = "yadgar-pretooluse-router.py"


def _router_entry(home: Path) -> dict:
    cmd = f"/venv/bin/python3 {home / '.claude' / 'hooks' / _ROUTER}"
    return {
        "matcher": "Bash|Edit|Write|NotebookEdit",
        "hooks": [{"type": "command", "command": cmd}],
    }


def _foreign_entry() -> dict:
    return {
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": "/usr/bin/foreign-guard pre-tool"}],
    }


def _seed_stale_install(home: Path, pre_tool_use: list[dict]) -> Path:
    claude = home / ".claude"
    hooks_dir = claude / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    script = hooks_dir / _ROUTER
    script.write_text("# stale router\n")
    (claude / "settings.json").write_text(json.dumps({"hooks": {"PreToolUse": pre_tool_use}}))
    return script


def _install(tmp_path: Path, monkeypatch, scope: str = "global") -> dict:
    monkeypatch.setenv("HOME", str(tmp_path))
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    return install_hooks_impl(home_dir=tmp_path, scope=scope, project_directory=str(proj))


def _global_hooks(home: Path) -> dict:
    return json.loads((home / ".claude" / "settings.json").read_text()).get("hooks", {})


def test_fresh_install_ships_no_router(tmp_path, monkeypatch):
    result = _install(tmp_path, monkeypatch)
    assert not (tmp_path / ".claude" / "hooks" / _ROUTER).exists()
    assert "PreToolUse" not in _global_hooks(tmp_path)
    assert not any("PreToolUse" in h for h in result["hooks_installed"])


def test_fresh_install_creates_no_exceptions_file(tmp_path, monkeypatch):
    _install(tmp_path, monkeypatch)
    assert not (tmp_path / ".claude" / "yadgar-hook-exceptions.json").exists()


def test_upgrade_strips_router_entry_and_keeps_foreign(tmp_path, monkeypatch):
    script = _seed_stale_install(tmp_path, [_router_entry(tmp_path), _foreign_entry()])
    _install(tmp_path, monkeypatch)
    pre = _global_hooks(tmp_path)["PreToolUse"]
    assert pre == [_foreign_entry()]
    assert not script.exists()


def test_upgrade_deletes_emptied_pretooluse_key(tmp_path, monkeypatch):
    script = _seed_stale_install(tmp_path, [_router_entry(tmp_path)])
    _install(tmp_path, monkeypatch)
    assert "PreToolUse" not in _global_hooks(tmp_path)
    assert not script.exists()


def test_upgrade_strips_router_hook_inside_a_shared_entry(tmp_path, monkeypatch):
    shared = _router_entry(tmp_path)
    shared["hooks"].append(_foreign_entry()["hooks"][0])
    _seed_stale_install(tmp_path, [shared])
    _install(tmp_path, monkeypatch)
    pre = _global_hooks(tmp_path)["PreToolUse"]
    assert len(pre) == 1
    assert pre[0]["hooks"] == _foreign_entry()["hooks"]


def test_project_scope_install_strips_global_router_entry(tmp_path, monkeypatch):
    script = _seed_stale_install(tmp_path, [_router_entry(tmp_path), _foreign_entry()])
    proj_settings = tmp_path / "proj" / ".claude" / "settings.json"
    proj_settings.parent.mkdir(parents=True)
    proj_settings.write_text(json.dumps({"hooks": {"PreToolUse": [_router_entry(tmp_path)]}}))
    _install(tmp_path, monkeypatch, scope="project")
    assert _global_hooks(tmp_path)["PreToolUse"] == [_foreign_entry()]
    assert "PreToolUse" not in json.loads(proj_settings.read_text())["hooks"]
    assert not script.exists()


def test_upgrade_leaves_exceptions_file_untouched(tmp_path, monkeypatch):
    _seed_stale_install(tmp_path, [_router_entry(tmp_path)])
    cfg = tmp_path / ".claude" / "yadgar-hook-exceptions.json"
    cfg.write_text('{"version": 1, "push_default_allowlist": ["nix"]}\n')
    before = cfg.read_bytes()
    _install(tmp_path, monkeypatch)
    assert cfg.read_bytes() == before


def test_dry_run_touches_nothing(tmp_path, monkeypatch):
    script = _seed_stale_install(tmp_path, [_router_entry(tmp_path)])
    settings = tmp_path / ".claude" / "settings.json"
    before = settings.read_bytes()
    monkeypatch.setenv("HOME", str(tmp_path))
    proj = tmp_path / "proj"
    proj.mkdir()
    install_hooks_impl(home_dir=tmp_path, scope="global", project_directory=str(proj), dry_run=True)
    assert script.exists()
    assert settings.read_bytes() == before


def test_sweep_keeps_script_still_named_by_global_settings(tmp_path):
    script = _seed_stale_install(tmp_path, [_router_entry(tmp_path)])
    _sweep_retired_router_script(tmp_path / ".claude", dry_run=False)
    assert script.exists()


def test_sweep_never_deletes_nix_symlink(tmp_path):
    claude = tmp_path / ".claude"
    hooks_dir = claude / "hooks"
    hooks_dir.mkdir(parents=True)
    (claude / "settings.json").write_text("{}")
    link = hooks_dir / _ROUTER
    link.symlink_to("/nix/store/abc-yadgar/yadgar-pretooluse-router.py")
    _sweep_retired_router_script(claude, dry_run=False)
    assert link.is_symlink()


def test_sweep_deletes_unreferenced_script(tmp_path):
    script = _seed_stale_install(tmp_path, [_foreign_entry()])
    _sweep_retired_router_script(tmp_path / ".claude", dry_run=False)
    assert not script.exists()
