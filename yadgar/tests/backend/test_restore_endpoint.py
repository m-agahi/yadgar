"""Contract tests for the backend POST /restore route (T2 Car B).

The route is a thin async shell: bootstrap engines → run_restore in a worker
thread → wrap the payload as {"result": ...}. These tests patch the engine
bootstrap + the restore body, so they pin the HTTP contract only (auth,
request validation, envelope) — the compute itself is covered by
test_restoration.py / test_backend_recall_slim_engines.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient

_RESTORE_PAYLOAD = {
    "checkpoint": None,
    "anchored_memories": 1,
    "recent_memories": 0,
    "hot_memories": 2,
    "predicted_memories": 0,
    "gaps_detected": 0,
    "memory_blocks": 0,
    "epoch": 5,
    "formatted": "# Yadgar Context Restoration (Hippocampal Replay)",
}


@pytest.fixture
def client(monkeypatch):
    """TestClient with auth escape hatch + patched engine bootstrap and body."""
    monkeypatch.setenv("YADGAR_ALLOW_ROOT", "1")

    import yadgar.backend.embed_service.embed_service as es
    import yadgar.backend.restoration as resto

    monkeypatch.setattr(es, "_ensure_recall_engines", MagicMock())
    run_restore = MagicMock(return_value=dict(_RESTORE_PAYLOAD))
    monkeypatch.setattr(resto, "run_restore", run_restore)

    with TestClient(es.app) as c:
        c.run_restore_mock = run_restore
        yield c


def test_restore_route_returns_result_envelope(client):
    resp = client.post("/restore", json={"directory": "/my/project"})
    assert resp.status_code == 200
    assert resp.json() == {"result": _RESTORE_PAYLOAD}
    # C10g: the route forwards BOTH scope values — restore's sinks key on
    # different columns, so dropping either silently unscopes half of them.
    client.run_restore_mock.assert_called_once_with("/my/project", None)


def test_restore_route_directory_defaults_empty(client):
    resp = client.post("/restore", json={})
    assert resp.status_code == 200
    client.run_restore_mock.assert_called_once_with("", None)


def test_restore_route_rejects_unknown_fields(client):
    """RestoreRequest is extra='forbid' — unknown fields are a 422, not ignored."""
    resp = client.post("/restore", json={"directory": "/p", "nope": 1})
    assert resp.status_code == 422
    client.run_restore_mock.assert_not_called()


def test_restore_route_locked_without_token(monkeypatch):
    """Fail-secure: no YADGAR_MCP_AUTH_TOKEN and no allow-root → 500.

    500, not 503, since ADR-0180 / task:0090 — an unconfigured admin token is a
    permanent server misconfiguration, not a transient outage.
    """
    monkeypatch.setenv("YADGAR_ALLOW_ROOT", "0")
    monkeypatch.delenv("YADGAR_MCP_AUTH_TOKEN", raising=False)

    import yadgar.backend.embed_service.embed_service as es

    with TestClient(es.app) as c:
        resp = c.post("/restore", json={"directory": "/p"})
    assert resp.status_code == 500
