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
# Real-machine guard (task:0082)
#
# `yadgar setup` now installs the code_graph binary BY DEFAULT and persists
# `code_graph.enabled` to the runtime-config store. Both are live side effects:
# the install does a real urlopen to GitHub, and the persist POSTs to
# 127.0.0.1:$YADGAR_PORT — which on a developer box is a RUNNING daemon whose
# store the suite must never write to. Stub the download and point the client at
# a dead port for every test in this module; tests needing specific behaviour
# re-patch on top.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_real_installs_or_store_writes(monkeypatch):
    monkeypatch.setenv("YADGAR_PORT", "1")  # connection refused, never the live daemon
    monkeypatch.delenv("YADGAR_MCP_AUTH_TOKEN", raising=False)
    with patch(
        "yadgar.core.install.codebase_memory_mcp.install_codebase_memory_mcp",
        return_value="/stubbed/bin/codebase-memory-mcp",
    ):
        yield


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


# ---------------------------------------------------------------------------
# _resolve_code_graph_action — the pure decision tree
#
# task:0082 REWROTE this tree. The Car-G5 shape (skip / install_only /
# install_persist, branching on TTY + an interactive prompt + a
# CODE_GRAPH_ENABLED env trigger) is GONE: it made `--no-code-graph` the only
# scriptable path while leaving `code_graph.enabled` at its True default.
# The tree is now two coherent outcomes and reads no ambient state.
# ---------------------------------------------------------------------------


def _args(**kw):
    base = {"no_code_graph": False}
    base.update(kw)
    return SimpleNamespace(**base)


