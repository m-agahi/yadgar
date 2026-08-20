"""task:0113 — maintenance gate: previous-state reporting + TTL self-heal.

Two properties the vacuum write-gate depends on:

1. ``/maintenance/enter`` reports the PRIOR state.  ``nightly_cycle`` holds the
   gate across steps 1-7 and runs the vacuum at step 4; a vacuum that
   unconditionally exits would un-gate the engine while the nightly still has DB
   work to do.  The vacuum therefore exits only when it was the one that entered.

2. The flag carries an optional monotonic DEADLINE.  ``cmd_vacuum_impl``'s
   ``finally`` covers returns, exceptions and ``sys.exit`` — it does NOT cover
   SIGKILL / OOM-kill / power loss, and post-task:0111 the core no longer
   restarts during a vacuum, so a clear-on-start reset would never fire.  The TTL
   is the only backstop that fires unconditionally, and its expiry must be LOUD:
   a fired TTL means a vacuum died without cleanup.

A missing/blank TTL keeps today's behaviour (no expiry) — a caller that has not
been updated must not regress.
"""

from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _reset_maintenance_state():
    """Every gate var is process-global — reset around each test.

    Delegates to the shared resetter so a var added to the window (Car 1 added
    ``operation`` / ``phase``) cannot leak across tests just because this fixture
    was not updated alongside it.
    """
    from yadgar._shared.runtime.maintenance import reset_maintenance_state

    reset_maintenance_state()
    yield
    reset_maintenance_state()


def _request(body: dict | None = None):
    """Minimal stand-in for a starlette Request with an async .json()."""

    async def _json():
        if body is None:
            raise ValueError("no body")
        return body

    return SimpleNamespace(json=_json)


def _enter(body: dict | None = None) -> dict:
    from yadgar.core.server.routes.control import maintenance_enter_handler

    resp = asyncio.run(maintenance_enter_handler(_request(body)))
    import json as _json

    return _json.loads(bytes(resp.body))


def _exit() -> dict:
    from yadgar.core.server.routes.control import maintenance_exit_handler

    resp = asyncio.run(maintenance_exit_handler(_request({})))
    import json as _json

    return _json.loads(bytes(resp.body))


# ---------------------------------------------------------------------------
# 1. previous-state reporting
# ---------------------------------------------------------------------------


class TestPreviousState:
    def test_enter_reports_previous_false_then_true(self) -> None:
        first = _enter({})
        assert first["maintenance_mode"] is True
        assert first["previous"] is False, "the first enter opened the window"
        second = _enter({})
        assert second["previous"] is True, (
            "a nested enter reported previous=False — the inner caller will exit "
            "the OUTER caller's window (the nightly un-wedge regression)"
        )

    def test_exit_clears_flag_and_deadline(self) -> None:
        import yadgar._shared.runtime.state as _st

        _enter({"ttl_seconds": 600})
        assert _st._maintenance_deadline is not None
        _exit()
        assert _st._maintenance_mode is False
        assert _st._maintenance_deadline is None, (
            "a stale deadline survived the exit — the next TTL-less enter would "
            "inherit an already-expired window and self-clear immediately"
        )

    def test_nested_enter_never_shortens_the_outer_window(self) -> None:
        """Nightly holds the gate far longer than the vacuum's own TTL.

        A nested enter that overwrote the deadline with its own smaller value
        would expire the OUTER window mid-nightly.
        """
        import yadgar._shared.runtime.state as _st

        _enter({"ttl_seconds": 100000})
        outer = _st._maintenance_deadline
        _enter({"ttl_seconds": 1})
        assert _st._maintenance_deadline == outer

    def test_nested_enter_under_a_no_expiry_outer_stays_no_expiry(self) -> None:
        import yadgar._shared.runtime.state as _st

        _enter({})
        assert _st._maintenance_deadline is None
        _enter({"ttl_seconds": 5})
        assert _st._maintenance_deadline is None


# ---------------------------------------------------------------------------
# 2. TTL expiry in the MCP short-circuit
# ---------------------------------------------------------------------------


_GATE_LOGGER = "yadgar.core.server._app"


def _wrap(fn):
    """Return the SYNC instrumented wrapper — the real MCP-tool gate path."""
    from yadgar.core.server._app import _build_tool_wrappers

    sync_wrapper, _async_wrapper = _build_tool_wrappers(fn, fn, lambda _r: 0)
    return sync_wrapper


def _decorated_tool():
    """A trivially-instrumented callable that goes through the maintenance gate."""

    def _tool():
        return {"ran": True}

    return _wrap(_tool)


class TestTtlExpiry:
    def test_maintenance_short_circuits_before_any_db_call(self) -> None:
        """Pin the pre-existing behaviour so the TTL edit cannot move the check."""
        import yadgar._shared.runtime.state as _st

        boom = []

        def _tool():
            boom.append("db")
            return {"ran": True}

        wrapped = _wrap(_tool)
        _st._maintenance_mode = True
        out = wrapped()
        assert out["error"] == "maintenance"
        assert boom == [], "the tool body ran during maintenance"

    def test_expired_ttl_is_treated_as_not_in_maintenance(self, caplog, monkeypatch) -> None:
        import yadgar._shared.runtime.state as _st

        monkeypatch.setattr(logging.getLogger(_GATE_LOGGER), "propagate", True)
        _st._maintenance_mode = True
        _st._maintenance_deadline = time.monotonic() - 1.0
        with caplog.at_level(logging.WARNING, logger=_GATE_LOGGER):
            out = _decorated_tool()()
        assert out == {"ran": True}, "an EXPIRED maintenance window still gated the tool"
        assert _st._maintenance_mode is False, "the expired flag was not cleared"
        assert _st._maintenance_deadline is None, "the expired deadline was not cleared"
        assert any("maintenance" in r.message.lower() for r in caplog.records), (
            "a fired TTL means a vacuum died without cleanup — it must log LOUDLY"
        )

    def test_ttl_absent_never_expires(self) -> None:
        """Back-compat: a caller that sends no TTL keeps today's behaviour."""
        import yadgar._shared.runtime.state as _st

        _enter({})
        assert _st._maintenance_deadline is None
        out = _decorated_tool()()
        assert out["error"] == "maintenance"

    def test_unexpired_ttl_still_gates(self) -> None:
        import yadgar._shared.runtime.state as _st

        _enter({"ttl_seconds": 3600})
        out = _decorated_tool()()
        assert out["error"] == "maintenance"
        assert _st._maintenance_mode is True
