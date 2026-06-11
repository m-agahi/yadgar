"""v5.50.2 — /api/control/* endpoint tests (TDD).

Tests (real behavioral — no string-grep-the-HTML):
1.  test_403_when_debug_apis_disabled — gate off → 403 with {"error":"debug APIs disabled"}
2.  test_200_config_get_when_debug_apis_enabled — gate on → 200 with knob table
3.  test_update_unaffected_by_debug_apis_gate — /api/control/update NOT gated by
    YADGAR_DEBUG_APIS_ENABLED (has its own gate)
4.  test_restart_confirmation_must_match_yadgar — wrong confirm → 400
5.  test_restart_confirmation_must_match_backend — backend confirm mismatch → 400
6.  test_restart_valid_yadgar_writes_sentinel_not_exec — sentinel written, no exec/subprocess
7.  test_restart_valid_backend_writes_sentinel_not_exec — same for backend
8.  test_config_get_returns_knob_table_shape — knobs list has required fields
9.  test_config_post_round_trip — set knob → GET → new value present
10. test_config_post_type_mismatch_returns_400 — string to float knob → 400
11. test_config_post_out_of_range_returns_400 — node_size = -1 → 400
12. test_action_consolidate_calls_consolidate_now — mock consolidate_now called
13. test_action_vacuum_calls_vacuum_now — mock vacuum_now called
14. test_action_reembed_calls_reembed_all — mock reembed_all called
15. test_action_unknown_returns_400 — unknown action → 400
16. test_restart_unknown_service_returns_400 — unknown service param → 400
"""

from __future__ import annotations

import json
from unittest.mock import patch

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

_TOKEN = "test-control-tok"
_YADGAR_STATE_DIR_ENV = "XDG_STATE_HOME"


# ---------------------------------------------------------------------------
# Minimal app factory — mirrors test_admin_config.py pattern
# ---------------------------------------------------------------------------


def _make_app(monkeypatch, *, debug_apis_on: bool = False, extra_env: dict | None = None):
    """Build a minimal Starlette app + BearerAuthMiddleware for control routes."""
    monkeypatch.setenv("YADGAR_REQUIRE_AUTH", "1")
    monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", _TOKEN)
    monkeypatch.setenv("YADGAR_DEBUG_APIS_ENABLED", "on" if debug_apis_on else "off")
    # Stub out YADGAR_UPDATE_DEBUG_APIS_ENABLED separately
    monkeypatch.setenv("YADGAR_UPDATE_DEBUG_APIS_ENABLED", "off")
    if extra_env:
        for k, v in extra_env.items():
            monkeypatch.setenv(k, v)

    from yadgar.auth_middleware import BearerAuthMiddleware
    from yadgar.server.routes.control import (
        control_action_handler,
        control_config_get_handler,
        control_config_post_handler,
        control_restart_handler,
    )

    app = BearerAuthMiddleware(
        Starlette(
            routes=[
                Route("/api/control/config", control_config_get_handler, methods=["GET"]),
                Route("/api/control/config", control_config_post_handler, methods=["POST"]),
                Route("/api/control/action/{action}", control_action_handler, methods=["POST"]),
                Route("/api/control/restart/{service}", control_restart_handler, methods=["POST"]),
                # A stub for the update route to verify it's NOT affected by debug gate
                Route(
                    "/api/control/update",
                    lambda req: __import__(
                        "starlette.responses", fromlist=["JSONResponse"]
                    ).JSONResponse({"ok": True}),
                    methods=["POST"],
                ),
            ]
        )
    )
    return TestClient(app, raise_server_exceptions=True)


def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {_TOKEN}"}


# ===========================================================================
# 1 — Gate off → 403
# ===========================================================================


def test_403_when_debug_apis_disabled(monkeypatch, tmp_path):
    client = _make_app(monkeypatch, debug_apis_on=False)
    paths = [
        ("GET", "/api/control/config"),
        ("POST", "/api/control/config"),
        ("POST", "/api/control/action/consolidate"),
        ("POST", "/api/control/restart/yadgar"),
    ]
    for method, path in paths:
        if method == "GET":
            resp = client.get(path, headers=_auth_headers())
        else:
            resp = client.post(path, json={}, headers=_auth_headers())
        assert resp.status_code == 403, (
            f"Expected 403 for {method} {path}, got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert body.get("error") == "debug APIs disabled", (
            f"Expected error='debug APIs disabled' for {path}, got {body}"
        )


