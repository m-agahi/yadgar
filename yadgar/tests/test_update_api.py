"""v5.48.0 — TDD scaffolding (RED phase): /api/control/update HTTP route.

Tests for:
- yadgar/server/routes/control_update.py — POST /api/control/update
- Auth gating (BearerAuthMiddleware via /api/ prefix)
- YADGAR_DEBUG_APIS_ENABLED gate
- Response shape for action=check
- 400 on action=install + can_self_install=False
- 503 on PyPI unreachable

All httpx and subprocess calls are mocked.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

_TOKEN = "test-tok-update"


def _make_update_app(monkeypatch, require_auth: bool = True, debug_apis: str = "on"):
    """Build a minimal Starlette app with the /api/control/update route and BearerAuth."""
    if require_auth:
        monkeypatch.setenv("YADGAR_REQUIRE_AUTH", "1")
        monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", _TOKEN)
    else:
        monkeypatch.setenv("YADGAR_REQUIRE_AUTH", "0")

    monkeypatch.setenv("YADGAR_UPDATE_DEBUG_APIS_ENABLED", debug_apis)

    from yadgar.auth_middleware import BearerAuthMiddleware
    from yadgar.server.routes.control_update import control_update_handler

    app = BearerAuthMiddleware(
        Starlette(routes=[Route("/api/control/update", control_update_handler, methods=["POST"])])
    )
    return app


def _auth_headers():
    return {"Authorization": f"Bearer {_TOKEN}"}


# ---------------------------------------------------------------------------
# Auth tests
# ---------------------------------------------------------------------------


class TestControlUpdateAuth:
    def test_no_auth_returns_401(self, monkeypatch):
        """POST without bearer token → 401."""
        app = _make_update_app(monkeypatch)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/control/update", json={})
        assert resp.status_code == 401, f"Expected 401, got {resp.status_code}"

    def test_invalid_token_returns_401(self, monkeypatch):
        """POST with wrong bearer token → 401."""
        app = _make_update_app(monkeypatch)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/api/control/update",
            json={},
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 401, f"Expected 401, got {resp.status_code}"

    def test_no_debug_gate_returns_403(self, monkeypatch):
        """POST without YADGAR_DEBUG_APIS_ENABLED=on → 403."""
        app = _make_update_app(monkeypatch, debug_apis="off")
        client = TestClient(app, raise_server_exceptions=False)

        mock_result = MagicMock()
        mock_result.available_version = "9.99.0"
        mock_result.checked_at = "2026-06-07T00:00:00Z"

        with (
            patch("yadgar.update.check.probe_latest_version", return_value=mock_result),
            patch("yadgar.update.install_methods.detect_install_method", return_value="pipx"),
            patch(
                "yadgar.update.install_methods.upgrade_command", return_value="pipx upgrade yadgar"
            ),
            patch("yadgar.update.install_methods.can_self_install", return_value=True),
        ):
            resp = client.post(
                "/api/control/update",
                json={"action": "check"},
                headers=_auth_headers(),
            )
        assert resp.status_code == 403, f"Expected 403, got {resp.status_code}"


# ---------------------------------------------------------------------------
# action=check happy path
# ---------------------------------------------------------------------------


class TestControlUpdateCheckAction:
    def test_action_check_returns_200_with_expected_shape(self, monkeypatch):
        """POST action=check with valid auth → 200 with all expected JSON keys."""
        app = _make_update_app(monkeypatch)
        client = TestClient(app, raise_server_exceptions=False)

        mock_result = MagicMock()
        mock_result.available_version = "9.99.0"
        mock_result.checked_at = "2026-06-07T00:00:00Z"

        with (
            patch("yadgar.update.check.probe_latest_version", return_value=mock_result),
            patch("yadgar.update.install_methods.detect_install_method", return_value="pipx"),
            patch(
                "yadgar.update.install_methods.upgrade_command", return_value="pipx upgrade yadgar"
            ),
            patch("yadgar.update.install_methods.can_self_install", return_value=True),
        ):
            resp = client.post(
                "/api/control/update",
                json={"action": "check"},
                headers=_auth_headers(),
            )

        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()

        required_keys = {
            "current_version",
            "available_version",
            "update_available",
            "install_method",
            "upgrade_command",
            "release_notes_url",
            "checked_at",
        }
        missing = required_keys - set(body.keys())
        assert not missing, f"Missing keys in response: {missing}\nGot: {list(body.keys())}"

    def test_action_check_update_available_true_when_versions_differ(self, monkeypatch):
        """update_available=True when available != current."""
        from yadgar import __version__

        app = _make_update_app(monkeypatch)
        client = TestClient(app, raise_server_exceptions=False)

        mock_result = MagicMock()
        mock_result.available_version = "9.99.0"  # definitely != current
        mock_result.checked_at = "2026-06-07T00:00:00Z"

        with (
            patch("yadgar.update.check.probe_latest_version", return_value=mock_result),
            patch("yadgar.update.install_methods.detect_install_method", return_value="pipx"),
            patch(
                "yadgar.update.install_methods.upgrade_command", return_value="pipx upgrade yadgar"
            ),
            patch("yadgar.update.install_methods.can_self_install", return_value=True),
        ):
            resp = client.post(
                "/api/control/update",
                json={"action": "check"},
                headers=_auth_headers(),
            )

        body = resp.json()
        assert body["update_available"] is True
        assert body["current_version"] == __version__

    def test_release_notes_url_contains_available_version(self, monkeypatch):
        """release_notes_url includes the available_version."""
        app = _make_update_app(monkeypatch)
        client = TestClient(app, raise_server_exceptions=False)

        mock_result = MagicMock()
        mock_result.available_version = "9.99.0"
        mock_result.checked_at = "2026-06-07T00:00:00Z"

        with (
            patch("yadgar.update.check.probe_latest_version", return_value=mock_result),
            patch("yadgar.update.install_methods.detect_install_method", return_value="pipx"),
            patch(
                "yadgar.update.install_methods.upgrade_command", return_value="pipx upgrade yadgar"
            ),
            patch("yadgar.update.install_methods.can_self_install", return_value=True),
        ):
            resp = client.post(
                "/api/control/update",
                json={"action": "check"},
                headers=_auth_headers(),
            )

        body = resp.json()
        assert "9.99.0" in body["release_notes_url"]
        assert "pypi.org" in body["release_notes_url"]

    def test_default_action_is_check(self, monkeypatch):
        """POST with no action field defaults to action=check."""
        app = _make_update_app(monkeypatch)
        client = TestClient(app, raise_server_exceptions=False)

        mock_result = MagicMock()
        mock_result.available_version = "9.99.0"
        mock_result.checked_at = "2026-06-07T00:00:00Z"

        with (
            patch("yadgar.update.check.probe_latest_version", return_value=mock_result),
            patch("yadgar.update.install_methods.detect_install_method", return_value="pipx"),
            patch(
                "yadgar.update.install_methods.upgrade_command", return_value="pipx upgrade yadgar"
            ),
            patch("yadgar.update.install_methods.can_self_install", return_value=True),
        ):
            # Empty body — no "action" field
            resp = client.post(
                "/api/control/update",
                json={},
                headers=_auth_headers(),
            )

        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# action=install
# ---------------------------------------------------------------------------


class TestControlUpdateInstallAction:
    def test_install_on_non_self_installable_returns_400(self, monkeypatch):
        """POST action=install + can_self_install=False → 400."""
        app = _make_update_app(monkeypatch)
        client = TestClient(app, raise_server_exceptions=False)

        mock_result = MagicMock()
        mock_result.available_version = "9.99.0"
        mock_result.checked_at = "2026-06-07T00:00:00Z"

        with (
            patch("yadgar.update.check.probe_latest_version", return_value=mock_result),
            patch("yadgar.update.install_methods.detect_install_method", return_value="container"),
            patch("yadgar.update.install_methods.upgrade_command", return_value="docker pull ..."),
            patch("yadgar.update.install_methods.can_self_install", return_value=False),
        ):
            resp = client.post(
                "/api/control/update",
                json={"action": "install"},
                headers=_auth_headers(),
            )

        assert resp.status_code == 400, f"Expected 400, got {resp.status_code}: {resp.text}"

    def test_pypi_unreachable_returns_503(self, monkeypatch):
        """POST action=check when PyPI unreachable → 503."""
        import httpx

        app = _make_update_app(monkeypatch)
        client = TestClient(app, raise_server_exceptions=False)

        with patch(
            "yadgar.update.check.probe_latest_version",
            side_effect=httpx.ConnectError("unreachable"),
        ):
            resp = client.post(
                "/api/control/update",
                json={"action": "check"},
                headers=_auth_headers(),
            )

        assert resp.status_code == 503, f"Expected 503, got {resp.status_code}: {resp.text}"
