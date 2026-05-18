"""v5.1.2 H1 — install_hooks host-vs-container tests.

Tests:
1. CLI subcommand writes to host settings.json (monkeypatched HOME).
2. MCP tool refuses when running inside container (YADGAR_IN_CONTAINER=1).
3. MCP tool writes to host settings.json when NOT in container.
4. CLI --dry-run prints diff/preview without writing any file.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

# ── helper: locate install_hooks MCP tool function ──────────────────────────


def _get_mcp_install_hooks():
    """Return the install_hooks function exposed via yadgar.server."""
    from yadgar import server as _s

    return _s.install_hooks


# ─────────────────────────────────────────────────────────────────────────────
# 1. CLI subcommand writes to host settings.json
# ─────────────────────────────────────────────────────────────────────────────


def test_cli_subcommand_writes_to_host_settings(tmp_path, monkeypatch):
    """python -m yadgar install-hooks --scope=global writes to $HOME/.claude/settings.json."""
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()

    env = {**os.environ, "HOME": str(tmp_path)}
    # Ensure NOT in container env for this test
    env.pop("YADGAR_IN_CONTAINER", None)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "yadgar",
            "install-hooks",
            "--scope",
            "global",
            "--project-directory",
            str(proj_dir),
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, (
        f"install-hooks CLI exited non-zero.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )

    settings_file = tmp_path / ".claude" / "settings.json"
    assert settings_file.exists(), (
        f"Expected {settings_file} to exist after CLI install-hooks --scope=global"
    )

    settings = json.loads(settings_file.read_text())
    hooks = settings.get("hooks", {})
    assert hooks, "No hooks written to settings.json"
    # At minimum SessionStart, PostToolUse, PreCompact must be present
    for expected_event in ("SessionStart", "PostToolUse", "PreCompact"):
        assert expected_event in hooks, f"Hook event {expected_event!r} missing from settings.json"


# ─────────────────────────────────────────────────────────────────────────────
# 2. MCP tool refuses when running inside container
# ─────────────────────────────────────────────────────────────────────────────


def test_mcp_tool_refuses_in_container(tmp_path, monkeypatch):
    """install_hooks MCP tool returns refused status when YADGAR_IN_CONTAINER=1."""
    monkeypatch.setenv("YADGAR_IN_CONTAINER", "1")
    monkeypatch.setenv("HOME", str(tmp_path))

    install_hooks = _get_mcp_install_hooks()
    result = install_hooks(project_directory=str(tmp_path / "proj"), scope="global")

    assert result.get("status") == "refused", (
        f"Expected status='refused', got {result.get('status')!r}. Full result: {result}"
    )
    assert result.get("reason") == "running_in_container", (
        f"Expected reason='running_in_container', got {result.get('reason')!r}"
    )
    assert "host_command" in result, "Expected 'host_command' field in refused response"

    # Must NOT have written anything to the container's settings
    assert not (tmp_path / ".claude" / "settings.json").exists(), (
        "settings.json must NOT be written when container-mode is detected"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 3. MCP tool works on host (non-container)
# ─────────────────────────────────────────────────────────────────────────────


def test_mcp_tool_works_on_host(tmp_path, monkeypatch):
    """install_hooks MCP tool writes hooks to monkeypatched HOME when NOT in container."""
    # Ensure neither container marker is set
    monkeypatch.delenv("YADGAR_IN_CONTAINER", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    # Patch container detection so /.dockerenv presence on CI doesn't trip this test
    import yadgar.install_hooks_lib as lib

    monkeypatch.setattr(lib, "is_running_in_container", lambda: False)

    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()

    install_hooks = _get_mcp_install_hooks()
    result = install_hooks(project_directory=str(proj_dir), scope="global")

    assert result.get("status") == "installed", (
        f"Expected status='installed', got {result.get('status')!r}. Full result: {result}"
    )

    settings_file = tmp_path / ".claude" / "settings.json"
    assert settings_file.exists(), "settings.json must be written when running on host"
    settings = json.loads(settings_file.read_text())
    assert "hooks" in settings, "No hooks key in written settings.json"


# ─────────────────────────────────────────────────────────────────────────────
# 4. CLI --dry-run prints preview without writing
# ─────────────────────────────────────────────────────────────────────────────


def test_cli_dry_run_prints_diff_no_write(tmp_path):
    """install-hooks --dry-run prints a settings.json preview but writes nothing."""
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()

    env = {**os.environ, "HOME": str(tmp_path)}
    env.pop("YADGAR_IN_CONTAINER", None)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "yadgar",
            "install-hooks",
            "--scope",
            "global",
            "--project-directory",
            str(proj_dir),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, (
        f"--dry-run exited non-zero.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )

    # Must print something (diff or preview)
    output = result.stdout + result.stderr
    assert output.strip(), "--dry-run produced no output"

    # Must NOT write settings.json
    settings_file = tmp_path / ".claude" / "settings.json"
    assert not settings_file.exists(), f"--dry-run must not write {settings_file}; file was created"