class TestResolveCodeGraphAction:
    def test_no_flags_installs(self):
        assert _resolve_code_graph_action(_args()) == "install"

    def test_no_code_graph_flag_opts_out(self):
        assert _resolve_code_graph_action(_args(no_code_graph=True)) == "opt_out"

    def test_only_two_outcomes(self):
        outcomes = {
            _resolve_code_graph_action(_args()),
            _resolve_code_graph_action(_args(no_code_graph=True)),
        }
        assert outcomes == {"install", "opt_out"}

    def test_ignores_legacy_code_graph_env_trigger(self, monkeypatch):
        """CODE_GRAPH_ENABLED is no longer read by setup — the binary installs by
        default, so an env install-trigger has nothing left to trigger."""
        monkeypatch.setenv("CODE_GRAPH_ENABLED", "1")
        assert _resolve_code_graph_action(_args(no_code_graph=True)) == "opt_out"
        monkeypatch.setenv("CODE_GRAPH_ENABLED", "0")
        assert _resolve_code_graph_action(_args()) == "install"

    def test_never_prompts_regardless_of_tty(self):
        """No branch consults stdin, so a TTY changes nothing (and input() is
        never reachable)."""
        import builtins
        import sys

        class _Tty:
            def isatty(self):
                return True

        with (
            patch.object(sys, "stdin", _Tty()),
            patch.object(builtins, "input", _no_prompt),
        ):
            assert _resolve_code_graph_action(_args()) == "install"


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

    def test_default_installs_and_persists_when_daemon_up(self, capsys):
        out, installs, sets = self._run(_args(), set_return=True, capsys=capsys)
        assert installs, "the DEFAULT path must install the binary"
        assert sets == [("code_graph.enabled", True, "global", None)]
        assert "enabled" in out.lower()

    def test_daemon_down_installs_and_prints_manual_step(self, capsys):
        out, installs, sets = self._run(_args(), set_return=False, capsys=capsys)
        assert installs, "binary must be installed even when daemon is down"
        assert sets == [("code_graph.enabled", True, "global", None)]
        # set() returned False (daemon down) → tell the user how to enable manually.
        assert "config_set" in out

    def test_no_code_graph_flag_installs_nothing_and_disables(self, capsys):
        """task:0082 criterion 3 — opting out turns the FLAG off too, so the
        store never claims a feature whose binary was deliberately skipped."""
        out, installs, sets = self._run(_args(no_code_graph=True), set_return=True, capsys=capsys)
        assert installs == []
        assert sets == [("code_graph.enabled", False, "global", None)]
        assert "disabled" in out.lower()

    def test_opt_out_persist_failure_warns_about_the_divergence(self, capsys):
        out, installs, sets = self._run(_args(no_code_graph=True), set_return=False, capsys=capsys)
        assert installs == []
        assert sets == [("code_graph.enabled", False, "global", None)]
        assert "NOT disabled" in out
        assert "config_set" in out

    def test_failed_install_disables_the_flag(self, capsys):
        out, installs, sets = self._run(_args(), set_return=True, install_ok=False, capsys=capsys)
        assert installs, "install must be ATTEMPTED"
        assert sets == [("code_graph.enabled", False, "global", None)]
        assert "install boom" in out

    def test_legacy_env_trigger_is_ignored(self, capsys, monkeypatch):
        """CODE_GRAPH_ENABLED no longer changes anything — default is install."""
        monkeypatch.setenv("CODE_GRAPH_ENABLED", "1")
        _out, installs, sets = self._run(_args(), set_return=True, capsys=capsys)
        assert installs
        assert sets == [("code_graph.enabled", True, "global", None)]


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
        assert "config_set" in out or "yadgar setup" in out

    def test_cmd_setup_code_graph_survives_daemon_unreachable_end_to_end(self, tmp_path, capsys):
        """Full default `yadgar setup` run with a real connection-refused
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


# ---------------------------------------------------------------------------
# task:0082 — unattended default install + store/binary coherence
#
# (a) `yadgar setup` with NO flags and NO usable stdin must complete: it must
#     never prompt, never block, and must install the code_graph binary
#     (the feature is ON by default, so an install that opts OUT of it by
#     default is incoherent).
# (b) After ANY setup run the `code_graph.enabled` state in the runtime-config
#     store and the presence of the host binary must AGREE.
# ---------------------------------------------------------------------------


class _ClosedStdin:
    """A stdin stand-in that is not a TTY and explodes if anything reads it.

    Models `yadgar setup </dev/null` / a detached provisioning shell: any
    attempt to prompt is a hard test failure rather than a hang.
    """

    def isatty(self):
        return False

    def read(self, *a, **k):
        raise AssertionError("setup must never read stdin")

    def readline(self, *a, **k):
        raise AssertionError("setup must never read stdin")

    def fileno(self):
        raise OSError("stdin is closed")


def _no_prompt(*a, **k):
    raise AssertionError("setup must never prompt (input() called)")


class TestUnattendedDefaultInstall:
    """(a) Unattended, flagless setup installs code_graph without prompting."""

    def test_default_action_is_install(self):
        assert _resolve_code_graph_action(_args()) == "install"

    def test_resolve_needs_no_tty_or_prompt_injection(self):
        """The decision tree must not depend on a TTY or a prompt callable —
        that dependency IS the non-interactive-install bug."""
        import inspect

        params = set(inspect.signature(_resolve_code_graph_action).parameters)
        assert params == {"args"}, f"unexpected params: {sorted(params)}"

    def test_cmd_setup_installs_binary_with_closed_stdin(self, tmp_path, capsys):
        import builtins
        import sys

        mock_check = {"ok": True, "version": "24.0"}
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        installs: list = []

        def _fake_install(skip_if_exists=False):
            installs.append(skip_if_exists)
            return str(fake_home / ".local/bin/codebase-memory-mcp")

        with (
            patch("yadgar.core.daemon.daemon.YadgarDaemon.check_docker", return_value=mock_check),
            patch("yadgar._shared.paths.CONFIG_DIR", tmp_path),
            patch("yadgar._shared.paths.DATA_DIR", tmp_path),
            patch("yadgar._shared.paths.STATE_DIR", tmp_path),
            patch("yadgar._shared.paths.SECRETS_ENV_PATH", tmp_path / "secrets.env"),
            patch(
                "yadgar._shared.config.config_yaml.get_config_path",
                return_value=tmp_path / "config.yaml",
            ),
            patch("yadgar._shared.config.config_yaml.cmd_config_init"),
            patch.object(Path, "home", staticmethod(lambda: fake_home)),
            patch(
                "yadgar.core.install.codebase_memory_mcp.install_codebase_memory_mcp",
                side_effect=_fake_install,
            ),
            patch("yadgar.core.runtime_config_client.set", return_value=True),
            patch.object(sys, "stdin", _ClosedStdin()),
            patch.object(builtins, "input", _no_prompt),
        ):
            cmd_setup(SimpleNamespace())  # no flags at all — must not raise

        out = capsys.readouterr().out
        assert "setup complete" in out
        assert installs, "flagless setup must install the code_graph binary"

    def test_install_is_idempotent_skip_if_exists(self, capsys):
        """An already-installed binary must not force a re-download — otherwise
        an offline re-run of `yadgar setup` fails for no reason."""
        from yadgar.core.cli import setup as _setup

        seen: list = []

        def _fake_install(skip_if_exists=False):
            seen.append(skip_if_exists)
            return "/fake/bin/codebase-memory-mcp"

        with patch(
            "yadgar.core.install.codebase_memory_mcp.install_codebase_memory_mcp",
            side_effect=_fake_install,
        ):
            assert _setup._do_install_code_graph() is True

        assert seen == [True], "setup must pass skip_if_exists=True"


class TestCodeGraphStoreBinaryCoherence:
    """(b) The store's enabled-state and the binary's presence cannot diverge."""

    def _run_scenario(self, args, *, install_ok, tmp_path, capsys):
        """Run the code_graph setup step against a fake HOME and report the
        resulting (enabled_state, binary_present) pair."""
        import shutil as _shutil

        from yadgar.core.cli import setup as _setup

        fake_home = tmp_path / "home"
        bin_dir = fake_home / ".local" / "bin"
        bin_dir.mkdir(parents=True)
        set_calls: list = []

        def _fake_install(skip_if_exists=False):
            if not install_ok:
                raise RuntimeError("no network: download failed")
            target = bin_dir / "codebase-memory-mcp"
            target.write_text("#!/bin/sh\n")
            return str(target)

        def _fake_set(key, value, *, scope="global", directory=None):
            set_calls.append((key, value, scope, directory))
            return True

        with (
            patch(
                "yadgar.core.install.codebase_memory_mcp.install_codebase_memory_mcp",
                side_effect=_fake_install,
            ),
            patch("yadgar.core.runtime_config_client.set", side_effect=_fake_set),
            patch.object(Path, "home", staticmethod(lambda: fake_home)),
            # Never let the developer's real PATH answer the presence question.
            patch.object(_shutil, "which", return_value=None),
        ):
            _setup._maybe_install_code_graph(args)

            from yadgar.core.code_graph import runner

            binary_present = runner.resolve_binary() is not None

        # ADR-0163: no row → is_enabled() resolves True (default-on). Fold the
        # writes setup actually made over that default.
        enabled = True
        for key, value, _scope, _directory in set_calls:
            if key == "code_graph.enabled":
                enabled = bool(value)

        return enabled, binary_present, set_calls, capsys.readouterr().out

    def test_default_setup_enabled_and_binary_present(self, tmp_path, capsys):
        enabled, present, _sets, _out = self._run_scenario(
            _args(), install_ok=True, tmp_path=tmp_path, capsys=capsys
        )
        assert enabled is True
        assert present is True
        assert enabled == present, "store enable-state and binary presence must agree"

    def test_opt_out_disables_flag_and_installs_nothing(self, tmp_path, capsys):
        """Criterion 3: opting out must ALSO disable code_graph.enabled — not
        merely skip the binary (that is the divergence the user reported)."""
        enabled, present, sets, _out = self._run_scenario(
            _args(no_code_graph=True), install_ok=True, tmp_path=tmp_path, capsys=capsys
        )
        assert present is False
        assert enabled is False
        assert enabled == present
        assert ("code_graph.enabled", False, "global", None) in sets

    def test_failed_install_disables_flag(self, tmp_path, capsys):
        """Criterion 6 + coherence: a download that cannot succeed must not
        leave the feature enabled with no binary behind it."""
        enabled, present, sets, out = self._run_scenario(
            _args(), install_ok=False, tmp_path=tmp_path, capsys=capsys
        )
        assert present is False
        assert enabled is False
        assert enabled == present
        assert ("code_graph.enabled", False, "global", None) in sets
        assert "no network" in out

    @pytest.mark.parametrize("install_ok", [True, False])
    @pytest.mark.parametrize("opt_out", [True, False])
    def test_states_never_diverge(self, install_ok, opt_out, tmp_path, capsys):
        enabled, present, _sets, _out = self._run_scenario(
            _args(no_code_graph=opt_out), install_ok=install_ok, tmp_path=tmp_path, capsys=capsys
        )
        assert enabled == present, (
            f"divergence: enabled={enabled} binary_present={present} "
            f"(opt_out={opt_out}, install_ok={install_ok})"
        )


class TestCodeGraphInstallDegradesGracefully:
    """Criterion 6: an impossible binary install must never fail the whole setup."""

    def _install_raising(self, exc, tmp_path, capsys):
        mock_check = {"ok": True, "version": "24.0"}
        fake_home = tmp_path / "home"
        fake_home.mkdir()

        with (
            patch("yadgar.core.daemon.daemon.YadgarDaemon.check_docker", return_value=mock_check),
            patch("yadgar._shared.paths.CONFIG_DIR", tmp_path),
            patch("yadgar._shared.paths.DATA_DIR", tmp_path),
            patch("yadgar._shared.paths.STATE_DIR", tmp_path),
            patch("yadgar._shared.paths.SECRETS_ENV_PATH", tmp_path / "secrets.env"),
            patch(
                "yadgar._shared.config.config_yaml.get_config_path",
                return_value=tmp_path / "config.yaml",
            ),
            patch("yadgar._shared.config.config_yaml.cmd_config_init"),
            patch.object(Path, "home", staticmethod(lambda: fake_home)),
            patch(
                "yadgar.core.install.codebase_memory_mcp.install_codebase_memory_mcp",
                side_effect=exc,
            ),
            patch("yadgar.core.runtime_config_client.set", return_value=True),
        ):
            cmd_setup(SimpleNamespace())  # must not raise

        return capsys.readouterr().out

    def test_offline_download_failure_completes_setup(self, tmp_path, capsys):
        import urllib.error

        out = self._install_raising(
            urllib.error.URLError("Network is unreachable"), tmp_path, capsys
        )
        assert "setup complete" in out
        assert "Network is unreachable" in out
        assert "code_graph" in out

    def test_unsupported_platform_completes_setup(self, tmp_path, capsys):
        out = self._install_raising(RuntimeError("Unsupported OS 'Windows'"), tmp_path, capsys)
        assert "setup complete" in out
        assert "Unsupported OS" in out

    def test_failure_message_names_a_real_retry_path(self, tmp_path, capsys):
        out = self._install_raising(RuntimeError("boom"), tmp_path, capsys)
        # Must not point users at a flag that no longer exists.
        assert "--code-graph" not in out
        assert "yadgar setup" in out


class TestCodeGraphCliSurface:
    """Criterion 4: the CLI collapses to a single opt-out flag."""

    def _parser(self):
        import argparse

        root = argparse.ArgumentParser()
        subs = root.add_subparsers()
        register(subs)
        return root

    def test_no_code_graph_flag_still_accepted(self):
        args = self._parser().parse_args(["setup", "--no-code-graph"])
        assert args.no_code_graph is True

    def test_code_graph_opt_in_flag_is_gone(self):
        """`--code-graph` was a no-op once enabled=True became the default —
        the surface collapses to the opt-out."""
        with pytest.raises(SystemExit):
            self._parser().parse_args(["setup", "--code-graph"])

    def test_default_leaves_no_code_graph_false(self):
        args = self._parser().parse_args(["setup"])
        assert getattr(args, "no_code_graph", False) is False
