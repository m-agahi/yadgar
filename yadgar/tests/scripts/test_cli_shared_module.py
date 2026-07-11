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
        with patch(
            "yadgar.core.server.tools._forward._forward_restore", return_value=payload
        ) as fwd:
            result = _shared.forward_restore("/my/proj")
        fwd.assert_called_once_with("/my/proj")
        assert result is payload


class TestForwardPreCompactDrain:
    def test_delegates_to_forward_admin_with_op(self):
        payload = {"status": "drained", "epoch": 1, "auto_checkpoint_created": True}
        with patch("yadgar.core.server.tools._forward._forward_admin", return_value=payload) as fwd:
            result = _shared.forward_pre_compact_drain("/my/proj")
        fwd.assert_called_once_with("pre_compact_drain", {"directory": "/my/proj"})
        assert result is payload


class TestSilenceLogging:
    def test_disables_critical(self):
        with patch("logging.disable") as mock_disable:
            _shared.silence_logging()
        mock_disable.assert_called_once_with(logging.CRITICAL)


class TestLocalConstructionGone:
    def test_init_replay_lightweight_removed(self):
        """T2 Car B: the CLI must not construct a local replay stack anymore."""
        assert not hasattr(_shared, "init_replay_lightweight")
