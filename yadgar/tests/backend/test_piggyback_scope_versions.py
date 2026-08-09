"""Car B — piggyback scope versions on every backend response.

Every /admin response (and the read-flavored siblings: /recall, /read_query,
/viz) carries the CURRENT scope versions in an ``AdminResponse.scope_versions``
envelope field (Car B §15.2 envelope choice). Core compares against what it
holds; a moved version makes its cached entries unreachable — zero extra
round-trips in steady state.

A bump between two calls is reflected in the second response. This is the
reusable version-in-key mechanism (ADR-0053) extended from data caches
(slot/entity) to config + ledger scope kinds.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from yadgar.backend.cache.scope_versions import get_scope_versions
from yadgar.backend.embed_service.embed_service_models import AdminRequest


@pytest.fixture
def _stub_admin(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Stub ``run_admin_op_async`` to a fast echo; routes real calls.

    Patches ``yadgar.backend.admin_exec`` (where the route's lazy import
    resolves) rather than ``embed_service_routes`` (which imports lazily
    inside the route body).
    """

    async def _echo(op: str, payload: dict) -> dict:
        return {"op": op, "echo": payload}

    monkeypatch.setattr("yadgar.backend.admin_exec.run_admin_op_async", _echo)
    return _echo  # type: ignore[return-value]


class TestScopeVersionsEnvelope:
    def test_admin_response_carries_scope_versions(self, _stub_admin: AsyncMock) -> None:
        from yadgar.backend.embed_service.embed_service import app

        client = TestClient(app)
        resp = client.post(
            "/admin",
            json=AdminRequest(op="list_task_rows", payload={"project_id": "x"}).model_dump(),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "scope_versions" in body, (
            "AdminResponse must carry the current scope versions so core can "
            "invalidate its PTC entries when a scope bumps (ADR-0053)"
        )
        assert isinstance(body["scope_versions"], dict)
        # The two new kinds are present (even at v0 — never bumped).
        assert "config" in body["scope_versions"]
        assert "ledger" in body["scope_versions"]

    def test_bump_between_calls_reflected_in_subsequent_response(
        self, _stub_admin: AsyncMock
    ) -> None:
        sv = get_scope_versions()
        from yadgar.backend.embed_service.embed_service import app as _app

        client = TestClient(_app)

        # First call: snapshot the current per-kind epoch (singleton state may
        # have been touched by earlier tests in the session).
        body1 = client.post(
            "/admin",
            json={"op": "list_task_rows", "payload": {"project_id": "x"}},
        ).json()
        v1 = body1["scope_versions"]["config"]

        # Bump a config scope.
        sv.bump("config", "seq_batch")

        # Second call: scope_versions reflects the bump (per-kind epoch).
        body2 = client.post(
            "/admin",
            json={"op": "list_task_rows", "payload": {"project_id": "x"}},
        ).json()
        v2 = body2["scope_versions"]["config"]
        assert v2 == v1 + 1, (
            f"scope_versions envelope must reflect the bump between calls: first={v1}, second={v2}"
        )


class TestAdminResponseModel:
    def test_admin_response_has_scope_versions_field(self) -> None:
        from yadgar.backend.embed_service.embed_service_models import AdminResponse

        # Pydantic v2: model_fields is the canonical field map.
        assert "scope_versions" in AdminResponse.model_fields

    def test_scope_versions_field_default(self) -> None:
        from yadgar.backend.embed_service.embed_service_models import AdminResponse

        # Defaults to empty dict so existing direct construction (no
        # scope_versions kwarg) still works — backwards-compat.
        resp = AdminResponse(result={"ok": True})
        assert resp.scope_versions == {}
        assert resp.result == {"ok": True}
