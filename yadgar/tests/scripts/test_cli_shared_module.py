"""Tests for yadgar/core/cli/_shared.py — shared CLI forward helpers.

T2 Car B: ``init_replay_lightweight`` (local engine construction) is GONE —
the CLI drain/restore subcommands are thin HTTP forwarders to the backend
(POST /restore + the /admin op ``pre_compact_drain``). These tests pin:
  * forward_restore delegates to the core _forward_restore helper
  * forward_pre_compact_drain delegates to _forward_admin with the right op
  * silence_logging disables library logging (hooks must only print data)
  * the local-construction era is really over (no init_replay_lightweight)
"""

from __future__ import annotations

import logging
from unittest.mock import patch

from yadgar.core.cli import _shared


class TestForwardRestore:
    def test_delegates_to_forward_restore(self):
        payload = {"formatted": "# R", "epoch": 2}
        with patch("yadgar.core.forward._forward_restore", return_value=payload) as fwd:
            result = _shared.forward_restore("/my/proj")
        fwd.assert_called_once_with("/my/proj")
        assert result is payload


class TestForwardPreCompactDrain:
    def test_delegates_to_forward_admin_with_op(self):
        payload = {"status": "drained", "epoch": 1, "auto_checkpoint_created": True}
        with patch("yadgar.core.forward._forward_admin", return_value=payload) as fwd:
            result = _shared.forward_pre_compact_drain("/my/proj")
        # HOOKS Car 2 + fix-drain-inflight: transcript_path (None when omitted) +
        # a host-parsed in_flight (None when no transcript_path). C11 added
        # project_id, which is None here because no caller named one — the
        # payload carries the absence explicitly rather than substituting a key.
        fwd.assert_called_once_with(
            "pre_compact_drain",
            {
                "directory": "/my/proj",
                "transcript_path": None,
                "in_flight": None,
                "project_id": None,
            },
        )
        assert result is payload

    def test_forwards_the_project_id_it_is_given(self):
        """C11: a resolved identity rides the /admin payload verbatim."""
        payload = {"status": "drained"}
        with patch("yadgar.core.forward._forward_admin", return_value=payload) as fwd:
            _shared.forward_pre_compact_drain("/my/proj", None, "acme/widget")
        assert fwd.call_args.args[1]["project_id"] == "acme/widget"

    def test_forwards_transcript_path_when_given(self):
        """HOOKS Car 2: an explicit transcript_path is forwarded to the backend."""
        payload = {"status": "drained"}
        fixture = str(
            __import__("pathlib").Path(__file__).parent.parent
            / "fixtures"
            / "transcript_in_flight.jsonl"
        )
        with patch("yadgar.core.forward._forward_admin", return_value=payload) as fwd:
            _shared.forward_pre_compact_drain("/my/proj", fixture)
        args, _ = fwd.call_args
        assert args[0] == "pre_compact_drain"
        sent = args[1]
        assert sent["directory"] == "/my/proj"
        assert sent["transcript_path"] == fixture

    def test_host_side_parse_populates_in_flight(self):
        """Car fix-drain-inflight: the drain forwarder parses the transcript on the
        HOST (where .claude + git are visible) and carries a NON-empty in_flight in
        the /admin payload — the backend just persists it. This is the core fix:
        previously the payload carried only the path and the (blind) container
        produced an empty in_flight."""
        payload = {"status": "drained"}
        fixture = str(
            __import__("pathlib").Path(__file__).parent.parent
            / "fixtures"
            / "transcript_in_flight.jsonl"
        )
        with patch("yadgar.core.forward._forward_admin", return_value=payload) as fwd:
            _shared.forward_pre_compact_drain("/my/proj", fixture)
        sent = fwd.call_args[0][1]
        in_flight = sent["in_flight"]
        assert in_flight is not None
        assert set(in_flight["agents"]) == {"bbbbbbbbbbbbbbbb2", "eeeeeeeeeeeeeeee5"}
        assert "bg_shell_001" in in_flight["bg_shells"]

    def test_no_transcript_no_in_flight(self):
        """No transcript_path → in_flight stays None (nothing to parse host-side)."""
        payload = {"status": "drained"}
        with patch("yadgar.core.forward._forward_admin", return_value=payload) as fwd:
            _shared.forward_pre_compact_drain("/my/proj")
        sent = fwd.call_args[0][1]
        assert sent["in_flight"] is None


class TestSilenceLogging:
    def test_disables_critical(self):
        with patch("logging.disable") as mock_disable:
            _shared.silence_logging()
        mock_disable.assert_called_once_with(logging.CRITICAL)


class TestLocalConstructionGone:
    def test_init_replay_lightweight_removed(self):
        """T2 Car B: the CLI must not construct a local replay stack anymore."""
        assert not hasattr(_shared, "init_replay_lightweight")
