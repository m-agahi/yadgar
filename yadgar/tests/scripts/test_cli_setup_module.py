"""Tests for yadgar/cli/setup.py — first-run setup subcommand.

Wave 3 coverage: yadgar/cli/setup.py (64 stmts, 7.8% pre-wave).
Strategy: mock YadgarDaemon.check_docker, yadgar.paths, config_yaml, and secrets.
Test _render_secrets_env + cmd_setup in docker-available and fallback modes.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from yadgar.core.cli.setup import _render_secrets_env, cmd_setup, register

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
