"""Tests for yadgar/__main__.py — v5.49.9 wave 4 coverage.

Module: yadgar.__main__
Target: ≥80% line coverage

Strategy:
- Drive cli() by patching sys.argv and mocking heavy imports.
- --version path: mock print_version_summary, expect SystemExit(0).
- Default (no subcommand): mock yadgar.server.main to prevent real server start.
- Subcommand dispatch: mock args.func to test the else branch.
- Banner printing: test non-quiet + non-stdio transport triggers banner.
- cmd_vacuum re-export: trivial delegation test.
- STARTUP_BANNER: verify it contains expected strings.

Floor: The `if __name__ == "__main__": cli()` guard (lines 160-161) is
       untestable without subprocess execution of the module as a script.
       Lines covered by `# pragma: no cover` in source are excluded per spec.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _restore_yadgar_env():
    """cli() mutates os.environ directly (YADGAR_PORT/HOST/DB_PATH/TRANSPORT,
    and — Car 5 item 4 — YADGAR_EMBED_URL) — it does NOT go through
    monkeypatch, so those writes leak across xdist worker tests (e.g. --port
    9876 leaked into yadgar.hooks.subagent_stop's _PORT). Snapshot + restore
    the cli()-touched env vars around every test in this module."""
    _keys = ("YADGAR_PORT", "YADGAR_HOST", "YADGAR_DB_PATH", "YADGAR_TRANSPORT", "YADGAR_EMBED_URL")
    _saved = {k: os.environ.get(k) for k in _keys}
    yield
    for k, v in _saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# ---------------------------------------------------------------------------
# STARTUP_BANNER content
# ---------------------------------------------------------------------------


class TestStartupBanner:
    def test_banner_contains_version(self):
        from yadgar import __version__
        from yadgar.__main__ import STARTUP_BANNER

        assert __version__ in STARTUP_BANNER

    def test_banner_contains_module_names(self):
        from yadgar.__main__ import STARTUP_BANNER

        assert "StorageEngine" in STARTUP_BANNER
        assert "Retriever" in STARTUP_BANNER

    def test_banner_contains_core_tools(self):
        from yadgar.__main__ import STARTUP_BANNER

        assert "memorize" in STARTUP_BANNER
        assert "recall" in STARTUP_BANNER


# ---------------------------------------------------------------------------
# VALID_TRANSPORTS
# ---------------------------------------------------------------------------


class TestValidTransports:
    def test_valid_transports_does_not_contain_stdio(self):
        """Phase 2b: stdio removed from VALID_TRANSPORTS; streamable-http is the default."""
        from yadgar.__main__ import VALID_TRANSPORTS

        assert "stdio" not in VALID_TRANSPORTS

    def test_valid_transports_contains_sse(self):
        from yadgar.__main__ import VALID_TRANSPORTS

        assert "sse" in VALID_TRANSPORTS

    def test_valid_transports_contains_streamable_http(self):
        from yadgar.__main__ import VALID_TRANSPORTS

        assert "streamable-http" in VALID_TRANSPORTS


# ---------------------------------------------------------------------------
# cli() — --version flag
# ---------------------------------------------------------------------------


class TestCliVersion:
    def test_version_flag_exits_zero(self, monkeypatch):
        """--version calls print_version_summary and exits 0."""
        monkeypatch.setattr(sys, "argv", ["yadgar", "--version"])

        mock_print_version = MagicMock()

        with patch("yadgar.core.cli.version.print_version_summary", mock_print_version):
            with patch("yadgar.core.server.main"):
                import importlib

                import yadgar.__main__ as main_mod

                importlib.reload(main_mod)
                try:
                    main_mod.cli()
                except SystemExit as e:
                    assert e.code == 0

        mock_print_version.assert_called_once()

    def test_version_json_flag_passes_json_mode(self, monkeypatch):
        """--version --json passes json_mode=True to print_version_summary."""
        monkeypatch.setattr(sys, "argv", ["yadgar", "--version", "--json"])

        mock_print_version = MagicMock()

        with patch("yadgar.core.cli.version.print_version_summary", mock_print_version):
            import importlib

            import yadgar.__main__ as main_mod

            importlib.reload(main_mod)
            try:
                main_mod.cli()
            except SystemExit:
                pass

        mock_print_version.assert_called_once_with(json_mode=True)


# ---------------------------------------------------------------------------
# cli() — default server mode (no subcommand)
# ---------------------------------------------------------------------------


class TestCliDefaultServer:
    def test_default_mode_calls_server_main(self, monkeypatch):
        """With no subcommand, cli() calls yadgar.server.main."""
        monkeypatch.setattr(sys, "argv", ["yadgar"])

        mock_server_main = MagicMock()
        mock_settings = MagicMock()
        mock_settings.CORE_LOG_LEVEL = None
        mock_settings.LOG_FORMAT = None

        with patch("yadgar.core.server.main", mock_server_main):
            with patch("yadgar._shared.config.get_settings", return_value=mock_settings):
                import importlib

                import yadgar.__main__ as main_mod

                importlib.reload(main_mod)
                main_mod.cli()

        mock_server_main.assert_called_once()

    def test_default_mode_passes_transport(self, monkeypatch):
        """cli() passes transport arg to server.main."""
        monkeypatch.setattr(sys, "argv", ["yadgar", "--transport", "sse"])

        mock_server_main = MagicMock()
        mock_settings = MagicMock()
        mock_settings.CORE_LOG_LEVEL = None
        mock_settings.LOG_FORMAT = None

        with patch("yadgar.core.server.main", mock_server_main):
            with patch("yadgar._shared.config.get_settings", return_value=mock_settings):
                import importlib

                import yadgar.__main__ as main_mod

                importlib.reload(main_mod)
                main_mod.cli()

        call_kwargs = mock_server_main.call_args
        assert call_kwargs.kwargs.get("transport") == "sse" or (
            len(call_kwargs.args) >= 3 and call_kwargs.args[2] == "sse"
        )

    def test_host_arg_sets_env_var(self, monkeypatch):
        """--host sets YADGAR_HOST env var."""
        monkeypatch.setattr(sys, "argv", ["yadgar", "--host", "0.0.0.0"])

        mock_server_main = MagicMock()
        mock_settings = MagicMock()
        mock_settings.CORE_LOG_LEVEL = None
        mock_settings.LOG_FORMAT = None

        with patch("yadgar.core.server.main", mock_server_main):
            with patch("yadgar._shared.config.get_settings", return_value=mock_settings):
                import importlib
                import os

                import yadgar.__main__ as main_mod

                importlib.reload(main_mod)
                main_mod.cli()

        assert os.environ.get("YADGAR_HOST") == "0.0.0.0"

    def test_port_arg_sets_env_var(self, monkeypatch):
        """--port sets YADGAR_PORT env var."""
        monkeypatch.setattr(sys, "argv", ["yadgar", "--port", "9999"])

        mock_server_main = MagicMock()
        mock_settings = MagicMock()
        mock_settings.CORE_LOG_LEVEL = None
        mock_settings.LOG_FORMAT = None

        with patch("yadgar.core.server.main", mock_server_main):
            with patch("yadgar._shared.config.get_settings", return_value=mock_settings):
                import importlib
                import os

                import yadgar.__main__ as main_mod

                importlib.reload(main_mod)
                main_mod.cli()

        assert os.environ.get("YADGAR_PORT") == "9999"

    def test_banner_printed_for_non_stdio_non_quiet(self, monkeypatch, capsys):
        """Banner printed to stderr when transport != stdio and --quiet not set."""
        monkeypatch.setattr(sys, "argv", ["yadgar", "--transport", "sse"])

        mock_server_main = MagicMock()
        mock_settings = MagicMock()
        mock_settings.CORE_LOG_LEVEL = None
        mock_settings.LOG_FORMAT = None

        with patch("yadgar.core.server.main", mock_server_main):
            with patch("yadgar._shared.config.get_settings", return_value=mock_settings):
                import importlib

                import yadgar.__main__ as main_mod

                importlib.reload(main_mod)
                main_mod.cli()

        err = capsys.readouterr().err
        assert "Yadgar" in err or "Transport" in err

    def test_default_transport_prints_banner(self, monkeypatch, capsys):
        """Phase 2b: default is streamable-http; bare 'yadgar' now prints the banner."""
        monkeypatch.setattr(sys, "argv", ["yadgar"])

        mock_server_main = MagicMock()
        mock_settings = MagicMock()
        mock_settings.CORE_LOG_LEVEL = None
        mock_settings.LOG_FORMAT = None

        with patch("yadgar.core.server.main", mock_server_main):
            with patch("yadgar._shared.config.get_settings", return_value=mock_settings):
                import importlib

                import yadgar.__main__ as main_mod

                importlib.reload(main_mod)
                main_mod.cli()

        err = capsys.readouterr().err
        assert "=== Yadgar" in err or "Transport" in err

    def test_no_banner_when_quiet(self, monkeypatch, capsys):
        """--quiet suppresses banner even for non-stdio transport."""
        monkeypatch.setattr(sys, "argv", ["yadgar", "--transport", "sse", "--quiet"])

        mock_server_main = MagicMock()
        mock_settings = MagicMock()
        mock_settings.CORE_LOG_LEVEL = None
        mock_settings.LOG_FORMAT = None

        with patch("yadgar.core.server.main", mock_server_main):
            with patch("yadgar._shared.config.get_settings", return_value=mock_settings):
                import importlib

                import yadgar.__main__ as main_mod

                importlib.reload(main_mod)
                main_mod.cli()

        err = capsys.readouterr().err
        assert "=== Yadgar" not in err

    def test_banner_includes_host_port_db_path(self, monkeypatch, capsys):
        """Banner prints host, port, and db_path when all are given with non-stdio transport."""
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "yadgar",
                "--transport",
                "sse",
                "--host",
                "1.2.3.4",
                "--port",
                "9876",
                "--db-path",
                "/tmp/db",
            ],
        )

        mock_server_main = MagicMock()
        mock_settings = MagicMock()
        mock_settings.CORE_LOG_LEVEL = None
        mock_settings.LOG_FORMAT = None

        with patch("yadgar.core.server.main", mock_server_main):
            with patch("yadgar._shared.config.get_settings", return_value=mock_settings):
                import importlib

                import yadgar.__main__ as main_mod

                importlib.reload(main_mod)
                main_mod.cli()

        err = capsys.readouterr().err
        assert "1.2.3.4" in err
        assert "9876" in err
        assert "/tmp/db" in err


# ---------------------------------------------------------------------------
# cli() — logging configuration paths
# ---------------------------------------------------------------------------


class TestCliLogging:
    def test_log_level_triggers_configure_logging(self, monkeypatch):
        """Non-WARN log level triggers configure_logging call."""
        monkeypatch.setattr(sys, "argv", ["yadgar"])

        mock_server_main = MagicMock()
        mock_settings = MagicMock()
        mock_settings.CORE_LOG_LEVEL = "DEBUG"
        mock_settings.LOG_FORMAT = "text"
        mock_configure = MagicMock()

        with patch("yadgar.core.server.main", mock_server_main):
            with patch("yadgar._shared.config.get_settings", return_value=mock_settings):
                with patch(
                    "yadgar._shared.observability.log_config.configure_logging", mock_configure
                ):
                    import importlib

                    import yadgar.__main__ as main_mod

                    importlib.reload(main_mod)
                    main_mod.cli()

        mock_configure.assert_called_once()

    def test_json_log_format_triggers_configure_logging(self, monkeypatch):
        """LOG_FORMAT=json with no level triggers configure_logging with json."""
        monkeypatch.setattr(sys, "argv", ["yadgar"])

        mock_server_main = MagicMock()
        mock_settings = MagicMock()
        mock_settings.CORE_LOG_LEVEL = None
        mock_settings.LOG_FORMAT = "json"
        mock_configure = MagicMock()

        with patch("yadgar.core.server.main", mock_server_main):
            with patch("yadgar._shared.config.get_settings", return_value=mock_settings):
                with patch(
                    "yadgar._shared.observability.log_config.configure_logging", mock_configure
                ):
                    import importlib

                    import yadgar.__main__ as main_mod

                    importlib.reload(main_mod)
                    main_mod.cli()

        mock_configure.assert_called_once()
        call_kwargs = mock_configure.call_args.kwargs
        assert call_kwargs.get("log_format") == "json"


# ---------------------------------------------------------------------------
# cli() — subcommand dispatch
# ---------------------------------------------------------------------------


class TestCliSubcommandDispatch:
    def test_subcommand_calls_func(self, monkeypatch):
        """When a subcommand is set, cli() calls args.func(args)."""
        monkeypatch.setattr(sys, "argv", ["yadgar", "stats"])

        mock_func = MagicMock()

        # Patch the stats.register to inject our mock func
        def _fake_register(subparsers):
            p = subparsers.add_parser("stats")
            p.set_defaults(func=mock_func)

        with patch("yadgar.core.cli.stats.register", side_effect=_fake_register):
            # Need other cli modules to register without error
            import importlib

            import yadgar.__main__ as main_mod

            importlib.reload(main_mod)
            main_mod.cli()

        mock_func.assert_called_once()

    def test_subcommand_defaults_yadgar_embed_url_when_unset(self, monkeypatch):
        """Car 5 item 4: on a bare host CLI, YADGAR_EMBED_URL is never set by
        anything — it only exists inside containers (e.g.
        `http://yadgar-backend:8001`, wired by daemon.py/systemd units).
        Every host CLI subcommand that forwards an admin op (`seed`,
        `drain`, `restore`, `stats`'s /read_query debug path, ...) needs it
        — without a default, `yadgar seed <dir>` used to die with
        "YADGAR_EMBED_URL is not set". cli() must default it to the
        published host port (docker-compose maps the backend's embed/admin
        service to 127.0.0.1:8001 — see docker-compose.yml) before running
        any subcommand, WITHOUT hardcoding a container hostname."""
        monkeypatch.delenv("YADGAR_EMBED_URL", raising=False)
        monkeypatch.setattr(sys, "argv", ["yadgar", "stats"])

        mock_func = MagicMock()

        def _fake_register(subparsers):
            p = subparsers.add_parser("stats")
            p.set_defaults(func=mock_func)

        with patch("yadgar.core.cli.stats.register", side_effect=_fake_register):
            import importlib

            import yadgar.__main__ as main_mod

            importlib.reload(main_mod)
            main_mod.cli()

        assert os.environ.get("YADGAR_EMBED_URL") == "http://127.0.0.1:8001"

    def test_subcommand_preserves_explicit_yadgar_embed_url(self, monkeypatch):
        """An explicitly-configured YADGAR_EMBED_URL (e.g. the in-container
        `http://yadgar-backend:8001` form, or an operator override) must NOT
        be clobbered by the host-CLI fallback default."""
        monkeypatch.setenv("YADGAR_EMBED_URL", "http://yadgar-backend:8001")
        monkeypatch.setattr(sys, "argv", ["yadgar", "stats"])

        mock_func = MagicMock()

        def _fake_register(subparsers):
            p = subparsers.add_parser("stats")
            p.set_defaults(func=mock_func)

        with patch("yadgar.core.cli.stats.register", side_effect=_fake_register):
            import importlib

            import yadgar.__main__ as main_mod

            importlib.reload(main_mod)
            main_mod.cli()

        assert os.environ.get("YADGAR_EMBED_URL") == "http://yadgar-backend:8001"


# ---------------------------------------------------------------------------
# cmd_vacuum re-export
# ---------------------------------------------------------------------------


class TestCmdVacuumReexport:
    def test_cmd_vacuum_delegates(self):
        """cmd_vacuum re-export delegates to yadgar.cli.vacuum.cmd_vacuum."""

        mock_vacuum = MagicMock(return_value=None)

        with patch("yadgar.core.cli.vacuum.cmd_vacuum", mock_vacuum):
            import importlib

            import yadgar.__main__ as main_mod

            importlib.reload(main_mod)
            args = SimpleNamespace()
            main_mod.cmd_vacuum(args)

        mock_vacuum.assert_called_once_with(args)


class TestSubcommandExitCodePropagates:
    """A handler's return value IS the process exit code.

    ``cli()`` called ``args.func(args)`` and discarded the result, so a
    subcommand that returned non-zero still exited 0. Observed on the
    sandbox VM 2026-08-15: a handler printed its own ``FAILED`` line on
    stderr and exited 0 — a CI step or operator script reads that as
    success.

    Driven end-to-end through the real parser (``cli()`` builds it
    inline, so there is no seam to patch) using a subcommand whose
    non-zero path needs no backend: ``snapshot restore`` against a
    missing snapshot returns 2 without attempting a single write.
    """

    def test_nonzero_handler_return_becomes_nonzero_exit(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        import pytest as _pytest

        import yadgar.__main__ as main_mod

        missing = tmp_path / "no-such-snapshot.surql"
        monkeypatch.setattr(
            "sys.argv",
            ["yadgar", "snapshot", "restore", "--snapshot", str(missing)],
        )
        with _pytest.raises(SystemExit) as caught:
            main_mod.cli()
        assert caught.value.code == 2
        # argparse ALSO exits 2 on a malformed command line, so the code alone
        # cannot tell "the handler returned 2" from "the parser rejected the
        # args" — which is the whole mechanism under test. The handler's own
        # stderr line is the discriminator.
        assert "snapshot restore: snapshot does not exist" in capsys.readouterr().err

    def test_handler_returning_none_still_exits_zero(self, monkeypatch) -> None:
        """Most handlers return None — they must keep exiting 0."""
        import yadgar.__main__ as main_mod

        monkeypatch.setattr("sys.argv", ["yadgar", "pending-findings"])
        # ``pending-findings`` calls sys.exit(0) itself on an empty
        # transcript; either way the process status must be 0.
        try:
            main_mod.cli()
        except SystemExit as exit_:
            assert exit_.code in (0, None)

    def test_non_int_handler_return_does_not_exit(self, monkeypatch) -> None:
        """A handler returning a non-status object must not become a failure.

        The dispatch tests double ``func`` with a ``MagicMock``, whose call
        result is truthy — treating that as an exit code would fail every
        such command.
        """
        import argparse
        from unittest.mock import MagicMock

        import yadgar.__main__ as main_mod

        sentinel = MagicMock()
        parsed = argparse.Namespace(
            command="stats",
            func=lambda _a: sentinel,
            quiet=True,
            version=False,
            json=False,
        )
        with patch.object(argparse.ArgumentParser, "parse_args", return_value=parsed):
            main_mod.cli()  # no SystemExit
