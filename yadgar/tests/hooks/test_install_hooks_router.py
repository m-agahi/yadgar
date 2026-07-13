"""HOOKS train Car 1 — installer coupling tests for the PreToolUse router.

Asserts install_hooks_impl repoints the PreToolUse entry at the router script,
seeds the exceptions config create-if-absent (never clobbers a user entry),
best-effort-unlinks the orphaned db-lockdown script, and updates the report
string. TDD: written before the installer edits (red → green).
"""

from __future__ import annotations

import json
from pathlib import Path

from yadgar.core.install.install_hooks_lib import install_hooks_impl


def _install(tmp_path: Path, monkeypatch, scope: str = "global") -> dict:
    monkeypatch.setenv("HOME", str(tmp_path))
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    return install_hooks_impl(home_dir=tmp_path, scope=scope, project_directory=str(proj))


def test_router_script_installed(tmp_path, monkeypatch):
    _install(tmp_path, monkeypatch)
    router = tmp_path / ".claude" / "hooks" / "yadgar-pretooluse-router.py"
    assert router.exists()


def test_pretooluse_entry_points_at_router(tmp_path, monkeypatch):
    _install(tmp_path, monkeypatch)
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    pre = settings["hooks"]["PreToolUse"]
    assert len(pre) == 1
    assert pre[0]["matcher"] == "Bash"
    assert "yadgar-pretooluse-router.py" in pre[0]["hooks"][0]["command"]
    assert "db-lockdown" not in pre[0]["hooks"][0]["command"]


def test_exceptions_config_seeded(tmp_path, monkeypatch):
    _install(tmp_path, monkeypatch)
    cfg_path = tmp_path / ".claude" / "yadgar-hook-exceptions.json"
    assert cfg_path.exists()
    cfg = json.loads(cfg_path.read_text())
    assert cfg["push_default_allowlist"] == ["nix", "ledger", "ostad"]
    assert cfg["disabled_guards"] == []


def test_exceptions_config_not_clobbered_on_reinstall(tmp_path, monkeypatch):
    _install(tmp_path, monkeypatch)
    cfg_path = tmp_path / ".claude" / "yadgar-hook-exceptions.json"
    # User adds a repo, then reinstalls.
    user_cfg = {
        "version": 1,
        "push_default_allowlist": ["nix", "ledger", "ostad", "myrepo"],
        "disabled_guards": [],
    }
    cfg_path.write_text(json.dumps(user_cfg))
    _install(tmp_path, monkeypatch)
    survived = json.loads(cfg_path.read_text())
    assert "myrepo" in survived["push_default_allowlist"]


def test_report_string_updated(tmp_path, monkeypatch):
    result = _install(tmp_path, monkeypatch)
    installed = result["hooks_installed"]
    assert "PreToolUse (router-guard)" in installed
    assert "PreToolUse (DB lockdown)" not in installed


def test_orphan_db_lockdown_unlinked(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    # Simulate a stale prior install.
    hooks_dir = tmp_path / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    orphan = hooks_dir / "yadgar-db-lockdown-check.py"
    orphan.write_text("# stale\n")
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    install_hooks_impl(home_dir=tmp_path, scope="global", project_directory=str(proj))
    assert not orphan.exists()
