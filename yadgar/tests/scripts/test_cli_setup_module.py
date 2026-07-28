"""Tests for yadgar/cli/setup.py — first-run setup subcommand.

Wave 3 coverage: yadgar/cli/setup.py (64 stmts, 7.8% pre-wave).
Strategy: mock YadgarDaemon.check_docker, yadgar.paths, config_yaml, and secrets.
Test _render_secrets_env + cmd_setup in docker-available and fallback modes.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from yadgar.core.cli.setup import (
    _existing_secrets_token,
    _register_claude_code_mcp,
    _render_secrets_env,
    _resolve_code_graph_action,
    cmd_setup,
    register,
)

# ---------------------------------------------------------------------------
# _render_secrets_env
# ---------------------------------------------------------------------------


class TestRenderSecretsEnv:
    def test_contains_mcp_auth_token(self):
        out = _render_secrets_env("tok123", "dbpass", "rwpass", "ropass")
        assert "YADGAR_MCP_AUTH_TOKEN=tok123" in out

    def test_contains_surreal_user(self):
        out = _render_secrets_env("tok", "dp", "rw", "ro")
        assert "SURREAL_USER=root" in out

    def test_contains_surreal_pass(self):
        out = _render_secrets_env("tok", "dbpass", "rw", "ro")
        assert "SURREAL_PASS=dbpass" in out

    def test_contains_rw_credentials(self):
        out = _render_secrets_env("tok", "dp", "rwpass", "ro")
        assert "YADGAR_RW_PASS=rwpass" in out

    def test_contains_ro_credentials(self):
        out = _render_secrets_env("tok", "dp", "rw", "ropass")
        assert "YADGAR_RO_PASS=ropass" in out

    def test_contains_db_aliases(self):
        out = _render_secrets_env("tok", "dp", "rwpass", "ro")
        assert "YADGAR_DB_USER=yadgar" in out
        assert "YADGAR_DB_PASS=rwpass" in out

    def test_is_string(self):
        out = _render_secrets_env("t", "d", "r", "o")
        assert isinstance(out, str)


# ---------------------------------------------------------------------------
# register() — parser wiring
# ---------------------------------------------------------------------------


class TestRegister:
    def test_register_creates_setup_subparser(self):
        import argparse

        root = argparse.ArgumentParser()
        subs = root.add_subparsers()
        register(subs)
        args = root.parse_args(["setup"])
        assert hasattr(args, "func")

    def test_register_accepts_no_code_graph(self):
        import argparse

        root = argparse.ArgumentParser()
        subs = root.add_subparsers()
        register(subs)
        args = root.parse_args(["setup", "--no-code-graph"])
        assert getattr(args, "no_code_graph", False) is True

    def test_code_graph_and_no_code_graph_are_mutually_exclusive(self):
        import argparse

        root = argparse.ArgumentParser()
        subs = root.add_subparsers()
        register(subs)
        with pytest.raises(SystemExit):
            root.parse_args(["setup", "--code-graph", "--no-code-graph"])


# ---------------------------------------------------------------------------
# _resolve_code_graph_action — the pure decision tree (Car G5)
# ---------------------------------------------------------------------------


def _args(**kw):
    base = {"code_graph": False, "no_code_graph": False}
    base.update(kw)
    return SimpleNamespace(**base)


class TestResolveCodeGraphAction:
    def test_no_code_graph_flag_skips(self):
        # --no-code-graph wins even over a TTY / env / --code-graph.
        action = _resolve_code_graph_action(
            _args(no_code_graph=True, code_graph=True),
            isatty=True,
            env_enabled=True,
            prompt_fn=lambda: True,
        )
        assert action == "skip"

    def test_code_graph_flag_installs_and_persists(self):
        action = _resolve_code_graph_action(
            _args(code_graph=True), isatty=False, env_enabled=False, prompt_fn=lambda: False
        )
        assert action == "install_persist"

    def test_env_flag_installs_only_no_persist(self):
        # CODE_GRAPH_ENABLED is an INSTALL trigger, not a runtime-enable persist.
        action = _resolve_code_graph_action(
            _args(), isatty=True, env_enabled=True, prompt_fn=lambda: True
        )
        assert action == "install_only"

    def test_interactive_yes_installs_and_persists(self):
        action = _resolve_code_graph_action(
            _args(), isatty=True, env_enabled=False, prompt_fn=lambda: True
        )
        assert action == "install_persist"

    def test_interactive_no_skips(self):
        action = _resolve_code_graph_action(
            _args(), isatty=True, env_enabled=False, prompt_fn=lambda: False
        )
        assert action == "skip"

    def test_non_interactive_no_flag_no_env_skips_without_prompting(self):
        # THE no-hang guarantee: no TTY + no flag + no env → skip, prompt NEVER called.
        called = {"n": 0}

        def _prompt():
            called["n"] += 1
            return True

        action = _resolve_code_graph_action(
            _args(), isatty=False, env_enabled=False, prompt_fn=_prompt
        )
        assert action == "skip"
        assert called["n"] == 0, "must not prompt when stdin is not a TTY"


# ---------------------------------------------------------------------------
# cmd_setup — docker available
# ---------------------------------------------------------------------------


class TestCmdSetupDockerAvailable:
    def _run(self, tmp_path, capsys):
        mock_check = {"ok": True, "version": "24.0"}
        mock_config_path = tmp_path / "config.yaml"
        mock_secrets_path = tmp_path / "secrets.env"
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        args = SimpleNamespace()

        with (
            patch("yadgar.core.daemon.daemon.YadgarDaemon.check_docker", return_value=mock_check),
            patch("yadgar._shared.paths.CONFIG_DIR", tmp_path),
            patch("yadgar._shared.paths.DATA_DIR", tmp_path),
            patch("yadgar._shared.paths.STATE_DIR", tmp_path),
            patch("yadgar._shared.paths.SECRETS_ENV_PATH", mock_secrets_path),
            patch(
                "yadgar._shared.config.config_yaml.get_config_path", return_value=mock_config_path
            ),
            patch("yadgar._shared.config.config_yaml.cmd_config_init"),
            # NEVER let cmd_setup's now-real MCP registration touch the actual
            # host $HOME — confine ~/.claude.json to a tmp fake home.
            patch.object(Path, "home", staticmethod(lambda: fake_home)),
        ):
            cmd_setup(args)

        return capsys.readouterr().out, mock_secrets_path

    def test_prints_docker_ok(self, tmp_path, capsys):
        out, _ = self._run(tmp_path, capsys)
        assert "✓" in out

    def test_prints_setup_complete(self, tmp_path, capsys):
        out, _ = self._run(tmp_path, capsys)
        assert "setup complete" in out

    def test_writes_secrets_file(self, tmp_path, capsys):
        _, secrets_path = self._run(tmp_path, capsys)
        assert secrets_path.exists()

    def test_secrets_file_has_correct_mode(self, tmp_path, capsys):
        import stat

        _, secrets_path = self._run(tmp_path, capsys)
        mode = stat.S_IMODE(secrets_path.stat().st_mode)
        assert mode == 0o600

    def test_prints_next_steps_with_docker(self, tmp_path, capsys):
        out, _ = self._run(tmp_path, capsys)
        assert "daemon start" in out or "yadgar" in out

    def test_prints_mcp_config_json(self, tmp_path, capsys):
        out, _ = self._run(tmp_path, capsys)
        assert "streamable-http" in out or "mcpServers" in out

    def test_existing_secrets_file_kept(self, tmp_path, capsys):
        existing = tmp_path / "secrets.env"
        existing.write_text("EXISTING=yes\n")
        existing.chmod(0o600)

        mock_check = {"ok": True, "version": "24.0"}
        mock_config_path = tmp_path / "config.yaml"
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        args = SimpleNamespace()

        with (
            patch("yadgar.core.daemon.daemon.YadgarDaemon.check_docker", return_value=mock_check),
            patch("yadgar._shared.paths.CONFIG_DIR", tmp_path),
            patch("yadgar._shared.paths.DATA_DIR", tmp_path),
            patch("yadgar._shared.paths.STATE_DIR", tmp_path),
            patch("yadgar._shared.paths.SECRETS_ENV_PATH", existing),
            patch(
                "yadgar._shared.config.config_yaml.get_config_path", return_value=mock_config_path
            ),
            patch("yadgar._shared.config.config_yaml.cmd_config_init"),
            patch.object(Path, "home", staticmethod(lambda: fake_home)),
        ):
            cmd_setup(args)

        # Content not changed
        assert existing.read_text() == "EXISTING=yes\n"
        # No token line in the legacy secrets.env → MCP registration must be
        # skipped (not write a headerless/broken entry), and ~/.claude.json
        # must not even be created.
        assert not (fake_home / ".claude.json").exists()


# ---------------------------------------------------------------------------
# cmd_setup — docker unavailable
# ---------------------------------------------------------------------------


class TestCmdSetupDockerUnavailable:
    def test_docker_unavailable_prints_warning(self, tmp_path, capsys):
        """Phase 2b: Docker unavailable now shows HTTP config (not stdio fallback)."""
        mock_check = {"ok": False, "reason": "Docker not found"}
        mock_config_path = tmp_path / "config.yaml"
        mock_secrets_path = tmp_path / "secrets.env"
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        args = SimpleNamespace()

        with (
            patch("yadgar.core.daemon.daemon.YadgarDaemon.check_docker", return_value=mock_check),
            patch("yadgar._shared.paths.CONFIG_DIR", tmp_path),
            patch("yadgar._shared.paths.DATA_DIR", tmp_path),
            patch("yadgar._shared.paths.STATE_DIR", tmp_path),
            patch("yadgar._shared.paths.SECRETS_ENV_PATH", mock_secrets_path),
            patch(
                "yadgar._shared.config.config_yaml.get_config_path", return_value=mock_config_path
            ),
            patch("yadgar._shared.config.config_yaml.cmd_config_init"),
            patch.object(Path, "home", staticmethod(lambda: fake_home)),
        ):
            cmd_setup(args)

        out = capsys.readouterr().out
        # Phase 2b: still shows ✗ for Docker unavailable; no longer offers stdio mode
        assert "✗" in out or "Docker unavailable" in out or "Docker not found" in out

    def test_docker_unavailable_prints_http_config(self, tmp_path, capsys):
        """Phase 2b: Docker unavailable still emits streamable-HTTP config (not stdio command)."""
        mock_check = {"ok": False, "reason": "Docker not found"}
        mock_config_path = tmp_path / "config.yaml"
        mock_secrets_path = tmp_path / "secrets.env"
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        args = SimpleNamespace()

        with (
            patch("yadgar.core.daemon.daemon.YadgarDaemon.check_docker", return_value=mock_check),
            patch("yadgar._shared.paths.CONFIG_DIR", tmp_path),
            patch("yadgar._shared.paths.DATA_DIR", tmp_path),
            patch("yadgar._shared.paths.STATE_DIR", tmp_path),
            patch("yadgar._shared.paths.SECRETS_ENV_PATH", mock_secrets_path),
            patch(
                "yadgar._shared.config.config_yaml.get_config_path", return_value=mock_config_path
            ),
            patch("yadgar._shared.config.config_yaml.cmd_config_init"),
            patch.object(Path, "home", staticmethod(lambda: fake_home)),
        ):
            cmd_setup(args)

        out = capsys.readouterr().out
        # Phase 2b: no longer "command": "yadgar" (stdio); must emit streamable-http config
        assert "streamable-http" in out
        assert '"command"' not in out


# ---------------------------------------------------------------------------
# _existing_secrets_token — parse YADGAR_MCP_AUTH_TOKEN= from secrets.env
# ---------------------------------------------------------------------------


class TestExistingSecretsToken:
    def test_parses_token_line(self, tmp_path):
        secrets_path = tmp_path / "secrets.env"
        secrets_path.write_text("FOO=bar\nYADGAR_MCP_AUTH_TOKEN=abc123\nBAZ=qux\n")
        assert _existing_secrets_token(secrets_path) == "abc123"

    def test_missing_file_returns_empty(self, tmp_path):
        assert _existing_secrets_token(tmp_path / "nope.env") == ""

    def test_no_token_line_returns_empty(self, tmp_path):
        secrets_path = tmp_path / "secrets.env"
        secrets_path.write_text("EXISTING=yes\n")
        assert _existing_secrets_token(secrets_path) == ""


# ---------------------------------------------------------------------------
# _register_claude_code_mcp / cmd_setup — ADR-0161 / task #37: `yadgar setup`
# must actually WRITE the MCP registration, not just print instructions.
# ---------------------------------------------------------------------------


class TestRegisterClaudeCodeMcp:
    def test_empty_token_skips_without_writing(self, tmp_path, capsys):
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        with patch.object(Path, "home", staticmethod(lambda: fake_home)):
            result = _register_claude_code_mcp("")
        assert result is None
        assert not (fake_home / ".claude.json").exists()
        assert "skipped" in capsys.readouterr().out.lower()

    def test_writes_entry_with_bearer_token(self, tmp_path, capsys):
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        with patch.object(Path, "home", staticmethod(lambda: fake_home)):
            result = _register_claude_code_mcp("tok-xyz")

        claude_json = fake_home / ".claude.json"
        assert claude_json.exists()
        written = json.loads(claude_json.read_text())
        entry = written["mcpServers"]["yadgar"]
        assert entry["type"] == "streamable-http"
        assert entry["headers"]["Authorization"] == "Bearer tok-xyz"
        assert result is not None
        assert result["updated"] == str(claude_json)
        assert "registered" in capsys.readouterr().out.lower()

    def test_preserves_foreign_mcp_servers(self, tmp_path):
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        claude_json = fake_home / ".claude.json"
        claude_json.write_text(
            json.dumps({"mcpServers": {"other": {"type": "stdio", "command": "foo"}}})
        )
        with patch.object(Path, "home", staticmethod(lambda: fake_home)):
            _register_claude_code_mcp("tok-xyz")

        written = json.loads(claude_json.read_text())
        assert written["mcpServers"]["other"] == {"type": "stdio", "command": "foo"}
        assert written["mcpServers"]["yadgar"]["headers"]["Authorization"] == "Bearer tok-xyz"

    def test_registration_failure_does_not_raise(self, tmp_path, capsys):
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        with (
            patch.object(Path, "home", staticmethod(lambda: fake_home)),
            patch(
                "yadgar.core.install.clients.mcp_register.register_mcp",
                side_effect=OSError("disk full"),
            ),
        ):
            result = _register_claude_code_mcp("tok-xyz")
        assert result is None
        assert "failed" in capsys.readouterr().out.lower()


class TestCmdSetupWritesMcpRegistration:
    """Integration: a fresh `yadgar setup` run must leave Claude Code configured
    without a separate manual `yadgar daemon configure-mcp` step (ADR-0161, #37).

    RED against the pre-fix `cmd_setup` (which only printed the JSON snippet and
    never wrote ~/.claude.json) — GREEN now that it calls the real registration.
    """

    def _run(self, tmp_path, capsys, *, existing_secrets_text: str | None = None):
        mock_check = {"ok": True, "version": "24.0"}
        mock_config_path = tmp_path / "config.yaml"
        mock_secrets_path = tmp_path / "secrets.env"
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        if existing_secrets_text is not None:
            mock_secrets_path.write_text(existing_secrets_text)
        args = SimpleNamespace()

        with (
            patch("yadgar.core.daemon.daemon.YadgarDaemon.check_docker", return_value=mock_check),
            patch("yadgar._shared.paths.CONFIG_DIR", tmp_path),
            patch("yadgar._shared.paths.DATA_DIR", tmp_path),
            patch("yadgar._shared.paths.STATE_DIR", tmp_path),
            patch("yadgar._shared.paths.SECRETS_ENV_PATH", mock_secrets_path),
            patch(
                "yadgar._shared.config.config_yaml.get_config_path", return_value=mock_config_path
            ),
            patch("yadgar._shared.config.config_yaml.cmd_config_init"),
            patch.object(Path, "home", staticmethod(lambda: fake_home)),
        ):
            cmd_setup(args)

        return capsys.readouterr().out, fake_home

    def test_fresh_secrets_registers_mcp_for_claude_code(self, tmp_path, capsys):
        out, fake_home = self._run(tmp_path, capsys)

        claude_json = fake_home / ".claude.json"
        assert claude_json.exists(), "cmd_setup must WRITE ~/.claude.json, not just print it"
        entry = json.loads(claude_json.read_text())["mcpServers"]["yadgar"]
        assert entry["type"] == "streamable-http"
        assert entry["headers"]["Authorization"].startswith("Bearer ")
        assert entry["headers"]["Authorization"] != "Bearer ${YADGAR_MCP_AUTH_TOKEN}"
        # Next-steps no longer instruct a separate manual configure-mcp step.
        assert "yadgar daemon configure-mcp" not in out

    def test_existing_secrets_with_token_registers_mcp(self, tmp_path, capsys):
        out, fake_home = self._run(
            tmp_path,
            capsys,
            existing_secrets_text=("YADGAR_MCP_AUTH_TOKEN=preexisting-tok\nSURREAL_USER=root\n"),
        )

        claude_json = fake_home / ".claude.json"
        assert claude_json.exists()
        entry = json.loads(claude_json.read_text())["mcpServers"]["yadgar"]
        assert entry["headers"]["Authorization"] == "Bearer preexisting-tok"
        assert "yadgar daemon configure-mcp" not in out


# ---------------------------------------------------------------------------
# _maybe_install_code_graph — install + on-enable persist (Car G5)
# ---------------------------------------------------------------------------


class TestMaybeInstallCodeGraph:
    def _run(self, args, *, set_return=True, install_ok=True, capsys):
        from yadgar.core.cli import setup as _setup

        install_calls: list = []
        set_calls: list = []

        def _fake_install(skip_if_exists=False):
            install_calls.append(skip_if_exists)
            if not install_ok:
                raise RuntimeError("install boom")
            return "/home/x/.local/bin/codebase-memory-mcp"

        def _fake_set(key, value, *, scope="global", directory=None):
            set_calls.append((key, value, scope, directory))
            return set_return

        with (
            patch(
                "yadgar.core.install.codebase_memory_mcp.install_codebase_memory_mcp",
                side_effect=_fake_install,
            ),
            patch("yadgar.core.install.codebase_memory_mcp.BINARY_NAME", "codebase-memory-mcp"),
            patch("yadgar.core.install.codebase_memory_mcp.VERSION", "v0.9.0"),
            patch("yadgar.core.runtime_config_client.set", side_effect=_fake_set),
        ):
            _setup._maybe_install_code_graph(args)

        return capsys.readouterr().out, install_calls, set_calls

    def test_flag_installs_and_persists_when_daemon_up(self, capsys):
        out, installs, sets = self._run(_args(code_graph=True), set_return=True, capsys=capsys)
        assert installs, "binary must be installed"
        assert sets == [("code_graph.enabled", True, "global", None)]
        assert "enabled" in out.lower()

    def test_daemon_down_installs_and_prints_manual_step(self, capsys):
        out, installs, sets = self._run(_args(code_graph=True), set_return=False, capsys=capsys)
        assert installs, "binary must be installed even when daemon is down"
        assert sets == [("code_graph.enabled", True, "global", None)]
        # set() returned False (daemon down) → tell the user how to enable manually.
        assert "config set code_graph.enabled true" in out or "config_set" in out

    def test_no_code_graph_flag_installs_nothing(self, capsys):
        out, installs, sets = self._run(_args(no_code_graph=True), set_return=True, capsys=capsys)
        assert installs == []
        assert sets == []

    def test_env_only_installs_without_persist(self, capsys, monkeypatch):
        monkeypatch.setenv("CODE_GRAPH_ENABLED", "1")
        out, installs, sets = self._run(_args(), set_return=True, capsys=capsys)
        assert installs, "env trigger must install the binary"
        assert sets == [], "env trigger must NOT persist the runtime-enable"

    def test_non_interactive_no_flag_no_env_does_nothing(self, capsys, monkeypatch):
        monkeypatch.delenv("CODE_GRAPH_ENABLED", raising=False)
        # Force a non-TTY stdin.
        with patch("sys.stdin.isatty", return_value=False):
            out, installs, sets = self._run(_args(), capsys=capsys)
        assert installs == []
        assert sets == []


# ---------------------------------------------------------------------------
# Lock-in regression (task #37 / ADR-0161): `--code-graph` enable-persist must
# degrade gracefully — not crash `yadgar setup` — when the daemon is genuinely
# unreachable at the socket level. Unlike TestMaybeInstallCodeGraph above
# (which mocks `runtime_config_client.set` directly), this drives a REAL
# connection failure through `urllib.request.urlopen` so the whole chain
# (setup.py -> runtime_config_client.set -> urllib) is exercised end to end.
# This behavior was ALREADY correct before this change (runtime_config_client
# .set() catches every exception and returns False, never raises — see
# yadgar/tests/core/test_runtime_config_client.py::test_connection_refused_returns_false)
# — this test locks it in at the setup.py integration layer as well.
# ---------------------------------------------------------------------------


class TestCodeGraphPersistSurvivesRealConnectionRefused:
    def test_persist_code_graph_enable_does_not_raise_on_connection_refused(self, capsys):
        import urllib.error

        from yadgar.core import runtime_config_client
        from yadgar.core.cli.setup import _persist_code_graph_enable

        with patch.object(
            runtime_config_client._req,
            "urlopen",
            side_effect=urllib.error.URLError("Connection refused"),
        ):
            # Must not raise.
            _persist_code_graph_enable()

        out = capsys.readouterr().out
        assert "not reachable" in out or "not persisted" in out
        assert "config_set" in out or "yadgar setup --code-graph" in out

    def test_cmd_setup_code_graph_survives_daemon_unreachable_end_to_end(self, tmp_path, capsys):
        """Full `yadgar setup --code-graph` run with a real connection-refused
        error on the persist call: binary install still happens, setup still
        reaches 'setup complete', and no exception escapes cmd_setup."""
        import urllib.error

        from yadgar.core import runtime_config_client

        mock_check = {"ok": True, "version": "24.0"}
        mock_config_path = tmp_path / "config.yaml"
        mock_secrets_path = tmp_path / "secrets.env"
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        args = SimpleNamespace(code_graph=True, no_code_graph=False)

        with (
            patch("yadgar.core.daemon.daemon.YadgarDaemon.check_docker", return_value=mock_check),
            patch("yadgar._shared.paths.CONFIG_DIR", tmp_path),
            patch("yadgar._shared.paths.DATA_DIR", tmp_path),
            patch("yadgar._shared.paths.STATE_DIR", tmp_path),
            patch("yadgar._shared.paths.SECRETS_ENV_PATH", mock_secrets_path),
            patch(
                "yadgar._shared.config.config_yaml.get_config_path", return_value=mock_config_path
            ),
            patch("yadgar._shared.config.config_yaml.cmd_config_init"),
            patch.object(Path, "home", staticmethod(lambda: fake_home)),
            patch(
                "yadgar.core.install.codebase_memory_mcp.install_codebase_memory_mcp",
                return_value="/home/x/.local/bin/codebase-memory-mcp",
            ),
            patch("yadgar.core.install.codebase_memory_mcp.BINARY_NAME", "codebase-memory-mcp"),
            patch("yadgar.core.install.codebase_memory_mcp.VERSION", "v0.9.0"),
            patch.object(
                runtime_config_client._req,
                "urlopen",
                side_effect=urllib.error.URLError("Connection refused"),
            ),
        ):
            cmd_setup(args)  # must not raise

        out = capsys.readouterr().out
        assert "setup complete" in out
        assert "not reachable" in out or "not persisted" in out
