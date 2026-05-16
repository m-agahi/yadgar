"""Tests for §28 /hooks/session-context endpoint (project_brief pipe).

TDD — written BEFORE the implementation.
Covers:
- Endpoint requires bearer auth (401 without token)
- Returns markdown body from project_brief._render
- Integrates project_brief catalog payload
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

# ---------------------------------------------------------------------------
# Test app setup
# ---------------------------------------------------------------------------


def _make_client(token: str) -> TestClient:
    """Build a TestClient for the session-context endpoint."""
    os.environ["YADGAR_REQUIRE_AUTH"] = "1"
    os.environ["YADGAR_MCP_AUTH_TOKEN"] = token

    from yadgar import server as _server
    from yadgar.auth_middleware import BearerAuthMiddleware

    # Use streamable_http_app() to get the actual callable ASGI app
    asgi_app = _server.mcp_server.streamable_http_app()
    return TestClient(BearerAuthMiddleware(asgi_app), raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _engines(tmp_path):
    from yadgar import server

    db_path = str(tmp_path / "test.db")
    server.init_engines(db_path=db_path, embedding_model="all-MiniLM-L6-v2")
    yield
    server.shutdown()


# ---------------------------------------------------------------------------
# Auth enforcement
# ---------------------------------------------------------------------------


def test_session_context_requires_bearer_token(tmp_path):
    """/hooks/session-context returns 401 without Authorization header."""
    client = _make_client("secret-token")
    resp = client.get(f"/hooks/session-context?directory={tmp_path}")
    assert resp.status_code == 401, f"Expected 401, got {resp.status_code}"


def test_session_context_accepts_valid_bearer(tmp_path):
    """/hooks/session-context returns 200 with valid bearer token."""
    token = "valid-token-xyz"
    client = _make_client(token)
    resp = client.get(
        f"/hooks/session-context?directory={tmp_path}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"


def test_session_context_rejects_wrong_token(tmp_path):
    """/hooks/session-context returns 401 with wrong bearer token."""
    client = _make_client("correct-token")
    resp = client.get(
        f"/hooks/session-context?directory={tmp_path}",
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Response body — markdown from project_brief
# ---------------------------------------------------------------------------


def test_session_context_returns_text_field(tmp_path):
    """Response JSON must contain a 'text' field."""
    token = "tok"
    client = _make_client(token)
    resp = client.get(
        f"/hooks/session-context?directory={tmp_path}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "text" in body, f"Response missing 'text' field: {body}"


def test_session_context_text_is_string(tmp_path):
    """'text' field in response is a string."""
    token = "tok2"
    client = _make_client(token)
    resp = client.get(
        f"/hooks/session-context?directory={tmp_path}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["text"], str)


def test_session_context_integrates_project_brief(tmp_path):
    """Endpoint calls project_brief and uses its _render field as text."""
    token = "tok3"

    mock_brief = {
        "_render": "# My Project\n\nTest render content.",
        "project": "yadgar",
        "branch": "master",
        "stale_wiki_count": 0,
        "init_memory_present": False,
        "active_work_present": False,
        "top_anchors": [],
        "recent_episode_count": 0,
    }

    from yadgar import server as _server

    with patch.object(_server, "project_brief", return_value=mock_brief) as mock_pb:
        client = _make_client(token)
        resp = client.get(
            f"/hooks/session-context?directory={tmp_path}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["text"] == "# My Project\n\nTest render content."
    mock_pb.assert_called_once()


def test_session_context_uses_directory_param(tmp_path):
    """Endpoint passes directory query param to project_brief."""
    token = "tok4"
    from yadgar import server as _server

    captured_dir = {}

    def _fake_brief(directory, mode="catalog"):
        captured_dir["dir"] = directory
        return {
            "_render": f"# Project at {directory}",
            "project": "test",
            "branch": "master",
            "stale_wiki_count": 0,
            "init_memory_present": False,
            "active_work_present": False,
            "top_anchors": [],
            "recent_episode_count": 0,
        }

    with patch.object(_server, "project_brief", side_effect=_fake_brief):
        client = _make_client(token)
        resp = client.get(
            f"/hooks/session-context?directory={tmp_path}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    assert captured_dir["dir"] == str(tmp_path)


def test_session_context_graceful_on_storage_error(tmp_path):
    """Endpoint returns 200 with empty text if project_brief fails."""
    token = "tok5"
    from yadgar import server as _server

    with patch.object(_server, "project_brief", side_effect=RuntimeError("DB down")):
        client = _make_client(token)
        resp = client.get(
            f"/hooks/session-context?directory={tmp_path}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["text"], str)
