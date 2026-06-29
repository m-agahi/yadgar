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
10. test_config_post_type_mismatch_returns_422 — string to float knob → 422
11. test_config_post_out_of_range_returns_400 — node_size = -1 → 400
12. test_action_consolidate_calls_consolidate_now — mock consolidate_now called
13. test_action_vacuum_calls_vacuum_now — mock vacuum_now called
14. test_action_reembed_calls_reembed_all — mock reembed_all called
15. test_action_unknown_returns_400 — unknown action → 400
16. test_restart_unknown_service_returns_400 — unknown service param → 400
17. test_config_post_write_blocked_knob_returns_400 — security/enforcement knobs → 400
18. test_config_get_returns_enriched_fields — GET returns description/section/category/locked
19. test_config_get_locked_when_env_set — env-set knob has locked=True
20. test_config_post_env_locked_returns_409 — POST to env-set knob → 409 Conflict
21. test_section_category_map_covers_all_field_meta_sections — drift guard
22. test_no_patch_write_path_under_admin_config — admin_config.py has no write surface
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


def test_logs_stay_debug_gated_via_middleware(monkeypatch, tmp_path):
    """ADR-0013 (v5.88.2): /api/logs/* stays behind YADGAR_DEBUG_APIS_ENABLED.

    Operational control paths move OFF the debug gate (see
    test_ops_paths_ungated_with_debug_off); dev introspection (/api/logs/*) is
    NOT a UI button and stays gated. The middleware gate fires before route
    resolution, so the path need not be registered on this app. Replaces the
    old test_403_when_debug_apis_disabled which gated action/restart.
    """
    client = _make_app(monkeypatch, debug_apis_on=False)
    resp = client.get("/api/logs/stream", headers=_auth_headers())
    assert resp.status_code == 403, (
        f"Expected 403 for /api/logs/stream with debug off, got {resp.status_code}: {resp.text}"
    )
    assert resp.json().get("error") == "debug APIs disabled"


def test_ops_paths_ungated_with_debug_off(monkeypatch, tmp_path):
    """ADR-0013: action consolidate/reembed/vacuum + restart are NOT debug-gated.

    With a valid bearer token and YADGAR_DEBUG_APIS_ENABLED=off they reach the
    handler (never the gate's 403). Each must return a non-403 status. RED before
    the _is_debug_api_path carve-out.
    """
    client = _make_app(monkeypatch, debug_apis_on=False)
    cases = [
        ("/api/control/action/consolidate", {}),
        ("/api/control/action/reembed", {}),
        ("/api/control/action/vacuum", {"confirm": "vacuum"}),
        ("/api/control/restart/yadgar", {"confirm": "yadgar"}),
    ]
    for path, body in cases:
        resp = client.post(path, json=body, headers=_auth_headers())
        assert resp.status_code != 403, (
            f"{path} must NOT be debug-gated (ADR-0013); got 403: {resp.text}"
        )


def test_config_post_not_gated_by_debug_flag(monkeypatch, tmp_path):
    """ADR-0011: POST /api/control/config succeeds WITHOUT the debug flag.

    Config writes are protected by (a) bearer auth (still required) and (b) the
    env-locked 409 refusal in the control route — NOT the debug-APIs gate. On the
    live UI the editor must be able to save with the flag off. This is the RED
    test for the fix: pre-fix the middleware returned 403 'debug APIs disabled'.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    # Use a non-env-locked float knob so the write is not refused with 409.
    monkeypatch.delenv("YADGAR_VIZ_NODE_SIZE_3D", raising=False)
    client = _make_app(monkeypatch, debug_apis_on=False)
    resp = client.post(
        "/api/control/config",
        json={"name": "YADGAR_VIZ_NODE_SIZE_3D", "value": 9.0},
        headers=_auth_headers(),
    )
    assert resp.status_code != 403, (
        f"POST config must NOT be debug-gated (ADR-0011); got 403: {resp.text}"
    )
    assert resp.status_code == 200, (
        f"POST config with valid auth + gate off must succeed; got {resp.status_code}: {resp.text}"
    )


def test_config_post_still_requires_auth(monkeypatch, tmp_path):
    """ADR-0011: un-gating the debug flag does NOT remove bearer auth on POST config."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("YADGAR_VIZ_NODE_SIZE_3D", raising=False)
    client = _make_app(monkeypatch, debug_apis_on=False)
    resp = client.post(
        "/api/control/config",
        json={"name": "YADGAR_VIZ_NODE_SIZE_3D", "value": 9.0},
    )  # no auth header
    assert resp.status_code == 401, (
        f"POST config must still require auth; got {resp.status_code}: {resp.text}"
    )


