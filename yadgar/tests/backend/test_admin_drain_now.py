"""Car 1 (task #29): backend ``drain_now`` admin op — synchronous cross-process drain.

RCA: ``wiki_add(wait=True)`` nudges a ``drain_now()`` that only lives in the
BACKEND process (ADR-0078 split). In the CORE MCP process ``_st._queue_drainer``
is ``None`` → the in-core nudge is a silent no-op → the caller passively waits on
the backend's 30s-interval drainer, but the 15s wait budget expires first →
``wait_timeout``.

Fix: a backend ``drain_now`` admin op runs the LIVE backend drainer's
``drain_now()`` synchronously so the core wait-path can nudge it over HTTP and
have the write commit BEFORE ``wait_for_job`` times out.

These tests pin the op body contract:
  1. drains a pending queue file synchronously (items_processed reflects it)
  2. None live drainer → graceful {items_processed: 0}, no crash
  3. registered in run_admin_op's dispatch table
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from yadgar.backend.admin_exec import admin_ops, drain, run_admin_op


def test_drain_now_op_registered():
    """``drain_now`` is a registered admin op (route validates against this set)."""
    assert "drain_now" in admin_ops()


def test_drain_now_calls_live_backend_drainer():
    """The op reads the LIVE backend drainer (_st._queue_drainer) and drains it."""
    import yadgar._shared.runtime.state as _st

    fake_drainer = MagicMock()
    fake_drainer.drain_now.return_value = 3

    with patch.object(_st, "_queue_drainer", fake_drainer):
        result = drain.drain_now({})

    fake_drainer.drain_now.assert_called_once_with()
    assert result["items_processed"] == 3
    assert result["drained"] is True


def test_drain_now_graceful_when_no_live_drainer():
    """None live drainer (op called before drainer starts) → 0 items, no crash."""
    import yadgar._shared.runtime.state as _st

    with patch.object(_st, "_queue_drainer", None):
        result = drain.drain_now({})

    assert result["items_processed"] == 0
    assert result["drained"] is False


def test_drain_now_via_run_admin_op_dispatch():
    """run_admin_op('drain_now', {}) dispatches to the impl (end-to-end registry)."""
    import yadgar._shared.runtime.state as _st

    fake_drainer = MagicMock()
    fake_drainer.drain_now.return_value = 1

    with (
        patch.object(_st, "_queue_drainer", fake_drainer),
        patch(
            "yadgar.backend.restoration.ensure_restoration_engines",
            MagicMock(),
        ),
    ):
        result = run_admin_op("drain_now", {})

    assert result["items_processed"] == 1
    assert result["drained"] is True


def test_drain_now_swallows_drainer_exception():
    """A drainer that raises mid-drain → op reports drained=False, does not propagate.

    The wait-path nudge is best-effort; a drain failure must not surface as a 500
    to the core forwarder (it falls through to the passive poll)."""
    import yadgar._shared.runtime.state as _st

    fake_drainer = MagicMock()
    fake_drainer.drain_now.side_effect = RuntimeError("db locked")

    with patch.object(_st, "_queue_drainer", fake_drainer):
        result = drain.drain_now({})

    assert result["drained"] is False
    assert result["items_processed"] == 0
