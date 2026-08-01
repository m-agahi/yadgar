"""Tests for yadgar/cli/version.py — version probe and summary.

Wave 3 coverage: yadgar/cli/version.py (49 stmts, 0% pre-wave).
Strategy: mock urllib.request.urlopen and yadgar.paths for _read_auth_token.
Test _probe_daemon, _read_auth_token, print_version_summary in text + json mode.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from yadgar.core.cli.version import _probe_daemon, _read_auth_token, print_version_summary

# ---------------------------------------------------------------------------
# _read_auth_token
# ---------------------------------------------------------------------------


class TestReadAuthToken:
    def test_reads_from_env(self, monkeypatch):
        monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", "env_token")
        assert _read_auth_token() == "env_token"

    def test_reads_from_secrets_file(self, tmp_path, monkeypatch):
        monkeypatch.delenv("YADGAR_MCP_AUTH_TOKEN", raising=False)
        secrets = tmp_path / "secrets.env"
        secrets.write_text("YADGAR_MCP_AUTH_TOKEN=file_token\n")
        import yadgar._shared.paths as _yp

        with patch.object(_yp, "SECRETS_ENV_PATH", secrets):
            result = _read_auth_token()
        assert result == "file_token"

    def test_returns_none_when_no_env_no_file(self, tmp_path, monkeypatch):
        monkeypatch.delenv("YADGAR_MCP_AUTH_TOKEN", raising=False)
        missing = tmp_path / "missing.env"
        import yadgar._shared.paths as _yp

        with patch.object(_yp, "SECRETS_ENV_PATH", missing):
            result = _read_auth_token()
        assert result is None

    def test_returns_none_on_secrets_read_error(self, tmp_path, monkeypatch):
        monkeypatch.delenv("YADGAR_MCP_AUTH_TOKEN", raising=False)
        secrets = tmp_path / "secrets.env"
        secrets.write_text("YADGAR_MCP_AUTH_TOKEN=tok\n")
        import yadgar._shared.paths as _yp

        with patch.object(_yp, "SECRETS_ENV_PATH", secrets):
            with patch("pathlib.Path.read_text", side_effect=OSError("permission denied")):
                result = _read_auth_token()
        assert result is None

    def test_strips_quotes_from_token(self, tmp_path, monkeypatch):
        monkeypatch.delenv("YADGAR_MCP_AUTH_TOKEN", raising=False)
        secrets = tmp_path / "secrets.env"
        secrets.write_text('YADGAR_MCP_AUTH_TOKEN="quoted_token"\n')
        import yadgar._shared.paths as _yp

        with patch.object(_yp, "SECRETS_ENV_PATH", secrets):
            result = _read_auth_token()
        assert result == "quoted_token"


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
            patch("yadgar.core.cli.version._read_auth_token", return_value=None),
            patch("yadgar.core.cli.version.urllib.request.urlopen", return_value=resp),
        ):
            result = _probe_daemon()
        assert result["running"] is True
        assert result["version"] == "5.49.8"

    def test_returns_running_false_on_connection_error(self):
        with (
            patch("yadgar.core.cli.version._read_auth_token", return_value=None),
            patch("yadgar.core.cli.version.urllib.request.urlopen", side_effect=OSError("refused")),
        ):
            result = _probe_daemon()
        assert result["running"] is False

    def test_returns_running_false_on_timeout(self):
        import urllib.error

        with (
            patch("yadgar.core.cli.version._read_auth_token", return_value=None),
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
            patch("yadgar.core.cli.version._read_auth_token", return_value="my_token"),
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
            patch("yadgar.core.cli.version._read_auth_token", return_value=None),
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
            patch("yadgar.core.cli.version._read_auth_token", return_value=None),
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