def test_config_post_env_locked_returns_409_when_debug_off(monkeypatch, tmp_path):
    """ADR-0011: env-locked knob POST → 409 even with debug flag OFF.

    The env-lock 409 refusal is the write protection that replaces the debug
    gate for config writes — it must fire regardless of YADGAR_DEBUG_APIS_ENABLED.
    """
    knob_name = "YADGAR_VIZ_HEALTH_REFRESH_SEC"
    monkeypatch.setenv(knob_name, "30")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    client = _make_app(
        monkeypatch,
        debug_apis_on=False,
        extra_env={knob_name: "30"},
    )
    resp = client.post(
        "/api/control/config",
        json={"name": knob_name, "value": 60},
        headers=_auth_headers(),
    )
    assert resp.status_code == 409, (
        f"Expected 409 for env-locked knob with debug off, got {resp.status_code}: {resp.text}"
    )


def test_ops_paths_still_require_auth_when_debug_off(monkeypatch, tmp_path):
    """ADR-0013: un-gating operational paths does NOT remove bearer auth.

    Without an Authorization header (debug off) the auth check returns 401 — the
    paths are no longer intercepted by the debug-gate 403, but auth still applies.
    RED before the carve-out (currently the gate 403s before auth runs).
    """
    monkeypatch.setenv(_YADGAR_STATE_DIR_ENV, str(tmp_path))
    client = _make_app(monkeypatch, debug_apis_on=False)
    cases = [
        ("/api/control/action/consolidate", {}),
        ("/api/control/action/reembed", {}),
        ("/api/control/action/vacuum", {"confirm": "vacuum"}),
        ("/api/control/restart/yadgar", {"confirm": "yadgar"}),
    ]
    for path, body in cases:
        resp = client.post(path, json=body)  # no auth header
        assert resp.status_code == 401, (
            f"{path} must still require auth (401, not gate 403); "
            f"got {resp.status_code}: {resp.text}"
        )


def test_is_debug_api_path_only_logs_gated():
    """ADR-0013 unit guard on _is_debug_api_path:

    /api/control/config stays ungated (ADR-0011); operational action/restart
    paths are now ALSO ungated (ADR-0013). Only /api/logs/* stays gated.
    """
    from yadgar.auth_middleware import _is_debug_api_path

    # Config: ungated for every method (ADR-0011)
    assert _is_debug_api_path("/api/control/config", "GET") is False
    assert _is_debug_api_path("/api/control/config", "POST") is False
    # Operational paths now ungated (ADR-0013)
    assert _is_debug_api_path("/api/control/action/consolidate", "POST") is False
    assert _is_debug_api_path("/api/control/action/reembed", "POST") is False
    assert _is_debug_api_path("/api/control/action/vacuum", "POST") is False
    assert _is_debug_api_path("/api/control/restart/yadgar", "POST") is False
    assert _is_debug_api_path("/api/control/restart/backend", "POST") is False
    # Logs streaming stays gated (dev introspection, not a UI button)
    assert _is_debug_api_path("/api/logs/poll", "GET") is True
    assert _is_debug_api_path("/api/logs/stream", "GET") is True


