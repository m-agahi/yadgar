"""v5.1.2 H1 + Car 7 (2026-07-26) — install_hooks host-vs-container tests.

The legacy `yadgar install-hooks` CLI was hard-removed in Car 7; the
single canonical path is now `yadgar install --client claude-code --hooks`.
Two of the original tests in this file (`test_cli_subcommand_writes_to_host_settings`,
`test_cli_dry_run_prints_diff_no_write`) tested the legacy CLI and have
been DELETED — the CLI no longer exists. The remaining tests cover the
MCP `install_hooks` tool (which still exists; Car 7 made it delegate
to `install_client("claude-code", hooks=True, ...)`):

1. MCP tool refuses when running inside container (YADGAR_IN_CONTAINER=1).
2. The container-refusal detail must NOT cite the dead /hooks/install-bootstrap
   endpoint, must NOT have a dead host_command_fallback key, and MUST
   expose a runnable host_command pointing at the new canonical command.
3. MCP tool writes hooks to monkeypatched HOME when NOT in container.

The host_command string in the container-refusal now points at
`yadgar install --client claude-code --hooks --scope=global` (was
`yadgar install-hooks --scope=global`).
"""

from __future__ import annotations

import json

# ── helper: locate install_hooks MCP tool function ──────────────────────────


def _get_mcp_install_hooks():
    """Return the install_hooks function exposed via yadgar.server."""
    from yadgar.core import server as _s

    return _s.install_hooks


# ─────────────────────────────────────────────────────────────────────────────
# 1. MCP tool refuses when running inside container
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


def test_container_refusal_uses_new_canonical_host_command(tmp_path, monkeypatch):
    """BUG D + Car 7: refusal must cite the new canonical command, not the legacy one."""
    monkeypatch.setenv("YADGAR_IN_CONTAINER", "1")
    monkeypatch.setenv("HOME", str(tmp_path))

    install_hooks = _get_mcp_install_hooks()
    result = install_hooks(project_directory=str(tmp_path / "proj"), scope="global")

    # The host_command MUST point at the new canonical command.
    assert "install --client claude-code --hooks" in result.get("host_command", ""), (
        f"host_command must use the new canonical path; got {result.get('host_command')!r}"
    )
    # The legacy command MUST NOT appear (it's hard-removed).
    assert "install-hooks" not in result.get("host_command", ""), (
        f"host_command must NOT reference the legacy install-hooks (hard-removed in Car 7); "
        f"got {result.get('host_command')!r}"
    )
    # BUG D guards still hold.
    assert "install-bootstrap" not in result.get("detail", ""), (
        "container-refusal detail still cites the dead /hooks/install-bootstrap endpoint"
    )
    assert "host_command_fallback" not in result, "dead host_command_fallback key must be dropped"
    assert "install-bootstrap" not in json.dumps(result), (
        "no install-bootstrap substring may remain anywhere in the refusal payload"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 3. MCP tool works on host (non-container)
# ─────────────────────────────────────────────────────────────────────────────


def test_mcp_tool_works_on_host(tmp_path, monkeypatch):
    """install_hooks MCP tool (Car 7: delegates to install_client) writes hooks
    to monkeypatched HOME when NOT in container.

    Coverage is the same shape as before — the MCP tool still writes
    ~/.claude/settings.json with the five hook types — but the code
    path now goes through `install_client(..., hooks=True, ...)` →
    `register_hooks` → `_emit_claude_json` rather than the legacy
    `install_hooks_impl`. This test pins the new path.
    """
    # Ensure neither container marker is set
    monkeypatch.delenv("YADGAR_IN_CONTAINER", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    # Patch container detection so /.dockerenv presence on CI doesn't trip this test
    import yadgar.core.install.install_hooks_lib as lib

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
