"""Tests for yadgar/cli/setup.py — first-run setup subcommand.

Wave 3 coverage: yadgar/cli/setup.py (64 stmts, 7.8% pre-wave).
Strategy: mock YadgarDaemon.check_docker, yadgar.paths, config_yaml, and secrets.
Test _render_secrets_env + cmd_setup in docker-available and fallback modes.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from yadgar.core.cli.setup import (
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
        ):
            cmd_setup(args)

        # Content not changed
        assert existing.read_text() == "EXISTING=yes\n"


# ---------------------------------------------------------------------------
# cmd_setup — docker unavailable
# ---------------------------------------------------------------------------


class TestCmdSetupDockerUnavailable:
    def test_docker_unavailable_prints_warning(self, tmp_path, capsys):
        """Phase 2b: Docker unavailable now shows HTTP config (not stdio fallback)."""
        mock_check = {"ok": False, "reason": "Docker not found"}
        mock_config_path = tmp_path / "config.yaml"
        mock_secrets_path = tmp_path / "secrets.env"
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
        ):
            cmd_setup(args)

        out = capsys.readouterr().out
        # Phase 2b: no longer "command": "yadgar" (stdio); must emit streamable-http config
        assert "streamable-http" in out
        assert '"command"' not in out


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