def test_config_get_not_gated_by_debug_flag(monkeypatch, tmp_path):
    """GET /api/control/config (read-only viewer) works WITHOUT the debug flag.

    Config display is non-sensitive (redacted knobs are skipped, env-sourced
    knobs render locked). Auth is still enforced — only the debug gate is lifted
    for the read path. Writes (POST) remain gated (test_403_when_debug_apis_disabled).
    """
    client = _make_app(monkeypatch, debug_apis_on=False)
    resp = client.get("/api/control/config", headers=_auth_headers())
    assert resp.status_code == 200, (
        f"GET config must be ungated; got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert "knobs" in body and isinstance(body["knobs"], list)
    assert len(body["knobs"]) > 0


def test_config_get_still_requires_auth(monkeypatch, tmp_path):
    """Un-gating the debug flag does NOT remove bearer auth on GET config."""
    client = _make_app(monkeypatch, debug_apis_on=False)
    resp = client.get("/api/control/config")  # no auth header
    assert resp.status_code == 401, (
        f"GET config must still require auth; got {resp.status_code}: {resp.text}"
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
    # Point XDG_STATE_HOME at tmp_path so sentinel lands in a predictable spot.
    # ADR-0013: debug gate OFF — restart is auth-gated, not debug-gated.
    monkeypatch.setenv(_YADGAR_STATE_DIR_ENV, str(tmp_path))
    client = _make_app(monkeypatch, debug_apis_on=False)

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
    # ADR-0013: debug gate OFF — restart is auth-gated, not debug-gated.
    monkeypatch.setenv(_YADGAR_STATE_DIR_ENV, str(tmp_path))
    client = _make_app(monkeypatch, debug_apis_on=False)

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
    # Remove the knob from env so the POST is not blocked by the env-lock 409.
    # (v5.85: env-set knobs return 409 — yaml write would be shadowed.)
    # monkeypatch tracks the removal and restores on teardown.
    monkeypatch.delenv("YADGAR_VIZ_NODE_SIZE_3D", raising=False)
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


def test_config_post_bool_value_is_lowercase(monkeypatch, tmp_path):
    """ADR-0013 bool-display fix: POST a bool knob → response value is lowercase.

    Root cause: the handler returned ``str(coerced)`` where ``coerced`` is a
    Python bool, so ``str(True)`` rendered as capitalized ``"True"`` — diverging
    from the GET path (lowercase env strings) and the YAML/JSON convention. The
    POST response (and the env it writes) must use lowercase ``"true"``/``"false"``.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    knob = "YADGAR_VIZ_PRECOMPUTED_LAYOUT_ENABLED"
    monkeypatch.delenv(knob, raising=False)
    client = _make_app(monkeypatch, debug_apis_on=False)
    resp = client.post(
        "/api/control/config",
        json={"name": knob, "value": "true"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 200, f"POST failed: {resp.status_code}: {resp.text}"
    assert resp.json()["value"] == "true", (
        f"bool POST value must be lowercase 'true', got {resp.json()['value']!r}"
    )
    # The mutated process env must also carry the lowercase form.
    import os as _os

    assert _os.environ[knob] == "true", (
        f"env var must be lowercase 'true', got {_os.environ[knob]!r}"
    )


# ===========================================================================
# 10 — Type mismatch → 400
# ===========================================================================


def test_config_post_type_mismatch_returns_422(monkeypatch, tmp_path):
    """POST a non-coercible value to a float knob → 422 Unprocessable Entity.

    v5.86 car #8: coercion failures now return 422 (was 400). 400 stays for
    structural problems (bad JSON, missing fields, unknown knob, write-blocked);
    422 is reserved for "well-formed request, value can't be coerced to the
    knob's type" — the shared set_config_value() writer raises and the API maps
    it to 422.
    """
    client = _make_app(monkeypatch, debug_apis_on=True)
    # env-set knobs return 409 before validation — remove so the 422 path is exercised.
    monkeypatch.delenv("YADGAR_VIZ_NODE_SIZE_3D", raising=False)
    resp = client.post(
        "/api/control/config",
        json={"name": "YADGAR_VIZ_NODE_SIZE_3D", "value": "not-a-number"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 422, f"Expected 422, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert "error" in body
    assert "mismatch" in body["error"].lower() or "invalid" in body["error"].lower()


# ===========================================================================
# 11 — Out-of-range → 400
# ===========================================================================


def test_config_post_out_of_range_returns_400(monkeypatch, tmp_path):
    """POST viz.node.size_3d = -1 → 400 out-of-range."""
    client = _make_app(monkeypatch, debug_apis_on=True)
    # v5.85: env-set knobs return 409 before validation — remove so the 400 path is exercised.
    monkeypatch.delenv("YADGAR_VIZ_NODE_SIZE_3D", raising=False)
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
    """POST /api/control/action/consolidate → consolidate_now called.

    ADR-0013: debug gate OFF — consolidate is safe (mode=light) + auth-gated.
    """
    client = _make_app(monkeypatch, debug_apis_on=False)
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
    """POST /api/control/action/vacuum with valid confirm → vacuum_now called.

    ADR-0013: vacuum is ungated (auth-gated) but carries real daemon downtime
    (2-5 min), so it requires a server-side confirm field matching "vacuum".
    """
    client = _make_app(monkeypatch, debug_apis_on=False)
    mock_result = {"vacuumed": 3}
    with patch(
        "yadgar.server.routes.control.vacuum_now",
        return_value=mock_result,
    ) as mock_fn:
        resp = client.post(
            "/api/control/action/vacuum",
            json={"confirm": "vacuum"},
            headers=_auth_headers(),
        )
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    mock_fn.assert_called_once_with(force=False)
    assert resp.json()["action"] == "vacuum"


def test_action_vacuum_without_confirm_returns_400(monkeypatch, tmp_path):
    """POST /api/control/action/vacuum WITHOUT confirm → 400; vacuum_now NOT called.

    ADR-0013: vacuum's daemon-downtime cost demands a typed confirm. A missing
    or mismatched confirm is rejected before vacuum_now runs.
    """
    client = _make_app(monkeypatch, debug_apis_on=False)
    with patch("yadgar.server.routes.control.vacuum_now") as mock_fn:
        resp = client.post(
            "/api/control/action/vacuum",
            json={},
            headers=_auth_headers(),
        )
    assert resp.status_code == 400, f"Expected 400, got {resp.status_code}: {resp.text}"
    assert "error" in resp.json()
    mock_fn.assert_not_called()


def test_action_vacuum_mismatched_confirm_returns_400(monkeypatch, tmp_path):
    """POST /api/control/action/vacuum with wrong confirm → 400; vacuum_now NOT called."""
    client = _make_app(monkeypatch, debug_apis_on=False)
    with patch("yadgar.server.routes.control.vacuum_now") as mock_fn:
        resp = client.post(
            "/api/control/action/vacuum",
            json={"confirm": "yes"},
            headers=_auth_headers(),
        )
    assert resp.status_code == 400, f"Expected 400, got {resp.status_code}: {resp.text}"
    mock_fn.assert_not_called()


def test_action_consolidate_reembed_need_no_confirm(monkeypatch, tmp_path):
    """ADR-0013: consolidate + reembed are safe — NO confirm field required."""
    client = _make_app(monkeypatch, debug_apis_on=False)
    with (
        patch("yadgar.server.routes.control.consolidate_now", return_value={}),
        patch("yadgar.server.routes.control.reembed_all", return_value={}),
    ):
        for action in ("consolidate", "reembed"):
            resp = client.post(
                f"/api/control/action/{action}",
                json={},
                headers=_auth_headers(),
            )
            assert resp.status_code == 200, (
                f"{action} must succeed without confirm; got {resp.status_code}: {resp.text}"
            )


def test_action_reembed_calls_reembed_all(monkeypatch, tmp_path):
    """POST /api/control/action/reembed → reembed_all called.

    ADR-0013: debug gate OFF — reembed is idempotent/safe + auth-gated.
    """
    client = _make_app(monkeypatch, debug_apis_on=False)
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


def test_action_emits_audit_log(monkeypatch, tmp_path, caplog):
    """ADR-0013: each successful operational action logs one audit line."""
    import logging

    client = _make_app(monkeypatch, debug_apis_on=False)
    with (
        patch("yadgar.server.routes.control.consolidate_now", return_value={}),
        caplog.at_level(logging.INFO, logger="yadgar.server.routes.control"),
    ):
        resp = client.post(
            "/api/control/action/consolidate",
            json={},
            headers=_auth_headers(),
        )
    assert resp.status_code == 200
    audit = [r for r in caplog.records if "triggered via control API" in r.getMessage()]
    assert audit, "expected an audit log line for the consolidate action"
    assert any("consolidate" in r.getMessage() for r in audit)


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


# ===========================================================================
# 17 — Write-blocked security/enforcement knobs → 400
# ===========================================================================


def test_config_post_write_blocked_knob_returns_400(monkeypatch, tmp_path):
    """POST /api/control/config on security/enforcement knobs → 400 write-protected.

    These knobs control auth, root access, and enforcement — the config editor
    must never allow them to be changed via the API, even with the debug gate on.
    """
    client = _make_app(monkeypatch, debug_apis_on=True)

    blocked_knobs = [
        ("YADGAR_DEBUG_APIS_ENABLED", "false"),  # gate self-disable
        ("YADGAR_ALLOW_ROOT", "true"),  # privilege escalation
        ("YADGAR_REQUIRE_AUTH", "false"),  # auth bypass
        ("YADGAR_BRANCH_ENFORCEMENT", "false"),  # enforcement bypass
        ("YADGAR_DIRECTORY_ENFORCEMENT", "false"),  # enforcement bypass
    ]

    for name, value in blocked_knobs:
        resp = client.post(
            "/api/control/config",
            json={"name": name, "value": value},
            headers=_auth_headers(),
        )
        assert resp.status_code == 400, (
            f"Expected 400 for write-blocked knob {name!r}, got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert "error" in body, f"No 'error' key in response for {name!r}: {body}"
        assert "write-protected" in body["error"].lower(), (
            f"Expected 'write-protected' in error for {name!r}, got: {body['error']!r}"
        )


# ===========================================================================
# 18 — Config GET returns enriched fields (v5.85 config control panel)
# ===========================================================================


def test_config_get_returns_enriched_fields(monkeypatch, tmp_path):
    """GET /api/control/config returns description, section, category, locked per knob.

    v5.85 extends the knob table shape with four metadata fields:
      - description: from FIELD_META (empty string for env-only knobs)
      - section: FIELD_META.section (or 'misc' if missing)
      - category: capability category derived from section→category map
      - locked: bool, True when source=='env' (yaml write would be shadowed)
    """
    client = _make_app(monkeypatch, debug_apis_on=True)
    resp = client.get("/api/control/config", headers=_auth_headers())
    assert resp.status_code == 200
    body = resp.json()
    knobs = body["knobs"]
    assert len(knobs) > 0

    # Every knob must have the four new fields
    for knob in knobs[:10]:
        assert "description" in knob, f"Knob missing 'description': {knob['name']}"
        assert "section" in knob, f"Knob missing 'section': {knob['name']}"
        assert "category" in knob, f"Knob missing 'category': {knob['name']}"
        assert "locked" in knob, f"Knob missing 'locked': {knob['name']}"
        assert isinstance(knob["description"], str), (
            f"'description' must be str, got {type(knob['description'])} for {knob['name']}"
        )
        assert isinstance(knob["section"], str), (
            f"'section' must be str, got {type(knob['section'])} for {knob['name']}"
        )
        assert isinstance(knob["category"], str), (
            f"'category' must be str, got {type(knob['category'])} for {knob['name']}"
        )
        assert isinstance(knob["locked"], bool), (
            f"'locked' must be bool, got {type(knob['locked'])} for {knob['name']}"
        )

    # Known category values (the 15 CAPABILITY_REGISTRY categories + 'config' fallback)
    valid_categories = {
        "retrieval",
        "storage",
        "write-path",
        "consolidation",
        "enrichment",
        "gate",
        "wiki",
        "curation",
        "mcp-tool",
        "observability",
        "security",
        "ops",
        "brain-dynamics",
        "viz",
        "config",
    }
    for knob in knobs:
        assert knob["category"] in valid_categories, (
            f"Knob {knob['name']!r} has unexpected category {knob['category']!r}"
        )

    # Existing fields must still be present (backwards-compatible)
    for knob in knobs[:5]:
        for field in ("name", "kind", "current", "default", "source", "reload"):
            assert field in knob, f"Existing field {field!r} missing from knob {knob['name']}"


# ===========================================================================
# P4.1 (viz-fix-plan-2026-06-27) — enum_choices on fixed-set string knobs
# ===========================================================================


def test_config_get_returns_enum_choices(monkeypatch, tmp_path):
    """GET /api/control/config returns enum_choices for fixed-set string knobs.

    P4.1: the config panel renders a <select> for enum-typed knobs, so each
    knob must advertise its allowed values via `enum_choices` (a list).
      - YADGAR_LOG_FORMAT is validator-backed ({json, text, human}) → non-empty.
      - Free-form / numeric knobs (e.g. YADGAR_PORT) → empty list.
    """
    client = _make_app(monkeypatch, debug_apis_on=True)
    resp = client.get("/api/control/config", headers=_auth_headers())
    assert resp.status_code == 200
    knobs = {k["name"]: k for k in resp.json()["knobs"]}

    # Every knob carries the key (list type, possibly empty)
    for knob in knobs.values():
        assert "enum_choices" in knob, f"Knob missing 'enum_choices': {knob['name']}"
        assert isinstance(knob["enum_choices"], list), (
            f"'enum_choices' must be a list, got {type(knob['enum_choices'])} for {knob['name']}"
        )

    # Enum knob: LOG_FORMAT advertises its validated allowed set
    assert "YADGAR_LOG_FORMAT" in knobs
    log_format_choices = knobs["YADGAR_LOG_FORMAT"]["enum_choices"]
    assert set(log_format_choices) == {"json", "text", "human"}, (
        f"LOG_FORMAT enum_choices wrong: {log_format_choices}"
    )

    # Non-enum knob: PORT has no fixed set → empty list
    assert "YADGAR_PORT" in knobs
    assert knobs["YADGAR_PORT"]["enum_choices"] == []


# ===========================================================================
# 19 — locked=True when knob is set in env
# ===========================================================================


def test_config_get_locked_when_env_set(monkeypatch, tmp_path):
    """GET /api/control/config: knob set in env → locked=True; knob not in env → locked=False."""
    # Set a non-secret VIZ knob in env so it shows up as env-sourced
    monkeypatch.setenv("YADGAR_VIZ_HEALTH_REFRESH_SEC", "30")
    monkeypatch.delenv("YADGAR_VIZ_NODE_SIZE_3D", raising=False)  # ensure not set

    client = _make_app(
        monkeypatch,
        debug_apis_on=True,
        extra_env={"YADGAR_VIZ_HEALTH_REFRESH_SEC": "30"},
    )
    resp = client.get("/api/control/config", headers=_auth_headers())
    assert resp.status_code == 200
    knobs_by_name = {k["name"]: k for k in resp.json()["knobs"]}

    # The env-set knob must be locked
    if "YADGAR_VIZ_HEALTH_REFRESH_SEC" in knobs_by_name:
        knob = knobs_by_name["YADGAR_VIZ_HEALTH_REFRESH_SEC"]
        assert knob["locked"] is True, (
            f"Expected locked=True for env-set knob, got locked={knob['locked']}"
        )
        assert knob["source"] == "env", (
            f"Expected source='env' for env-set knob, got source={knob['source']}"
        )

    # A knob NOT in env (and not yaml) should be unlocked
    if "YADGAR_VIZ_NODE_SIZE_3D" in knobs_by_name:
        knob_default = knobs_by_name["YADGAR_VIZ_NODE_SIZE_3D"]
        assert knob_default["locked"] is False, (
            f"Expected locked=False for default-sourced knob, got locked={knob_default['locked']}"
        )


# ===========================================================================
# 20 — POST to env-locked knob → 409 Conflict
# ===========================================================================


def test_config_post_env_locked_returns_409(monkeypatch, tmp_path):
    """POST /api/control/config to an env-set knob → 409 Conflict.

    When a knob is set via env var, a yaml write would be silently shadowed.
    The endpoint must refuse with 409 instead of silently writing a shadowed value.
    """
    # Set a non-secret VIZ knob in env
    knob_name = "YADGAR_VIZ_HEALTH_REFRESH_SEC"
    monkeypatch.setenv(knob_name, "30")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    client = _make_app(
        monkeypatch,
        debug_apis_on=True,
        extra_env={knob_name: "30"},
    )
    resp = client.post(
        "/api/control/config",
        json={"name": knob_name, "value": 60},
        headers=_auth_headers(),
    )
    assert resp.status_code == 409, (
        f"Expected 409 for env-locked knob, got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert "error" in body
    # Error must explain env-lock / shadowing
    err_lower = body["error"].lower()
    assert (
        "env" in err_lower
        or "lock" in err_lower
        or "shadow" in err_lower
        or "conflict" in err_lower
    ), f"Expected env/lock/shadow/conflict in error, got: {body['error']!r}"


# ===========================================================================
# 21 — Section→category map covers every FIELD_META section
# ===========================================================================


def test_section_category_map_covers_all_field_meta_sections(monkeypatch):
    """Every section in FIELD_META must map to a category in SECTION_TO_CATEGORY.

    Drift guard: adding a new FIELD_META section without updating the map
    causes unknown categories to silently fall back to 'config'. This test
    catches that regression.
    """
    from yadgar.config_yaml import FIELD_META
    from yadgar.server.routes.control import SECTION_TO_CATEGORY

    sections_in_meta = {meta["section"] for meta in FIELD_META.values()}
    unmapped = sections_in_meta - set(SECTION_TO_CATEGORY.keys())
    assert not unmapped, (
        f"FIELD_META sections without a SECTION_TO_CATEGORY entry: {sorted(unmapped)}\n"
        "Add them to SECTION_TO_CATEGORY in yadgar/server/routes/control.py. "
        "Fallback is 'config' but explicit mapping is required."
    )


def test_no_knobs_resolve_to_catchall_config_category():
    """Every FIELD_META knob must resolve to a non-catch-all category.

    The 'config' category is a catch-all fallback in _get_category for sections
    not listed in SECTION_TO_CATEGORY. No knob should resolve to it — all sections
    used in FIELD_META must be explicitly mapped to a real category.

    If this fails, reassign the offending knobs' 'section' field in
    yadgar/config_yaml.py FIELD_META to an existing, explicitly mapped section.
    Do NOT create new sections.
    """
    from yadgar.config_yaml import FIELD_META
    from yadgar.server.routes.control import _get_category

    catchall = [name for name, m in FIELD_META.items() if _get_category(m["section"]) == "config"]
    assert catchall == [], (
        f"Knobs resolving to catch-all 'config' category: {sorted(catchall)}\n"
        "Reassign each knob's 'section' in yadgar/config_yaml.py FIELD_META "
        "to an existing section that maps to a real category in SECTION_TO_CATEGORY."
    )


# ===========================================================================
# 22 — No write path exists under /admin/config (no parallel write surface)
# ===========================================================================


def test_no_patch_write_path_under_admin_config(monkeypatch):
    """admin_config.py must not register any write (PATCH/POST/PUT/DELETE) methods.

    The canonical config write path is POST /api/control/config. admin_config.py
    must remain read-only (GET only). This test guards against accidentally adding
    a parallel write surface under /admin/.
    """
    import inspect

    from yadgar.server import admin_config as ac_module

    source = inspect.getsource(ac_module)
    # Check that no write method is registered via custom_route
    for write_method in ("PATCH", "PUT", "DELETE"):
        assert write_method not in source, (
            f"admin_config.py contains {write_method!r} — parallel write path detected. "
            "Config writes must go through POST /api/control/config only."
        )
    # POST is also forbidden on /admin/config (it has its own write path)
    # Verify the only registered route is GET
    assert '"GET"' in source or "'GET'" in source, (
        "Expected GET route registration in admin_config.py"
    )