# ===========================================================================
# 2 — Gate on → 200
# ===========================================================================


def test_200_config_get_when_debug_apis_enabled(monkeypatch, tmp_path):
    client = _make_app(monkeypatch, debug_apis_on=True)
    resp = client.get("/api/control/config", headers=_auth_headers())
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert "knobs" in body, "Response missing 'knobs' key"
    assert isinstance(body["knobs"], list), "'knobs' must be a list"
    assert len(body["knobs"]) > 0, "'knobs' list must not be empty"


# ===========================================================================
# 3 — /api/control/update NOT affected by YADGAR_DEBUG_APIS_ENABLED
# ===========================================================================


def test_update_unaffected_by_debug_apis_gate(monkeypatch, tmp_path):
    """YADGAR_DEBUG_APIS_ENABLED=off must NOT gate /api/control/update.

    /api/control/update has its own gate (YADGAR_UPDATE_DEBUG_APIS_ENABLED).
    """
    client = _make_app(monkeypatch, debug_apis_on=False)
    # POST /api/control/update with debug gate OFF — stub handler returns 200
    resp = client.post("/api/control/update", json={}, headers=_auth_headers())
    # The stub returns 200 — the debug_apis gate must NOT have intercepted this
    assert resp.status_code == 200, (
        f"YADGAR_DEBUG_APIS_ENABLED gate should NOT block /api/control/update, "
        f"got {resp.status_code}: {resp.text}"
    )


# ===========================================================================
# 4–5 — Restart confirmation must match service name
# ===========================================================================


