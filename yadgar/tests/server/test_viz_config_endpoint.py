"""v5.11.0 — /api/viz/config endpoint tests.

TDD — written BEFORE implementation.

Tests:
1. test_viz_config_endpoint_returns_yaml_values   — custom config.yaml values round-trip
2. test_viz_config_endpoint_returns_defaults_when_unset — minimal / no config → hardcoded defaults
3. test_viz_config_endpoint_auth_required         — 401 without bearer token
4. test_viz_config_registry_complete              — every VIZ_* Settings field in FIELD_META + _REGISTRY
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_auth_client(token: str, monkeypatch: pytest.MonkeyPatch):
    """Full ASGI app wrapped in BearerAuthMiddleware (same pattern as test_session_context_endpoint)."""
    monkeypatch.setenv("YADGAR_REQUIRE_AUTH", "1")
    monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", token)

    from starlette.testclient import TestClient

    from yadgar.core import server as _server
    from yadgar.core.auth_middleware import BearerAuthMiddleware

    asgi_app = _server.mcp_server.streamable_http_app()
    return TestClient(BearerAuthMiddleware(asgi_app), raise_server_exceptions=False)


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("viz_config_endpoint")
    from yadgar.core import server

    db_path = str(tmp_path / "test.db")
    server.init_engines(db_path=db_path, embedding_model="all-MiniLM-L6-v2")
    yield
    server.shutdown()


# ---------------------------------------------------------------------------
# Test 1 — custom config.yaml values round-trip
# ---------------------------------------------------------------------------


def test_viz_config_endpoint_returns_yaml_values(tmp_path, monkeypatch):
    """Endpoint returns values from config.yaml when set."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "VIZ_NODE_SIZE_3D: 12\n"
        "VIZ_PHYSICS_CHARGE_STRENGTH: -20\n"
        "VIZ_EDGE_COLOR_SEMANTIC: '#ff0000'\n"
        "VIZ_SEARCH_DIM_OPACITY: 0.5\n"
    )
    monkeypatch.setenv("YADGAR_CONFIG_FILE", str(config_file))
    monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", "tok")
    monkeypatch.setenv("YADGAR_REQUIRE_AUTH", "1")
    # Defensive: clear leaked VIZ env vars so yaml values take precedence.
    monkeypatch.delenv("YADGAR_VIZ_NODE_SIZE_3D", raising=False)

    from starlette.testclient import TestClient

    # Clear settings cache so new env / config file is picked up
    from yadgar._shared.config import get_settings
    from yadgar.core import server as _server
    from yadgar.core.auth_middleware import BearerAuthMiddleware

    get_settings.cache_clear()

    asgi_app = _server.mcp_server.streamable_http_app()
    client = TestClient(BearerAuthMiddleware(asgi_app), raise_server_exceptions=False)
    resp = client.get("/api/viz/config", headers={"Authorization": "Bearer tok"})
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    data = resp.json()

    assert data["node"]["size_3d"] == 12
    assert data["physics"]["charge_strength"] == -20
    assert data["search"]["dim_opacity"] == 0.5

    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Test 2 — defaults returned when config.yaml has no viz keys
# ---------------------------------------------------------------------------


def _get_default_config(tmp_path, monkeypatch) -> dict:
    """Helper: build a client with empty config.yaml and return /api/viz/config data."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("")  # empty
    monkeypatch.setenv("YADGAR_CONFIG_FILE", str(config_file))
    monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", "tok2")
    monkeypatch.setenv("YADGAR_REQUIRE_AUTH", "1")
    # Defensive: clear any VIZ env vars that a co-worker test may have leaked into
    # os.environ directly (e.g. test_config_post_round_trip via control.py:337).
    monkeypatch.delenv("YADGAR_VIZ_NODE_SIZE_3D", raising=False)

    from starlette.testclient import TestClient

    from yadgar._shared.config import get_settings
    from yadgar.core import server as _server
    from yadgar.core.auth_middleware import BearerAuthMiddleware

    get_settings.cache_clear()
    asgi_app = _server.mcp_server.streamable_http_app()
    client = TestClient(BearerAuthMiddleware(asgi_app), raise_server_exceptions=False)
    resp = client.get("/api/viz/config", headers={"Authorization": "Bearer tok2"})
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    get_settings.cache_clear()
    return resp.json()


def test_viz_config_defaults_physics_and_layout(tmp_path, monkeypatch):
    """Default physics and layout values match v5.10.11 hardcoded values."""
    data = _get_default_config(tmp_path, monkeypatch)
    assert data["node"]["size_3d"] == 8
    assert data["physics"]["charge_strength"] == -18  # v5.50.0 Variant C (was -12)
    assert data["physics"]["link_distance_2d"] == 30
    assert data["physics"]["link_distance_3d"] == 36
    assert data["layout"]["auto_zoom_fit_tick_threshold"] == 80
    assert data["layout"]["zoom_fit_padding"] == 50
    assert data["layout"]["zoom_fit_transition_ms"] == 800


def test_viz_config_defaults_search_and_colors(tmp_path, monkeypatch):
    """Default search + edge + category colors match v5.10.11 hardcoded values."""
    data = _get_default_config(tmp_path, monkeypatch)
    assert data["search"]["dim_opacity"] == pytest.approx(0.18)
    assert data["search"]["match_color"] == "#ffffff"
    assert data["search"]["pinned_color"] == "#ffd700"
    assert data["edge"]["color"]["temporal"] == "#6e40c9"
    assert data["edge"]["color"]["transition"] == "#3fb950"
    assert data["edge"]["color"]["wiki_crossref"] == "#d2a8ff"
    assert data["edge"]["color"]["memory_wiki"] == "#ffa657"
    assert data["node"]["category_colors"]["architecture"] == "#58a6ff"
    assert data["node"]["category_colors"]["decision"] == "#ffa657"


# ---------------------------------------------------------------------------
# Test 3 — auth required
# ---------------------------------------------------------------------------


def test_viz_config_endpoint_auth_required(tmp_path, monkeypatch):
    """/api/viz/config returns 401 without Authorization header."""
    client = _make_auth_client("secret-token", monkeypatch)
    resp = client.get("/api/viz/config")
    assert resp.status_code == 401, f"Expected 401, got {resp.status_code}"


# ---------------------------------------------------------------------------
# Test 4 — I25 three-way registry completeness
# ---------------------------------------------------------------------------


def test_viz_config_registry_complete():
    """Every VIZ_* Settings field must appear in FIELD_META AND _REGISTRY.

    This is the I25 ratchet for viz knobs.
    """
    from yadgar._shared.config import Settings
    from yadgar._shared.config.config_registry import _REGISTRY
    from yadgar._shared.config.config_yaml import FIELD_META

    registry_names = {e.name for e in _REGISTRY}

    viz_fields = [
        name
        for name in Settings.model_fields
        if name.startswith("VIZ_") and name not in {"VIZ_PROXY", "VIZ_HEALTH_REFRESH_SEC"}
    ]

    assert viz_fields, "No VIZ_* knob fields found in Settings — implementation missing"

    missing_field_meta = []
    missing_registry = []
    for field in viz_fields:
        key = field.lower()
        if key not in FIELD_META:
            missing_field_meta.append(field)
        env_name = f"YADGAR_{field}"
        if env_name not in registry_names:
            missing_registry.append(field)

    assert not missing_field_meta, f"VIZ_* fields missing from FIELD_META: {missing_field_meta}"
    assert not missing_registry, f"VIZ_* fields missing from _REGISTRY: {missing_registry}"
