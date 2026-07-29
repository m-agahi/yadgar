"""Tests for yadgar/cli/seed.py — seed CLI subcommand.

Wave 2 coverage: yadgar/cli/seed.py (142 stmts, 0% pre-wave).
Strategy: mock at urllib.request boundary (HTTP path) and yadgar.seed.seed_project.
Test _load_anchors_yaml, _read_auth_token, _daemon_health_ok, _seed_anchors, cmd_seed.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from yadgar.core.cli.seed import (
    _daemon_health_ok,
    _load_anchors_yaml,
    _read_auth_token,
    _seed_anchors,
    cmd_seed,
)

# ---------------------------------------------------------------------------
# _load_anchors_yaml
# ---------------------------------------------------------------------------


class TestLoadAnchorsYaml:
    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            _load_anchors_yaml(str(tmp_path / "missing.yaml"))

    def test_list_format(self, tmp_path):
        p = tmp_path / "anchors.yaml"
        p.write_text("- content: foo\n  tags: [a, b]\n- content: bar\n  tags: [c]\n")
        result = _load_anchors_yaml(str(p))
        assert len(result) == 2
        assert result[0]["content"] == "foo"

    def test_dict_with_anchors_key(self, tmp_path):
        p = tmp_path / "anchors.yaml"
        p.write_text("anchors:\n  - content: first\n    tags: [x]\n")
        result = _load_anchors_yaml(str(p))
        assert len(result) == 1
        assert result[0]["content"] == "first"

    def test_invalid_format_raises(self, tmp_path):
        p = tmp_path / "anchors.yaml"
        p.write_text("key: value\nno_anchors: true\n")
        with pytest.raises(ValueError):
            _load_anchors_yaml(str(p))

    def test_empty_list_format(self, tmp_path):
        p = tmp_path / "anchors.yaml"
        p.write_text("[]\n")
        result = _load_anchors_yaml(str(p))
        assert result == []


# ---------------------------------------------------------------------------
# _read_auth_token
# ---------------------------------------------------------------------------


class TestReadAuthToken:
    """2026-07-29: the body now delegates to the single ``core.install.auth_token``
    resolver (three hand-rolled copies collapsed to one). The ASSERTIONS below are
    unchanged; only the injection seam moved — these used to patch
    ``seed._paths``, a module attribute the shared resolver never reads, so they
    now use the sanctioned ``$YADGAR_SECRETS_ENV_FILE`` override that
    ``paths.SECRETS_ENV_PATH`` honors at access time (the same seam
    ``test_mcp_register.py``'s token tests already use).
    """

    def test_env_var_wins(self):
        with patch.dict(os.environ, {"YADGAR_MCP_AUTH_TOKEN": "env-token"}):
            assert _read_auth_token() == "env-token"

    def test_empty_env_falls_back_to_file(self, tmp_path):
        secrets = tmp_path / "secrets.env"
        secrets.write_text("YADGAR_MCP_AUTH_TOKEN=file-token\n")
        with patch.dict(
            os.environ,
            {"YADGAR_MCP_AUTH_TOKEN": "", "YADGAR_SECRETS_ENV_FILE": str(secrets)},
        ):
            result = _read_auth_token()
        assert result == "file-token"

    def test_no_env_no_file_returns_empty(self, tmp_path):
        with patch.dict(
            os.environ,
            {
                "YADGAR_MCP_AUTH_TOKEN": "",
                "YADGAR_SECRETS_ENV_FILE": str(tmp_path / "missing.env"),
            },
        ):
            result = _read_auth_token()
        assert result == ""

    def test_file_with_quoted_token(self, tmp_path):
        secrets = tmp_path / "s.env"
        secrets.write_text('YADGAR_MCP_AUTH_TOKEN="quoted-token"\n')
        with patch.dict(
            os.environ,
            {"YADGAR_MCP_AUTH_TOKEN": "", "YADGAR_SECRETS_ENV_FILE": str(secrets)},
        ):
            result = _read_auth_token()
        assert result == "quoted-token"

    def test_file_skips_comments(self, tmp_path):
        secrets = tmp_path / "s.env"
        secrets.write_text("# comment\nYADGAR_MCP_AUTH_TOKEN=tok\n")
        with patch.dict(
            os.environ,
            {"YADGAR_MCP_AUTH_TOKEN": "", "YADGAR_SECRETS_ENV_FILE": str(secrets)},
        ):
            result = _read_auth_token()
        assert result == "tok"


# ---------------------------------------------------------------------------
# _daemon_health_ok
# ---------------------------------------------------------------------------


class TestDaemonHealthOk:
    def test_returns_true_when_daemon_responds(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"{}"
        with patch("urllib.request.urlopen", return_value=mock_resp):
            assert _daemon_health_ok() is True

    def test_returns_false_on_exception(self):
        import urllib.error

        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            assert _daemon_health_ok() is False

    def test_returns_false_on_timeout(self):
        with patch("urllib.request.urlopen", side_effect=TimeoutError("timeout")):
            assert _daemon_health_ok() is False


# ---------------------------------------------------------------------------
# _seed_anchors
# ---------------------------------------------------------------------------


class TestSeedAnchors:
    def test_dry_run_returns_loaded_count(self, capsys):
        anchors = [
            {"content": "anchor one", "tags": ["tag1"]},
            {"content": "anchor two", "tags": ["tag2"]},
        ]
        result = _seed_anchors(anchors, db_path=None, dry_run=True)
        assert result["loaded"] == 2
        assert result["created"] == 2
        assert result["dry_run"] is True

    def test_daemon_unreachable_skips_all(self, capsys):
        anchors = [{"content": "x", "tags": ["a"]}]
        with patch("yadgar.core.cli.seed._daemon_health_ok", return_value=False):
            result = _seed_anchors(anchors, db_path=None, dry_run=False)
        assert result["skipped"] == 1
        assert result.get("reason") == "daemon_unreachable"

    def test_happy_path_created(self):
        anchors = [{"content": "test anchor", "tags": ["_anchor"]}]
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"created": 1}).encode()
        with (
            patch("yadgar.core.cli.seed._daemon_health_ok", return_value=True),
            patch("urllib.request.urlopen", return_value=mock_resp),
            patch.dict(os.environ, {"YADGAR_MCP_AUTH_TOKEN": ""}),
        ):
            result = _seed_anchors(anchors, db_path=None, dry_run=False)
        assert result["created"] == 1
        assert result["skipped"] == 0

    def test_409_counts_as_skipped(self):
        import urllib.error

        anchors = [{"content": "duplicate", "tags": []}]
        http_err = urllib.error.HTTPError(url="", code=409, msg="Conflict", hdrs={}, fp=None)
        with (
            patch("yadgar.core.cli.seed._daemon_health_ok", return_value=True),
            patch("urllib.request.urlopen", side_effect=http_err),
        ):
            result = _seed_anchors(anchors, db_path=None, dry_run=False)
        http_err.close()  # HTTPError is file-like; unclosed → ResourceWarning at GC
        assert result["skipped"] == 1
        assert result["created"] == 0

    def test_other_http_error_counts_as_skipped(self):
        import urllib.error

        anchors = [{"content": "x", "tags": []}]
        http_err = urllib.error.HTTPError(url="", code=500, msg="Error", hdrs={}, fp=None)
        with (
            patch("yadgar.core.cli.seed._daemon_health_ok", return_value=True),
            patch("urllib.request.urlopen", side_effect=http_err),
        ):
            result = _seed_anchors(anchors, db_path=None, dry_run=False)
        http_err.close()  # HTTPError is file-like; unclosed → ResourceWarning at GC
        assert result["skipped"] == 1

    def test_missing_content_skipped(self):
        anchors = [{"content": "", "tags": ["a"]}]
        with (
            patch("yadgar.core.cli.seed._daemon_health_ok", return_value=True),
        ):
            result = _seed_anchors(anchors, db_path=None, dry_run=False)
        assert result["skipped"] == 1

    def test_ensures_anchor_tag_added(self):
        """Verify _anchor tag is always appended if missing."""
        sent_payloads = []

        def capture_urlopen(req, timeout=None):
            body = req.data
            if body:
                sent_payloads.append(json.loads(body.decode()))
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps({"created": 1}).encode()
            return mock_resp

        anchors = [{"content": "no anchor tag", "tags": ["custom"]}]
        with (
            patch("yadgar.core.cli.seed._daemon_health_ok", return_value=True),
            patch("urllib.request.urlopen", side_effect=capture_urlopen),
        ):
            _seed_anchors(anchors, db_path=None, dry_run=False)

        assert len(sent_payloads) == 1
        assert "_anchor" in sent_payloads[0]["tags"]


# ---------------------------------------------------------------------------
# cmd_seed — anchors mode
# ---------------------------------------------------------------------------


class TestCmdSeedAnchors:
    def _args(self, **kw):
        defaults = {
            "anchors": None,
            "directory": None,
            "db_path": None,
            "dry_run": False,
        }
        defaults.update(kw)
        return SimpleNamespace(**defaults)

    def test_anchors_mode_dry_run(self, tmp_path, capsys):
        p = tmp_path / "a.yaml"
        p.write_text("- content: my anchor\n  tags: [x]\n")
        args = self._args(anchors=str(p), dry_run=True)
        cmd_seed(args)
        out = capsys.readouterr()
        assert "DRY RUN" in out.err or "DRY RUN" in out.out

    def test_anchors_mode_file_not_found_exits(self, tmp_path, capsys):
        args = self._args(anchors=str(tmp_path / "missing.yaml"))
        with pytest.raises(SystemExit) as exc_info:
            cmd_seed(args)
        assert exc_info.value.code == 1

    def test_anchors_mode_daemon_unreachable_message(self, tmp_path, capsys):
        p = tmp_path / "a.yaml"
        p.write_text("- content: anchor\n  tags: [t]\n")
        args = self._args(anchors=str(p), dry_run=False)
        with patch("yadgar.core.cli.seed._daemon_health_ok", return_value=False):
            cmd_seed(args)
        err = capsys.readouterr().err
        assert "daemon" in err.lower() or "unreachable" in err.lower()

    def test_anchors_mode_success_prints_json(self, tmp_path, capsys):
        p = tmp_path / "a.yaml"
        p.write_text("- content: anchor\n  tags: [t]\n")
        args = self._args(anchors=str(p), dry_run=False)
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"created": 1}).encode()
        with (
            patch("yadgar.core.cli.seed._daemon_health_ok", return_value=True),
            patch("urllib.request.urlopen", return_value=mock_resp),
        ):
            cmd_seed(args)
        out = capsys.readouterr()
        parsed = json.loads(out.out.strip())
        assert parsed["created"] == 1


# ---------------------------------------------------------------------------
# cmd_seed — project scan mode
# ---------------------------------------------------------------------------


class TestCmdSeedProjectMode:
    def _args(self, directory, **kw):
        defaults = {
            "anchors": None,
            "db_path": None,
            "dry_run": False,
        }
        defaults.update(kw)
        return SimpleNamespace(directory=directory, **defaults)

    def test_dry_run_prints_would_create(self, tmp_path, capsys):
        args = self._args(str(tmp_path), dry_run=True)
        mock_result = {
            "project": "test_project",
            "memories_generated": 5,
            "created": 0,
            "replaced": 0,
            "memories": [{"tags": ["x"], "content": "memory content here"} for _ in range(5)],
        }
        with patch("yadgar.core.seed.seed_project", return_value=mock_result):
            cmd_seed(args)
        err = capsys.readouterr().err
        assert "DRY RUN" in err or "5" in err

    def test_success_prints_seeded_count(self, tmp_path, capsys):
        args = self._args(str(tmp_path))
        mock_result = {
            "project": "my_project",
            "memories_generated": 10,
            "created": 8,
            "replaced": 0,
            "memories": [],
        }
        with patch("yadgar.core.seed.seed_project", return_value=mock_result):
            cmd_seed(args)
        err = capsys.readouterr().err
        assert "my_project" in err or "8" in err

    def test_success_prints_json_to_stdout(self, tmp_path, capsys):
        args = self._args(str(tmp_path))
        mock_result = {
            "project": "proj",
            "memories_generated": 3,
            "created": 3,
            "replaced": 0,
            "memories": [],
        }
        with patch("yadgar.core.seed.seed_project", return_value=mock_result):
            cmd_seed(args)
        out = capsys.readouterr().out
        parsed = json.loads(out.strip())
        assert parsed["created"] == 3

    def test_replaced_included_in_message(self, tmp_path, capsys):
        args = self._args(str(tmp_path))
        mock_result = {
            "project": "p",
            "memories_generated": 5,
            "created": 3,
            "replaced": 2,
            "memories": [],
        }
        with patch("yadgar.core.seed.seed_project", return_value=mock_result):
            cmd_seed(args)
        err = capsys.readouterr().err
        assert "replaced" in err.lower() or "2" in err