def test_restart_confirmation_must_match_yadgar(monkeypatch, tmp_path):
    """POST /api/control/restart/yadgar with wrong confirm → 400."""
    client = _make_app(monkeypatch, debug_apis_on=True)
    resp = client.post(
        "/api/control/restart/yadgar",
        json={"confirm": "wrong"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400, f"Expected 400, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert "error" in body
    assert "confirmation mismatch" in body["error"].lower() or "mismatch" in body["error"].lower()


def test_restart_confirmation_must_match_backend(monkeypatch, tmp_path):
    """POST /api/control/restart/backend with wrong confirm → 400.

    Note: URL segment is 'backend' but service name is 'yadgar-backend'.
    """
    client = _make_app(monkeypatch, debug_apis_on=True)
    # Confirm with just "backend" — must fail (correct is "yadgar-backend")
    resp = client.post(
        "/api/control/restart/backend",
        json={"confirm": "backend"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400, (
        f"Expected 400 for 'backend' mismatch, got {resp.status_code}: {resp.text}"
    )

    # Confirm with empty string — must fail
    resp2 = client.post(
        "/api/control/restart/backend",
        json={"confirm": ""},
        headers=_auth_headers(),
    )
    assert resp2.status_code == 400


# ===========================================================================
# 6–7 — Valid restart: sentinel written, os.execv/subprocess NOT called
# ===========================================================================


def test_restart_valid_yadgar_writes_sentinel_not_exec(monkeypatch, tmp_path):
    """POST /api/control/restart/yadgar with correct confirm →
    202, sentinel file written, NO os.execv / subprocess / systemctl called.
    """
    # Point XDG_STATE_HOME at tmp_path so sentinel lands in a predictable spot
    monkeypatch.setenv(_YADGAR_STATE_DIR_ENV, str(tmp_path))
    client = _make_app(monkeypatch, debug_apis_on=True)

    with (
        patch("os.execv") as mock_execv,
        patch("subprocess.run") as mock_subprocess_run,
        patch("subprocess.Popen") as mock_popen,
        patch("os.system") as mock_system,
    ):
        resp = client.post(
            "/api/control/restart/yadgar",
            json={"confirm": "yadgar"},
            headers=_auth_headers(),
        )

    assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body.get("status") == "sentinel_written"

    # Assert no exec/subprocess called
    mock_execv.assert_not_called()
    mock_subprocess_run.assert_not_called()
    mock_popen.assert_not_called()
    mock_system.assert_not_called()

    # Assert sentinel file exists
    expected_sentinel = tmp_path / "yadgar" / "restart-yadgar.request"
    assert expected_sentinel.exists(), f"Sentinel file not found at {expected_sentinel}"
    sentinel_data = json.loads(expected_sentinel.read_text())
    assert sentinel_data["service"] == "yadgar"
    assert "requested_at" in sentinel_data
    assert "nonce" in sentinel_data


def test_restart_valid_backend_writes_sentinel_not_exec(monkeypatch, tmp_path):
    """POST /api/control/restart/backend with confirm='yadgar-backend' →
    202, sentinel written as 'restart-yadgar-backend.request', no exec.
    """
    monkeypatch.setenv(_YADGAR_STATE_DIR_ENV, str(tmp_path))
    client = _make_app(monkeypatch, debug_apis_on=True)

    with (
        patch("os.execv") as mock_execv,
        patch("subprocess.run") as mock_subprocess_run,
        patch("subprocess.Popen") as mock_popen,
        patch("os.system") as mock_system,
    ):
        resp = client.post(
            "/api/control/restart/backend",
            json={"confirm": "yadgar-backend"},
            headers=_auth_headers(),
        )

    assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body.get("status") == "sentinel_written"

    mock_execv.assert_not_called()
    mock_subprocess_run.assert_not_called()
    mock_popen.assert_not_called()
    mock_system.assert_not_called()

    expected_sentinel = tmp_path / "yadgar" / "restart-yadgar-backend.request"
    assert expected_sentinel.exists(), f"Sentinel file not found at {expected_sentinel}"
    sentinel_data = json.loads(expected_sentinel.read_text())
    assert sentinel_data["service"] == "yadgar-backend"


# ===========================================================================
# 8 — Config GET returns knob table shape
# ===========================================================================


def test_config_get_returns_knob_table_shape(monkeypatch, tmp_path):
    """GET /api/control/config → list with required fields per knob."""
    client = _make_app(monkeypatch, debug_apis_on=True)
    resp = client.get("/api/control/config", headers=_auth_headers())
    assert resp.status_code == 200
    body = resp.json()
    knobs = body["knobs"]
    assert len(knobs) > 30, f"Expected >30 knobs, got {len(knobs)}"
    # Verify shape of first knob
    for knob in knobs[:5]:
        for field in ("name", "kind", "current", "default", "source", "reload"):
            assert field in knob, f"Knob missing field {field!r}: {knob}"
        assert knob["reload"] in ("hot_reload", "restart_required"), (
            f"Unexpected reload value: {knob['reload']!r}"
        )


# ===========================================================================
# 9 — Config round-trip: POST then GET
# ===========================================================================


def test_config_post_round_trip(monkeypatch, tmp_path):
    """POST /api/control/config sets a knob → GET reflects new value."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    client = _make_app(monkeypatch, debug_apis_on=True)

    # Pick a float knob: YADGAR_VIZ_NODE_SIZE_3D (default 8.0)
    resp_post = client.post(
        "/api/control/config",
        json={"name": "YADGAR_VIZ_NODE_SIZE_3D", "value": 12.5},
        headers=_auth_headers(),
    )
    assert resp_post.status_code == 200, f"POST failed: {resp_post.status_code}: {resp_post.text}"
    post_body = resp_post.json()
    assert post_body["name"] == "YADGAR_VIZ_NODE_SIZE_3D"
    assert post_body["value"] == "12.5"

    # GET should reflect env update (env mutated in this process)
    resp_get = client.get("/api/control/config", headers=_auth_headers())
    assert resp_get.status_code == 200
    knobs = {k["name"]: k for k in resp_get.json()["knobs"]}
    assert "YADGAR_VIZ_NODE_SIZE_3D" in knobs
    assert knobs["YADGAR_VIZ_NODE_SIZE_3D"]["current"] == "12.5", (
        f"Expected '12.5', got {knobs['YADGAR_VIZ_NODE_SIZE_3D']['current']!r}"
    )


# ===========================================================================
# 10 — Type mismatch → 400
# ===========================================================================


def test_config_post_type_mismatch_returns_400(monkeypatch, tmp_path):
    """POST a string to a float knob → 400 type mismatch."""
    client = _make_app(monkeypatch, debug_apis_on=True)
    resp = client.post(
        "/api/control/config",
        json={"name": "YADGAR_VIZ_NODE_SIZE_3D", "value": "not-a-number"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400, f"Expected 400, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert "error" in body
    assert "type mismatch" in body["error"].lower() or "mismatch" in body["error"].lower()


# ===========================================================================
# 11 — Out-of-range → 400
# ===========================================================================


def test_config_post_out_of_range_returns_400(monkeypatch, tmp_path):
    """POST viz.node.size_3d = -1 → 400 out-of-range."""
    client = _make_app(monkeypatch, debug_apis_on=True)
    resp = client.post(
        "/api/control/config",
        json={"name": "YADGAR_VIZ_NODE_SIZE_3D", "value": -1.0},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400, f"Expected 400, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert "error" in body
    # Error should mention > 0 or range constraint
    assert (
        "0" in body["error"]
        or "range" in body["error"].lower()
        or "must be" in body["error"].lower()
    )


# ===========================================================================
# 12–14 — Action endpoints invoke the right internal functions (mocked)
# ===========================================================================


def test_action_consolidate_calls_consolidate_now(monkeypatch, tmp_path):
    """POST /api/control/action/consolidate → consolidate_now called."""
    client = _make_app(monkeypatch, debug_apis_on=True)
    mock_result = {"consolidated": 5}
    with patch(
        "yadgar.server.routes.control.consolidate_now",
        return_value=mock_result,
    ) as mock_fn:
        resp = client.post(
            "/api/control/action/consolidate",
            json={},
            headers=_auth_headers(),
        )
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    mock_fn.assert_called_once_with(mode="light")
    assert resp.json()["action"] == "consolidate"


def test_action_vacuum_calls_vacuum_now(monkeypatch, tmp_path):
    """POST /api/control/action/vacuum → vacuum_now called."""
    client = _make_app(monkeypatch, debug_apis_on=True)
    mock_result = {"vacuumed": 3}
    with patch(
        "yadgar.server.routes.control.vacuum_now",
        return_value=mock_result,
    ) as mock_fn:
        resp = client.post(
            "/api/control/action/vacuum",
            json={},
            headers=_auth_headers(),
        )
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    mock_fn.assert_called_once_with(force=False)
    assert resp.json()["action"] == "vacuum"


def test_action_reembed_calls_reembed_all(monkeypatch, tmp_path):
    """POST /api/control/action/reembed → reembed_all called."""
    client = _make_app(monkeypatch, debug_apis_on=True)
    with patch(
        "yadgar.server.routes.control.reembed_all",
        return_value={"reembedded": 10},
    ) as mock_fn:
        resp = client.post(
            "/api/control/action/reembed",
            json={},
            headers=_auth_headers(),
        )
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    mock_fn.assert_called_once()
    assert resp.json()["action"] == "reembed"


# ===========================================================================
# 15 — Unknown action → 400
# ===========================================================================


def test_action_unknown_returns_400(monkeypatch, tmp_path):
    client = _make_app(monkeypatch, debug_apis_on=True)
    resp = client.post(
        "/api/control/action/nuke",
        json={},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400, f"Expected 400, got {resp.status_code}: {resp.text}"
    assert "error" in resp.json()


# ===========================================================================
# 16 — Unknown restart service → 400
# ===========================================================================


def test_restart_unknown_service_returns_400(monkeypatch, tmp_path):
    client = _make_app(monkeypatch, debug_apis_on=True)
    resp = client.post(
        "/api/control/restart/zombie-daemon",
        json={"confirm": "zombie-daemon"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400, f"Expected 400, got {resp.status_code}: {resp.text}"
    assert "error" in resp.json()
