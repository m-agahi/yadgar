"""Tests for `yadgar install --client claude-code` MCP auth-token resolution.

2026-07-28 fresh-VM QA fix (docs/plans/fix-claude-code-mcp-auth-token-missing-2026-07-28.md).

Prior bug: `cmd_install` (`cli/install.py:97`) and `register_mcp_for_claude_code`
(`mcp_register.py:274`) resolved `YADGAR_MCP_AUTH_TOKEN` from `os.environ` ONLY.
The real source of truth on a real install is `~/.config/yadgar/secrets.env`
(sourced by the daemon, but NOT by the interactive shell where `yadgar install`
runs) — so a fresh VM whose shell hadn't sourced secrets.env produced a
headerless, unauthenticated MCP entry in `~/.claude.json`.

AC-2 / AC-3: these tests run the REAL `cmd_install` / `register_mcp_for_claude_code`
code paths (no mocking of the serializer or of register_mcp) with
`YADGAR_MCP_AUTH_TOKEN` absent from the environment and a temp secrets.env
(via `$YADGAR_SECRETS_ENV_FILE`) holding a known token, then read back the
written `~/.claude.json` and assert the Authorization header is present.
This mirrors the exact fresh-VM condition and is the seam the 2026-07-28 bug
lived in — no prior test exercised it (only the serializer was tested, with
the token hand-fed directly).

AC-4: `--print` must still emit the env-ref, never resolve/leak the literal
token that secrets.env now supplies to the write path.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from yadgar.core.cli.install import cmd_install

_KNOWN_TOKEN = "e2e-secrets-env-token-abc123"


def _write_secrets_env(tmp_path: Path, token: str = _KNOWN_TOKEN) -> Path:
    p = tmp_path / "secrets.env"
    p.write_text(f"SOME_OTHER_VAR=1\nYADGAR_MCP_AUTH_TOKEN={token}\nTRAILING=x\n")
    return p


def _install_args(**overrides) -> SimpleNamespace:
    """Minimal argparse-shaped namespace for cmd_install.

    Defaults to mcp-only (rules/hooks off) so tests only exercise the
    token-resolution seam, not the rules or hooks writers (out of scope for
    this fix — see the plan's "Scope discipline" section).
    """
    base: dict[str, object] = {
        "client": "claude-code",
        "auto_detect": False,
        "mcp": True,
        "rules": False,
        "hooks": False,
        "no_hooks": True,
        "print": False,
        "port": 8765,
        "scope": "global",
        "project_directory": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


# ── AC-2: cmd_install resolves the token from secrets.env when env unset ────


class TestCmdInstallTokenResolution:
    def test_env_absent_secrets_env_present_writes_auth_header(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        secrets_path = _write_secrets_env(tmp_path)

        monkeypatch.delenv("YADGAR_MCP_AUTH_TOKEN", raising=False)
        monkeypatch.setenv("YADGAR_SECRETS_ENV_FILE", str(secrets_path))
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

        cmd_install(_install_args())

        claude_json = home / ".claude.json"
        assert claude_json.exists()
        data = json.loads(claude_json.read_text())
        assert data["mcpServers"]["yadgar"]["headers"]["Authorization"] == f"Bearer {_KNOWN_TOKEN}"

    def test_env_present_still_wins_over_secrets_env(self, tmp_path, monkeypatch):
        """Explicit env override still wins (R3: intentional precedence, unchanged)."""
        home = tmp_path / "home"
        home.mkdir()
        secrets_path = _write_secrets_env(tmp_path, token="file-token-should-not-win")

        monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", "env-token-wins")
        monkeypatch.setenv("YADGAR_SECRETS_ENV_FILE", str(secrets_path))
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

        cmd_install(_install_args())

        data = json.loads((home / ".claude.json").read_text())
        assert data["mcpServers"]["yadgar"]["headers"]["Authorization"] == "Bearer env-token-wins"

    def test_both_absent_writes_headerless_entry_with_loud_warning(
        self, tmp_path, monkeypatch, capsys
    ):
        """OD-1: no token resolvable at all -> loud warn, non-fatal, headerless entry."""
        home = tmp_path / "home"
        home.mkdir()
        missing_secrets = tmp_path / "does-not-exist-secrets.env"

        monkeypatch.delenv("YADGAR_MCP_AUTH_TOKEN", raising=False)
        monkeypatch.setenv("YADGAR_SECRETS_ENV_FILE", str(missing_secrets))
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

        cmd_install(_install_args())

        data = json.loads((home / ".claude.json").read_text())
        assert "headers" not in data["mcpServers"]["yadgar"]
        err = capsys.readouterr().err
        assert "token" in err.lower()


# ── AC-3: register_mcp_for_claude_code (configure-mcp back-compat) ──────────


class TestRegisterMcpForClaudeCodeTokenResolution:
    def test_env_absent_secrets_env_present_writes_auth_header(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        secrets_path = _write_secrets_env(tmp_path)

        monkeypatch.delenv("YADGAR_MCP_AUTH_TOKEN", raising=False)
        monkeypatch.setenv("YADGAR_SECRETS_ENV_FILE", str(secrets_path))
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

        from yadgar.core.install.clients.mcp_register import register_mcp_for_claude_code

        result = register_mcp_for_claude_code(port=8765)
        assert result["new"]["headers"]["Authorization"] == f"Bearer {_KNOWN_TOKEN}"

        data = json.loads((home / ".claude.json").read_text())
        assert data["mcpServers"]["yadgar"]["headers"]["Authorization"] == f"Bearer {_KNOWN_TOKEN}"

    def test_both_absent_headerless_with_warning(self, tmp_path, monkeypatch, capsys):
        home = tmp_path / "home"
        home.mkdir()
        missing_secrets = tmp_path / "does-not-exist-secrets.env"

        monkeypatch.delenv("YADGAR_MCP_AUTH_TOKEN", raising=False)
        monkeypatch.setenv("YADGAR_SECRETS_ENV_FILE", str(missing_secrets))
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

        from yadgar.core.install.clients.mcp_register import register_mcp_for_claude_code

        result = register_mcp_for_claude_code(port=8765)
        assert "headers" not in result["new"]
        err = capsys.readouterr().err
        assert "token" in err.lower()


# ── AC-4: --print never leaks the resolved literal token ────────────────────


class TestCmdInstallPrintNeverLeaksToken:
    @pytest.mark.parametrize("client_name", ["claude-code", "opencode"])
    def test_print_emits_envref_not_literal(self, tmp_path, monkeypatch, capsys, client_name):
        home = tmp_path / "home"
        home.mkdir()
        secrets_path = _write_secrets_env(tmp_path)

        monkeypatch.delenv("YADGAR_MCP_AUTH_TOKEN", raising=False)
        monkeypatch.setenv("YADGAR_SECRETS_ENV_FILE", str(secrets_path))
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

        cmd_install(_install_args(client=client_name, print=True))

        out = capsys.readouterr().out
        assert _KNOWN_TOKEN not in out
        assert "${YADGAR_MCP_AUTH_TOKEN}" in out
        # --print must not write any file.
        assert not (home / ".claude.json").exists()
