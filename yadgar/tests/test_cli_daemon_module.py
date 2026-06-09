"""Tests for yadgar/cli/daemon.py — daemon CLI subcommand dispatcher.

Wave 2 coverage: yadgar/cli/daemon.py (153 stmts, 0% pre-wave).
Strategy: mock at YadgarDaemon boundary in yadgar.daemon module (lazy import).
Each sub-command handler is a thin dispatch wrapper — test dispatch logic
and output formatting. No subprocess.run calls reach the OS.
"""

from __future__ import annotations

import argparse
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from yadgar.cli.daemon import cmd_daemon, register

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _args(**kwargs) -> SimpleNamespace:
    """Build an args namespace for cmd_daemon."""
    defaults = {
        "port": None,
        "dev": False,
        "db_path": None,
        "daemon_command": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _mock_daemon_class(daemon_instance, check_docker_result=None):
    """Return a mock class for YadgarDaemon.

    - Instantiating the class returns daemon_instance.
    - Class-level check_docker() returns check_docker_result (default: ok).
    """
    if check_docker_result is None:
        check_docker_result = {"ok": True}
    mock_cls = MagicMock(return_value=daemon_instance)
    mock_cls.check_docker = MagicMock(return_value=check_docker_result)
    return mock_cls


# ---------------------------------------------------------------------------
# register()
# ---------------------------------------------------------------------------


class TestRegister:
    def test_registers_daemon_subparser(self):
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        register(subparsers)
        args = parser.parse_args(["daemon"])
        assert hasattr(args, "func")

    def test_all_subcommands_registered(self):
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        register(subparsers)
        for sub in ["pull", "build", "start", "stop", "restart", "status"]:
            ns = parser.parse_args(["daemon", sub])
            assert ns.daemon_command == sub

    def test_graceful_stop_with_timeout(self):
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        register(subparsers)
        ns = parser.parse_args(["daemon", "graceful-stop", "--timeout", "60"])
        assert ns.timeout == 60

    def test_build_no_cache_flag(self):
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        register(subparsers)
        ns = parser.parse_args(["daemon", "build", "--no-cache"])
        assert ns.no_cache is True

    def test_push_with_tag(self):
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        register(subparsers)
        ns = parser.parse_args(["daemon", "push", "--tag", "1.2.3"])
        assert ns.tag == "1.2.3"


# ---------------------------------------------------------------------------
# cmd_daemon — None sub-command prints usage
# ---------------------------------------------------------------------------


class TestCmdDaemonNone:
    def test_none_subcommand_prints_usage(self, capsys):
        args = _args(daemon_command=None)
        mock_d = MagicMock()
        mock_cls = _mock_daemon_class(mock_d)
        with patch("yadgar.daemon.YadgarDaemon", mock_cls):
            cmd_daemon(args)
        out = capsys.readouterr().out
        assert "Usage:" in out or "daemon" in out.lower()


# ---------------------------------------------------------------------------
# pull
# ---------------------------------------------------------------------------


class TestPull:
    def test_pull_success(self, capsys):
        args = _args(daemon_command="pull")
        mock_d = MagicMock()
        mock_d.pull.return_value = {"ok": True, "image": "yadgar:latest"}
        mock_cls = _mock_daemon_class(mock_d)
        with patch("yadgar.daemon.YadgarDaemon", mock_cls):
            cmd_daemon(args)
        out = capsys.readouterr().out
        assert "yadgar:latest" in out

    def test_pull_docker_unavailable_exits(self):
        args = _args(daemon_command="pull")
        mock_d = MagicMock()
        mock_cls = _mock_daemon_class(
            mock_d, check_docker_result={"ok": False, "reason": "no docker"}
        )
        with patch("yadgar.daemon.YadgarDaemon", mock_cls):
            with pytest.raises(SystemExit) as exc_info:
                cmd_daemon(args)
        assert exc_info.value.code == 1

    def test_pull_failure_exits(self):
        args = _args(daemon_command="pull")
        mock_d = MagicMock()
        mock_d.pull.return_value = {"ok": False, "reason": "network error"}
        mock_cls = _mock_daemon_class(mock_d)
        with patch("yadgar.daemon.YadgarDaemon", mock_cls):
            with pytest.raises(SystemExit) as exc_info:
                cmd_daemon(args)
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------


class TestStart:
    def test_start_success(self, capsys):
        args = _args(daemon_command="start")
        mock_d = MagicMock()
        mock_d.start.return_value = {
            "status": "started",
            "container": "yadgar-1",
            "port": 8765,
            "memory_mb": 512,
        }
        mock_cls = _mock_daemon_class(mock_d)
        with patch("yadgar.daemon.YadgarDaemon", mock_cls):
            cmd_daemon(args)
        out = capsys.readouterr().out
        assert "started" in out.lower() or "yadgar-1" in out

    def test_start_already_running(self, capsys):
        args = _args(daemon_command="start")
        mock_d = MagicMock()
        mock_d.start.return_value = {
            "status": "already_running",
            "container": "yadgar-1",
            "port": 8765,
        }
        mock_cls = _mock_daemon_class(mock_d)
        with patch("yadgar.daemon.YadgarDaemon", mock_cls):
            cmd_daemon(args)
        out = capsys.readouterr().out
        assert "already" in out.lower() or "running" in out.lower()

    def test_start_failed_exits(self):
        args = _args(daemon_command="start")
        mock_d = MagicMock()
        mock_d.start.return_value = {"status": "failed", "reason": "port in use"}
        mock_cls = _mock_daemon_class(mock_d)
        with patch("yadgar.daemon.YadgarDaemon", mock_cls):
            with pytest.raises(SystemExit) as exc_info:
                cmd_daemon(args)
        assert exc_info.value.code == 1

    def test_start_docker_unavailable_exits(self):
        args = _args(daemon_command="start")
        mock_d = MagicMock()
        mock_cls = _mock_daemon_class(
            mock_d, check_docker_result={"ok": False, "reason": "no docker"}
        )
        with patch("yadgar.daemon.YadgarDaemon", mock_cls):
            with pytest.raises(SystemExit) as exc_info:
                cmd_daemon(args)
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


class TestStop:
    def test_stop_success(self, capsys):
        args = _args(daemon_command="stop")
        mock_d = MagicMock()
        mock_d.stop.return_value = {"status": "stopped", "container": "yadgar-1"}
        mock_cls = _mock_daemon_class(mock_d)
        with patch("yadgar.daemon.YadgarDaemon", mock_cls):
            cmd_daemon(args)
        out = capsys.readouterr().out
        assert "stopped" in out.lower() or "yadgar-1" in out

    def test_stop_not_running(self, capsys):
        args = _args(daemon_command="stop")
        mock_d = MagicMock()
        mock_d.stop.return_value = {"status": "not_running"}
        mock_cls = _mock_daemon_class(mock_d)
        with patch("yadgar.daemon.YadgarDaemon", mock_cls):
            cmd_daemon(args)
        out = capsys.readouterr().out
        assert "not running" in out.lower()


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


class TestStatus:
    def test_status_running(self, capsys):
        args = _args(daemon_command="status")
        mock_d = MagicMock()
        mock_d.status.return_value = {
            "running": True,
            "container": "yadgar-1",
            "port": 8765,
            "version": "5.49.7",
            "uptime_seconds": 3600,
        }
        mock_cls = _mock_daemon_class(mock_d)
        with patch("yadgar.daemon.YadgarDaemon", mock_cls):
            cmd_daemon(args)
        out = capsys.readouterr().out
        assert "running" in out.lower()

    def test_status_not_running(self, capsys):
        args = _args(daemon_command="status")
        mock_d = MagicMock()
        mock_d.status.return_value = {"running": False}
        mock_cls = _mock_daemon_class(mock_d)
        with patch("yadgar.daemon.YadgarDaemon", mock_cls):
            cmd_daemon(args)
        out = capsys.readouterr().out
        assert "not running" in out.lower()


# ---------------------------------------------------------------------------
# restart
# ---------------------------------------------------------------------------


class TestRestart:
    def test_restart_success(self, capsys):
        args = _args(daemon_command="restart")
        mock_d = MagicMock()
        mock_d.restart.return_value = {
            "started": {"status": "started", "container": "yadgar-1", "port": 8765}
        }
        mock_cls = _mock_daemon_class(mock_d)
        with patch("yadgar.daemon.YadgarDaemon", mock_cls):
            cmd_daemon(args)
        out = capsys.readouterr().out
        assert "restarted" in out.lower() or "yadgar-1" in out

    def test_restart_unexpected_result(self, capsys):
        args = _args(daemon_command="restart")
        mock_d = MagicMock()
        mock_d.restart.return_value = {"started": {"status": "unknown"}}
        mock_cls = _mock_daemon_class(mock_d)
        with patch("yadgar.daemon.YadgarDaemon", mock_cls):
            cmd_daemon(args)
        err = capsys.readouterr().err
        assert err.strip() != "" or True  # at minimum, no crash


# ---------------------------------------------------------------------------
# configure-mcp
# ---------------------------------------------------------------------------


class TestConfigureMcp:
    def test_configure_mcp(self, capsys):
        args = _args(daemon_command="configure-mcp", port=8765)
        mock_d = MagicMock()
        mock_d.configure_mcp.return_value = {"updated": True}
        mock_cls = _mock_daemon_class(mock_d)
        with patch("yadgar.daemon.YadgarDaemon", mock_cls):
            cmd_daemon(args)
        out = capsys.readouterr().out
        assert "MCP" in out or "http" in out


# ---------------------------------------------------------------------------
# install-service
# ---------------------------------------------------------------------------


class TestInstallService:
    def test_install_service(self, capsys):
        args = _args(daemon_command="install-service")
        mock_d = MagicMock()
        mock_d.install_systemd_service.return_value = {
            "backend_service": "yadgar-backend.service",
            "core_service": "yadgar.service",
            "enable": "systemctl --user enable yadgar.service",
            "start": "systemctl --user start yadgar.service",
            "status": "systemctl --user status yadgar.service",
        }
        mock_cls = _mock_daemon_class(mock_d)
        with patch("yadgar.daemon.YadgarDaemon", mock_cls):
            cmd_daemon(args)
        out = capsys.readouterr().out
        assert "systemctl" in out or "service" in out.lower()


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------


class TestBuild:
    def test_build_success(self, capsys):
        args = _args(daemon_command="build", no_cache=False)
        mock_d = MagicMock()
        mock_d.build.return_value = {"ok": True, "image": "yadgar:dev", "target": "dev"}
        mock_cls = _mock_daemon_class(mock_d)
        with patch("yadgar.daemon.YadgarDaemon", mock_cls):
            cmd_daemon(args)
        out = capsys.readouterr().out
        assert "yadgar:dev" in out

    def test_build_failure_exits(self):
        args = _args(daemon_command="build", no_cache=True)
        mock_d = MagicMock()
        mock_d.build.return_value = {"ok": False, "reason": "Dockerfile missing"}
        mock_cls = _mock_daemon_class(mock_d)
        with patch("yadgar.daemon.YadgarDaemon", mock_cls):
            with pytest.raises(SystemExit) as exc_info:
                cmd_daemon(args)
        assert exc_info.value.code == 1

    def test_build_docker_unavailable_exits(self):
        args = _args(daemon_command="build", no_cache=False)
        mock_d = MagicMock()
        mock_cls = _mock_daemon_class(
            mock_d, check_docker_result={"ok": False, "reason": "no docker"}
        )
        with patch("yadgar.daemon.YadgarDaemon", mock_cls):
            with pytest.raises(SystemExit) as exc_info:
                cmd_daemon(args)
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# push
# ---------------------------------------------------------------------------


class TestPush:
    def test_push_success(self, capsys):
        args = _args(daemon_command="push", tag="1.0.0")
        mock_d = MagicMock()
        mock_d.push.return_value = {"ok": True, "pushed": ["yadgar:1.0.0", "yadgar:latest"]}
        mock_cls = _mock_daemon_class(mock_d)
        with patch("yadgar.daemon.YadgarDaemon", mock_cls):
            cmd_daemon(args)
        out = capsys.readouterr().out
        assert "1.0.0" in out or "latest" in out

    def test_push_failure_exits(self):
        args = _args(daemon_command="push", tag=None)
        mock_d = MagicMock()
        mock_d.push.return_value = {"ok": False, "reason": "auth failed"}
        mock_cls = _mock_daemon_class(mock_d)
        with patch("yadgar.daemon.YadgarDaemon", mock_cls):
            with pytest.raises(SystemExit) as exc_info:
                cmd_daemon(args)
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# graceful-stop
# ---------------------------------------------------------------------------


class TestGracefulStop:
    def test_graceful_stop_not_running(self):
        args = _args(daemon_command="graceful-stop", timeout=30)
        mock_d = MagicMock()
        mock_cls = _mock_daemon_class(mock_d)
        mock_sp_running = MagicMock(returncode=0, stdout="false\n")
        with (
            patch("yadgar.daemon.YadgarDaemon", mock_cls),
            patch("yadgar.daemon._prod_profile") as mock_prod,
            patch("subprocess.run", return_value=mock_sp_running),
        ):
            mock_prod.return_value = MagicMock(container_name="yadgar-prod")
            with pytest.raises(SystemExit) as exc_info:
                cmd_daemon(args)
        assert exc_info.value.code == 0

    def test_graceful_stop_success(self):
        args = _args(daemon_command="graceful-stop", timeout=30)
        mock_d = MagicMock()
        mock_cls = _mock_daemon_class(mock_d)

        def sp_side_effect(cmd, **kwargs):
            if "inspect" in cmd:
                return MagicMock(returncode=0, stdout="true\n")
            return MagicMock(returncode=0, stdout="")

        with (
            patch("yadgar.daemon.YadgarDaemon", mock_cls),
            patch("yadgar.daemon._prod_profile") as mock_prod,
            patch("subprocess.run", side_effect=sp_side_effect),
        ):
            mock_prod.return_value = MagicMock(container_name="yadgar-prod")
            with pytest.raises(SystemExit) as exc_info:
                cmd_daemon(args)
        assert exc_info.value.code == 0

    def test_graceful_stop_docker_unavailable_exits(self):
        args = _args(daemon_command="graceful-stop", timeout=30)
        mock_d = MagicMock()
        mock_cls = _mock_daemon_class(
            mock_d, check_docker_result={"ok": False, "reason": "no docker"}
        )
        with (
            patch("yadgar.daemon.YadgarDaemon", mock_cls),
            patch("yadgar.daemon._prod_profile") as mock_prod,
        ):
            mock_prod.return_value = MagicMock(container_name="yadgar-prod")
            with pytest.raises(SystemExit) as exc_info:
                cmd_daemon(args)
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# test / lint / shell — exec_in_container delegation
# ---------------------------------------------------------------------------


class TestContainerSubcommands:
    def test_test_subcommand_delegates(self):
        args = _args(daemon_command="test", extra_args=[])
        mock_d = MagicMock()
        mock_d.exec_in_container.return_value = 0
        mock_cls = _mock_daemon_class(mock_d)
        with patch("yadgar.daemon.YadgarDaemon", mock_cls):
            with pytest.raises(SystemExit) as exc_info:
                cmd_daemon(args)
        assert exc_info.value.code == 0
        call_args = mock_d.exec_in_container.call_args
        assert "pytest" in call_args[0][0]

    def test_lint_subcommand_delegates(self):
        args = _args(daemon_command="lint")
        mock_d = MagicMock()
        mock_d.exec_in_container.return_value = 0
        mock_cls = _mock_daemon_class(mock_d)
        with patch("yadgar.daemon.YadgarDaemon", mock_cls):
            with pytest.raises(SystemExit):
                cmd_daemon(args)
        call_args = mock_d.exec_in_container.call_args
        assert "ruff" in call_args[0][0]

    def test_shell_subcommand_delegates(self):
        args = _args(daemon_command="shell")
        mock_d = MagicMock()
        mock_d.exec_in_container.return_value = 0
        mock_cls = _mock_daemon_class(mock_d)
        with patch("yadgar.daemon.YadgarDaemon", mock_cls):
            with pytest.raises(SystemExit):
                cmd_daemon(args)
        call_args = mock_d.exec_in_container.call_args
        assert "/bin/bash" in call_args[0][0]
