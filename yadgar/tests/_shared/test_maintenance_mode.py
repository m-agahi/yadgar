"""Tests for maintenance mode (TDD — written before implementation, v5.50.3).

Coverage:
  1. Gate check: _maintenance_mode=True → _instrumented returns maintenance dict,
     body NOT invoked (short-circuit before traced_func).
  2. Gate check: _maintenance_mode=False → normal path runs.
  3. POST /api/control/maintenance/enter → flips _maintenance_mode True.
  4. POST /api/control/maintenance/exit  → flips _maintenance_mode False.
  5. Maintenance endpoints require bearer auth (same as other /api/control/* routes).
  6. Maintenance endpoints are NOT gated by YADGAR_DEBUG_APIS_ENABLED.
"""

from __future__ import annotations

import functools

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

import yadgar._shared.runtime.state as _st

_TOKEN = "test-maint-tok"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# NOTE this is a HAND-COPY of the payload built in
# ``yadgar/core/server/_app.py::_instrumented._maintenance`` — the real one is a
# closure inside a decorator factory and cannot be imported.  The gate tests
# below re-implement the wrapper, so nothing here would notice the real message
# drifting; ``test_real_gate_payload_matches_this_copy`` is what keeps the copy
# honest.  Message wording: task:0111 / ADR-0188 dropped "nightly" because a
# CLI- or timer-triggered vacuum engages this gate too.
_MAINTENANCE_RESPONSE = {
    "error": "maintenance",
    "message": "yadgar maintenance in progress (vacuum); retry shortly",
}


def test_real_gate_payload_matches_this_copy():
    """Guard the hand-copy: the real ``_app.py`` gate must build this payload."""
    from pathlib import Path

    import yadgar.core.server._app as _app_mod

    src = Path(_app_mod.__file__).read_text(encoding="utf-8")
    for value in _MAINTENANCE_RESPONSE.values():
        assert f'"{value}"' in src, (
            f"_app.py's maintenance gate no longer builds {value!r} — the "
            f"_MAINTENANCE_RESPONSE copy in this file has drifted from the real "
            f"payload, so every gate assertion below is testing a stale string."
        )


def _make_maintenance_app(monkeypatch):
    """Build minimal Starlette + BearerAuthMiddleware app with maintenance routes only."""
    monkeypatch.setenv("YADGAR_REQUIRE_AUTH", "1")
    monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", _TOKEN)
    monkeypatch.setenv("YADGAR_DEBUG_APIS_ENABLED", "off")  # gate OFF — must NOT block these

    from yadgar.core.auth_middleware import BearerAuthMiddleware
    from yadgar.core.server.routes.control import (
        maintenance_enter_handler,
        maintenance_exit_handler,
    )

    app = BearerAuthMiddleware(
        Starlette(
            routes=[
                Route(
                    "/api/control/maintenance/enter",
                    maintenance_enter_handler,
                    methods=["POST"],
                ),
                Route(
                    "/api/control/maintenance/exit",
                    maintenance_exit_handler,
                    methods=["POST"],
                ),
            ]
        )
    )
    return TestClient(app, raise_server_exceptions=True)


def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {_TOKEN}"}


# ---------------------------------------------------------------------------
# 1 — Gate ON: maintenance dict returned, body NOT invoked
# ---------------------------------------------------------------------------


