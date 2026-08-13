"""Tests for yadgar/cli/version.py — version probe and summary.

Wave 3 coverage: yadgar/cli/version.py (49 stmts, 0% pre-wave).
Strategy: mock urllib.request.urlopen and yadgar.core.cli.version.resolve_auth_token.
Test _probe_daemon, print_version_summary in text + json mode.

Car 9 (bug train): version.py's own hand-rolled ``_read_auth_token`` (env,
else secrets.env) was the "fourth hand-rolled copy" auth_token.py's docstring
forbids — replaced with a direct import of the ONE sanctioned resolver
(``yadgar.core.install.auth_token.resolve_auth_token``). The resolver's own
env/secrets-file/quote-stripping/missing-file coverage now lives exclusively
in ``yadgar/tests/core/test_auth_token_resolver.py`` — no longer duplicated
here, matching the precedent set for ``mcp_register`` and ``cli/seed.py``
(``TestNoDuplicateResolvers`` in that file).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from yadgar.core.cli.version import _probe_daemon, print_version_summary

# ---------------------------------------------------------------------------
# _probe_daemon
# ---------------------------------------------------------------------------


class TestProbeDaemon:
    def _mock_response(self, data: dict):
        resp = MagicMock()
        resp.read.return_value = json.dumps(data).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def test_returns_running_true_when_daemon_up(self):
        data = {"version": "5.49.8", "uptime_seconds": 3600, "db": "ok", "embed": "ok"}
        resp = self._mock_response(data)
        with (
            patch("yadgar.core.cli.version.resolve_auth_token", return_value=None),
            patch("yadgar.core.cli.version.urllib.request.urlopen", return_value=resp),
        ):
            result = _probe_daemon()
        assert result["running"] is True
        assert result["version"] == "5.49.8"

    def test_returns_running_false_on_connection_error(self):
        with (
            patch("yadgar.core.cli.version.resolve_auth_token", return_value=None),
            patch("yadgar.core.cli.version.urllib.request.urlopen", side_effect=OSError("refused")),
        ):
            result = _probe_daemon()
        assert result["running"] is False

    def test_returns_running_false_on_timeout(self):
        import urllib.error

        with (
            patch("yadgar.core.cli.version.resolve_auth_token", return_value=None),
            patch(
                "yadgar.core.cli.version.urllib.request.urlopen",
                side_effect=urllib.error.URLError("timed out"),
            ),
        ):
            result = _probe_daemon()
        assert result["running"] is False

    def test_auth_header_set_when_token_present(self):
        data = {"version": "5.0.0", "uptime_seconds": None, "db": True, "embed": True}
        resp = self._mock_response(data)
        with (
            patch("yadgar.core.cli.version.resolve_auth_token", return_value="my_token"),
            patch(
                "yadgar.core.cli.version.urllib.request.urlopen", return_value=resp
            ) as mock_open_url,
        ):
            _probe_daemon()
        req_obj = mock_open_url.call_args[0][0]
        assert req_obj.get_header("Authorization") == "Bearer my_token"

    def test_db_and_embed_ok_flags(self):
        data = {"version": "5.0", "db": True, "embed": "ok"}
        resp = self._mock_response(data)
        with (
            patch("yadgar.core.cli.version.resolve_auth_token", return_value=None),
            patch("yadgar.core.cli.version.urllib.request.urlopen", return_value=resp),
        ):
            result = _probe_daemon()
        assert result["db"] is True
        assert result["embed"] is True

    def test_http_error_is_closed(self):
        """HTTPError is a response object holding a file wrapper (a
        tempfile._TemporaryFileWrapper via addbase on py3.14); an unclosed
        instance fires a spurious ResourceWarning at GC that pytest-xdist
        mis-attributes to an unrelated test. _probe_daemon must close it
        deterministically (Car 0036)."""
        import urllib.error

        http_err = urllib.error.HTTPError(url="", code=500, msg="Error", hdrs={}, fp=None)
        with (
            patch("yadgar.core.cli.version.resolve_auth_token", return_value=None),
            patch("yadgar.core.cli.version.urllib.request.urlopen", side_effect=http_err),
        ):
            result = _probe_daemon()
        assert result["running"] is False
        assert http_err.fp is None or http_err.fp.closed, "the hook must close the caught HTTPError"


# ---------------------------------------------------------------------------
# print_version_summary
# ---------------------------------------------------------------------------


class TestPrintVersionSummary:
    def test_text_mode_daemon_running(self, capsys, monkeypatch):
        monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", "tok")
        daemon = {
            "running": True,
            "version": "5.49.8",
            "uptime_seconds": 100,
            "db": True,
            "embed": True,
        }
        with (
            patch("yadgar.core.cli.version._probe_daemon", return_value=daemon),
            patch("yadgar.__version__", "5.49.8"),
            patch("yadgar.BACKEND_VERSION", "5.0.3"),
        ):
            print_version_summary(json_mode=False)
        out = capsys.readouterr().out
        assert "5.49.8" in out
        assert "daemon" in out

    def test_text_mode_daemon_not_running(self, capsys):
        daemon = {"running": False}
        with (
            patch("yadgar.core.cli.version._probe_daemon", return_value=daemon),
            patch("yadgar.__version__", "5.49.8"),
            patch("yadgar.BACKEND_VERSION", "5.0.3"),
        ):
            print_version_summary(json_mode=False)
        out = capsys.readouterr().out
        assert "not running" in out

    def test_json_mode_outputs_valid_json(self, capsys):
        daemon = {"running": True, "version": "5.49.8"}
        with (
            patch("yadgar.core.cli.version._probe_daemon", return_value=daemon),
            patch("yadgar.__version__", "5.49.8"),
            patch("yadgar.BACKEND_VERSION", "5.0.3"),
        ):
            print_version_summary(json_mode=True)
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["core"] == "5.49.8"
        assert payload["backend"] == "5.0.3"
        assert payload["daemon"]["running"] is True

    def test_text_mode_no_uptime(self, capsys):
        daemon = {
            "running": True,
            "version": "5.49.8",
            "uptime_seconds": None,
            "db": False,
            "embed": False,
        }
        with (
            patch("yadgar.core.cli.version._probe_daemon", return_value=daemon),
            patch("yadgar.__version__", "5.49.8"),
            patch("yadgar.BACKEND_VERSION", "5.0.3"),
        ):
            print_version_summary(json_mode=False)
        out = capsys.readouterr().out
        assert "uptime" not in out
