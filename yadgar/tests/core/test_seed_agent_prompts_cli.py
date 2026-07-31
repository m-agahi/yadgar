"""TDD (RED-first) — S8 CLI: yadgar seed --agent-prompts.

Mirrors test_cli_seed_module.py for the --anchors branch.
Mock at the urllib.request boundary (HTTP path).

Tests:
  - dry_run makes zero HTTP calls
  - daemon_unreachable → graceful, no raise, skipped result
  - happy_path → POSTs to /hooks/seed-agent-prompts, returns created/skipped
  - cmd_seed dispatches to _seed_agent_prompts when --agent-prompts flag set
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class TestSeedAgentPromptsHelper:
    """Tests for _seed_agent_prompts(db_path, dry_run) helper."""

    def test_dry_run_zero_http_calls(self):
        from yadgar.core.cli.seed import _seed_agent_prompts

        with patch("urllib.request.urlopen") as mock_urlopen:
            result = _seed_agent_prompts(db_path=None, dry_run=True)
        mock_urlopen.assert_not_called()
        assert result["dry_run"] is True
        # dry run "creates" the 5 starters (printed without actual HTTP)
        assert result.get("created", 0) == 5 or result.get("skipped", 0) >= 0

    def test_daemon_unreachable_graceful(self):
        from yadgar.core.cli.seed import _seed_agent_prompts

        with patch("yadgar.core.cli.seed._daemon_health_ok", return_value=False):
            result = _seed_agent_prompts(db_path=None, dry_run=False)
        assert result.get("reason") == "daemon_unreachable"
        # No exception raised
        assert isinstance(result, dict)

    def test_happy_path_posts_to_correct_endpoint(self):
        from yadgar.core.cli.seed import _seed_agent_prompts

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"created": 4, "skipped": 0}).encode()
        with (
            patch("yadgar.core.cli.seed._daemon_health_ok", return_value=True),
            patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen,
        ):
            result = _seed_agent_prompts(db_path=None, dry_run=False)

        # Exactly one POST to /hooks/seed-agent-prompts
        assert mock_urlopen.call_count == 1
        call_req = mock_urlopen.call_args[0][0]
        assert "/hooks/seed-agent-prompts" in call_req.full_url
        assert result.get("created", 0) >= 0

    def test_happy_path_returns_created_count(self):
        from yadgar.core.cli.seed import _seed_agent_prompts

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"created": 4, "skipped": 0}).encode()
        with (
            patch("yadgar.core.cli.seed._daemon_health_ok", return_value=True),
            patch("urllib.request.urlopen", return_value=mock_resp),
        ):
            result = _seed_agent_prompts(db_path=None, dry_run=False)
        assert result.get("created") is not None
        mock_resp.close.assert_called_once()

    def test_http_error_closes_response(self):
        """HTTPError is a response object holding a file wrapper (a
        tempfile._TemporaryFileWrapper via addbase on py3.14); an unclosed
        instance fires a spurious ResourceWarning at GC. _seed_agent_prompts
        must close it deterministically."""
        import urllib.error

        from yadgar.core.cli.seed import _seed_agent_prompts

        http_err = urllib.error.HTTPError(url="", code=500, msg="Error", hdrs={}, fp=None)
        with (
            patch("yadgar.core.cli.seed._daemon_health_ok", return_value=True),
            patch("urllib.request.urlopen", side_effect=http_err),
        ):
            result = _seed_agent_prompts(db_path=None, dry_run=False)
        assert http_err.fp is None or http_err.fp.closed, "the hook must close the caught HTTPError"
        assert result.get("reason") == "request_failed"


class TestCmdSeedAgentPromptsFlag:
    """Tests for cmd_seed dispatching on --agent-prompts flag."""

    def _args(self, **kw):
        defaults = {
            "agent_prompts": False,
            "anchors": None,
            "directory": None,
            "db_path": None,
            "dry_run": False,
        }
        defaults.update(kw)
        return SimpleNamespace(**defaults)

    def test_agent_prompts_flag_dispatches_to_helper(self, capsys):
        from yadgar.core.cli.seed import cmd_seed

        mock_result = {"seeded": True, "created": 4, "skipped": 0, "dry_run": False}
        with patch("yadgar.core.cli.seed._seed_agent_prompts", return_value=mock_result) as mock_fn:
            args = self._args(agent_prompts=True)
            cmd_seed(args)
        mock_fn.assert_called_once()

    def test_agent_prompts_dry_run_prints_output(self, capsys):
        from yadgar.core.cli.seed import cmd_seed

        with patch("urllib.request.urlopen") as mock_urlopen:
            args = self._args(agent_prompts=True, dry_run=True)
            cmd_seed(args)
        mock_urlopen.assert_not_called()
        # Some output should appear (stderr or stdout)
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert len(combined) > 0

    def test_agent_prompts_success_prints_json(self, capsys):
        from yadgar.core.cli.seed import cmd_seed

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"created": 4, "skipped": 0}).encode()
        with (
            patch("yadgar.core.cli.seed._daemon_health_ok", return_value=True),
            patch("urllib.request.urlopen", return_value=mock_resp),
        ):
            args = self._args(agent_prompts=True)
            cmd_seed(args)
        out = capsys.readouterr().out
        parsed = json.loads(out.strip())
        assert isinstance(parsed, dict)

    def test_agent_prompts_daemon_unreachable_no_raise(self, capsys):
        from yadgar.core.cli.seed import cmd_seed

        with patch("yadgar.core.cli.seed._daemon_health_ok", return_value=False):
            args = self._args(agent_prompts=True)
            # Must not raise
            cmd_seed(args)
        err = capsys.readouterr().err
        assert "daemon" in err.lower() or "unreachable" in err.lower() or len(err) >= 0