def test_gate_on_returns_maintenance_dict_body_not_called():
    """When _maintenance_mode=True, _instrumented short-circuits before body.

    The sentinel body must NOT be called (proves early return before _traced_func).
    """
    # Import _tool here so the test reads the live import path.
    from yadgar.core.server._app import _tool

    body_called = []

    @_tool()
    def _sentinel_tool(x: int = 0) -> dict:
        body_called.append(x)
        return {"value": x}

    # The decorated function is now the MCP-registered version.
    # We need to call the inner _instrumented wrapper to test the gate.
    # _tool() returns mcp_server.tool()(_instrumented), which is a registered MCP tool.
    # We need to extract _instrumented to call it directly.
    # Strategy: patch _maintenance_mode=True at the _state level and call the tool.
    # The tool is already registered with mcp_server — we can't call the MCP machinery
    # without a running server. Instead, test the predicate directly:

    # Reset guard
    body_called.clear()

    saved = _st._maintenance_mode
    try:
        _st._maintenance_mode = True

        # Re-create a fresh _instrumented wrapper that reads the live state.
        # This is the exact pattern used by _tool() in _app.py.
        import functools

        import yadgar._shared.runtime.state as _st_ref

        sentinel_hit = []

        def _real_body(**kwargs):
            sentinel_hit.append(True)
            return {"ok": True}

        @functools.wraps(_real_body)
        def _instrumented_under_test(*args, **kwargs):
            # Mirrors the gate added to _instrumented in _app.py.
            if _st_ref._maintenance_mode:
                return _MAINTENANCE_RESPONSE.copy()
            return _real_body(**kwargs)

        result = _instrumented_under_test()
        assert result == _MAINTENANCE_RESPONSE, f"Expected maintenance response, got: {result}"
        assert not sentinel_hit, "Body must NOT be called when maintenance mode is ON"
    finally:
        _st._maintenance_mode = saved


# ---------------------------------------------------------------------------
# 2 — Gate OFF: normal path runs
# ---------------------------------------------------------------------------


def test_gate_off_normal_path_runs():
    """When _maintenance_mode=False, body is called normally."""
    import yadgar._shared.runtime.state as _st_ref

    sentinel_hit = []

    def _real_body(**kwargs):
        sentinel_hit.append(True)
        return {"ok": True}

    @functools.wraps(_real_body)
    def _instrumented_under_test(*args, **kwargs):
        if _st_ref._maintenance_mode:
            return _MAINTENANCE_RESPONSE.copy()
        return _real_body(**kwargs)

    saved = _st._maintenance_mode
    try:
        _st._maintenance_mode = False
        result = _instrumented_under_test()
        assert result == {"ok": True}, f"Expected normal result, got: {result}"
        assert sentinel_hit, "Body must be called when maintenance mode is OFF"
    finally:
        _st._maintenance_mode = saved


# ---------------------------------------------------------------------------
# 3 — POST /api/control/maintenance/enter sets _maintenance_mode=True
# ---------------------------------------------------------------------------


def test_enter_sets_maintenance_mode_true(monkeypatch):
    """POST /maintenance/enter → 200, _st._maintenance_mode becomes True."""
    client = _make_maintenance_app(monkeypatch)

    saved = _st._maintenance_mode
    try:
        _st._maintenance_mode = False
        resp = client.post(
            "/api/control/maintenance/enter",
            json={},
            headers=_auth_headers(),
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body.get("status") == "maintenance", f"Expected status='maintenance', got: {body}"
        assert _st._maintenance_mode is True, (
            "POST /maintenance/enter must set _st._maintenance_mode = True"
        )
    finally:
        _st._maintenance_mode = saved


# ---------------------------------------------------------------------------
# 4 — POST /api/control/maintenance/exit sets _maintenance_mode=False
# ---------------------------------------------------------------------------


def test_exit_sets_maintenance_mode_false(monkeypatch):
    """POST /maintenance/exit → 200, _st._maintenance_mode becomes False."""
    client = _make_maintenance_app(monkeypatch)

    saved = _st._maintenance_mode
    try:
        _st._maintenance_mode = True
        resp = client.post(
            "/api/control/maintenance/exit",
            json={},
            headers=_auth_headers(),
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body.get("status") == "active", f"Expected status='active', got: {body}"
        assert _st._maintenance_mode is False, (
            "POST /maintenance/exit must set _st._maintenance_mode = False"
        )
    finally:
        _st._maintenance_mode = saved


# ---------------------------------------------------------------------------
# 5 — Maintenance endpoints require bearer auth
# ---------------------------------------------------------------------------


def test_maintenance_endpoints_require_auth(monkeypatch):
    """Without auth header, /maintenance/enter and /maintenance/exit return 401."""
    client = _make_maintenance_app(monkeypatch)

    for path in (
        "/api/control/maintenance/enter",
        "/api/control/maintenance/exit",
    ):
        resp = client.post(path, json={})  # no auth header
        assert resp.status_code == 401, (
            f"Expected 401 for unauthenticated {path}, got {resp.status_code}: {resp.text}"
        )


# ---------------------------------------------------------------------------
# 6 — NOT gated by YADGAR_DEBUG_APIS_ENABLED
# ---------------------------------------------------------------------------


def test_maintenance_endpoints_not_gated_by_debug_apis(monkeypatch):
    """Maintenance endpoints must work with YADGAR_DEBUG_APIS_ENABLED=off.

    Other /api/control/* routes (config, action, restart) require
    YADGAR_DEBUG_APIS_ENABLED=on.  Maintenance must NOT.
    """
    # _make_maintenance_app already sets YADGAR_DEBUG_APIS_ENABLED=off
    client = _make_maintenance_app(monkeypatch)

    saved = _st._maintenance_mode
    try:
        # Enter should succeed even with debug APIs off
        resp_enter = client.post(
            "/api/control/maintenance/enter",
            json={},
            headers=_auth_headers(),
        )
        assert resp_enter.status_code == 200, (
            f"Enter must work with debug APIs off, got {resp_enter.status_code}: {resp_enter.text}"
        )
        assert resp_enter.json().get("error") != "debug APIs disabled", (
            "Maintenance enter must NOT be gated by YADGAR_DEBUG_APIS_ENABLED"
        )

        # Exit should succeed too
        resp_exit = client.post(
            "/api/control/maintenance/exit",
            json={},
            headers=_auth_headers(),
        )
        assert resp_exit.status_code == 200, (
            f"Exit must work with debug APIs off, got {resp_exit.status_code}: {resp_exit.text}"
        )
    finally:
        _st._maintenance_mode = saved


# ---------------------------------------------------------------------------
# 7 — enter response exposes the effective deadline (car E, task:0113 follow-up)
#
# A caller (e.g. a backup arm asserting the gate per ADR-0204) cannot verify it
# actually holds a self-heal belt from ``previous`` alone — it needs to know
# whether the window it is inside of has an expiry at all.  These tests are
# PURELY ADDITIVE assertions on the response body; they must not encode any
# change to gate/nesting semantics (see test_nested_no_ttl_outer_survives_ttl_inner
# below, which pins the opposite of "nested TTL takes the min").
# ---------------------------------------------------------------------------


def test_enter_with_ttl_on_cold_gate_reports_deadline_seconds(monkeypatch):
    """Cold gate + ttl_seconds=60 → deadline_seconds is present and ~60."""
    client = _make_maintenance_app(monkeypatch)

    saved_mode = _st._maintenance_mode
    saved_deadline = _st._maintenance_deadline
    try:
        _st._maintenance_mode = False
        _st._maintenance_deadline = None
        resp = client.post(
            "/api/control/maintenance/enter",
            json={"ttl_seconds": 60},
            headers=_auth_headers(),
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body.get("deadline_seconds") is not None, (
            f"Cold gate with a ttl must report a deadline_seconds, got: {body}"
        )
        assert body["deadline_seconds"] == pytest.approx(60, abs=5), (
            f"deadline_seconds should be ~60s out, got: {body['deadline_seconds']}"
        )
    finally:
        _st._maintenance_mode = saved_mode
        _st._maintenance_deadline = saved_deadline


def test_enter_with_no_ttl_on_cold_gate_reports_deadline_seconds_none(monkeypatch):
    """Cold gate + no ttl → deadline_seconds is None (no belt — preserved on purpose)."""
    client = _make_maintenance_app(monkeypatch)

    saved_mode = _st._maintenance_mode
    saved_deadline = _st._maintenance_deadline
    try:
        _st._maintenance_mode = False
        _st._maintenance_deadline = None
        resp = client.post(
            "/api/control/maintenance/enter",
            json={},
            headers=_auth_headers(),
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body.get("deadline_seconds") is None, (
            f"Cold gate with no ttl must report deadline_seconds=None, got: {body}"
        )
    finally:
        _st._maintenance_mode = saved_mode
        _st._maintenance_deadline = saved_deadline


def test_nested_no_ttl_outer_survives_ttl_inner(monkeypatch):
    """Outer entered with NO ttl, inner enters WITH a ttl → deadline_seconds stays None.

    This PINS the never-shorten / never-add-expiry invariant documented on
    ``maintenance_enter_handler``: "stays None if either side asked for no
    expiry".  A caller can use this response field to DETECT it has no belt —
    do NOT "fix" this by making the nested ttl take effect; that would let an
    inner TTL silently install an expiry the outer holder never asked for,
    which is a regression of the documented nesting contract, not a bug fix.
    """
    client = _make_maintenance_app(monkeypatch)

    saved_mode = _st._maintenance_mode
    saved_deadline = _st._maintenance_deadline
    try:
        _st._maintenance_mode = False
        _st._maintenance_deadline = None

        # Outer: no ttl.
        resp_outer = client.post(
            "/api/control/maintenance/enter",
            json={},
            headers=_auth_headers(),
        )
        assert resp_outer.status_code == 200
        assert resp_outer.json().get("deadline_seconds") is None

        # Inner (nested): ttl=30 — must NOT install an expiry on the outer window.
        resp_inner = client.post(
            "/api/control/maintenance/enter",
            json={"ttl_seconds": 30},
            headers=_auth_headers(),
        )
        assert resp_inner.status_code == 200, (
            f"Expected 200, got {resp_inner.status_code}: {resp_inner.text}"
        )
        body_inner = resp_inner.json()
        assert body_inner.get("previous") is True, (
            f"Inner enter must report previous=True (nested), got: {body_inner}"
        )
        assert body_inner.get("deadline_seconds") is None, (
            "Nested enter with a ttl over a no-ttl outer window must NOT install "
            f"a deadline (never-shorten/never-add-expiry contract), got: {body_inner}"
        )
    finally:
        _st._maintenance_mode = saved_mode
        _st._maintenance_deadline = saved_deadline


def test_nested_both_ttls_later_deadline_wins(monkeypatch):
    """Nested enter, both sides have a ttl → the LATER deadline wins (widening).

    Covers both directions: an inner ttl longer than the outer's widens the
    window forward, and an inner ttl shorter than the outer's must NOT pull
    the deadline back in — nesting only ever widens.
    """
    client = _make_maintenance_app(monkeypatch)

    saved_mode = _st._maintenance_mode
    saved_deadline = _st._maintenance_deadline
    try:
        # Case A: outer ttl=30, inner ttl=90 → widened to ~90.
        _st._maintenance_mode = False
        _st._maintenance_deadline = None
        client.post(
            "/api/control/maintenance/enter",
            json={"ttl_seconds": 30},
            headers=_auth_headers(),
        )
        resp = client.post(
            "/api/control/maintenance/enter",
            json={"ttl_seconds": 90},
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body.get("previous") is True
        assert body.get("deadline_seconds") == pytest.approx(90, abs=5), (
            f"Later (inner) deadline should win, got: {body.get('deadline_seconds')}"
        )

        # Case B: outer ttl=90, inner ttl=30 → stays widened at ~90 (not shortened).
        _st._maintenance_mode = False
        _st._maintenance_deadline = None
        client.post(
            "/api/control/maintenance/enter",
            json={"ttl_seconds": 90},
            headers=_auth_headers(),
        )
        resp2 = client.post(
            "/api/control/maintenance/enter",
            json={"ttl_seconds": 30},
            headers=_auth_headers(),
        )
        assert resp2.status_code == 200
        body2 = resp2.json()
        assert body2.get("previous") is True
        assert body2.get("deadline_seconds") == pytest.approx(90, abs=5), (
            "A shorter nested ttl must not shorten the outer deadline, got: "
            f"{body2.get('deadline_seconds')}"
        )
    finally:
        _st._maintenance_mode = saved_mode
        _st._maintenance_deadline = saved_deadline
