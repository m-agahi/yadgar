"""Car B — /health route reports MariaDB/ledger reachability.

§15.4 / ADR-0200: the /health route today probes SurrealDB + the embedding model
only. A backend that cannot reach MariaDB but /health still reports
``{"status":"ok"}`` is the same invisibility problem as the maintenance gate.

The route must surface ``ledger: bool`` in the payload and gate 503 on it:
a healthy backend reports ``status: ok`` AND ``ledger: true``.

The reachability probe is inlined inside ``health`` (no separate helper — keeps
``embed_service.py`` under the file_loc HARD cap), so the tests patch the lazy
``_get_sql_storage`` import instead.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, _patch, patch

from fastapi.testclient import TestClient


def _build_client() -> TestClient:
    from yadgar.backend.embed_service.embed_service import app

    return TestClient(app)


def _patch_engine(engine: object) -> _patch:
    """Patch the lazy ``_get_sql_storage`` import in embed_service to return *engine*."""
    return patch(
        "yadgar._shared.runtime.lifecycle._get_sql_storage",
        return_value=engine,
    )


class TestHealthLedgerProbe:
    def test_health_reports_ledger_true_when_mariadb_reachable(self) -> None:
        """When MariaStorageEngine.verify() succeeds, payload includes
        ``ledger: true``. Status reflects the OVERALL check (db + model +
        ledger); in test env SurrealDB / model are False, so we assert the
        ledger field, not the status field."""
        fake_engine = type("E", (), {"verify": AsyncMock(return_value={"ok": True})})()

        client = _build_client()
        with _patch_engine(fake_engine):
            resp = client.get("/health")
            body = resp.json()

        assert "ledger" in body, "/health payload must include ledger reachability"
        assert body["ledger"] is True

    def test_health_reports_ledger_false_when_mariadb_unreachable(self) -> None:
        """When MariaDB is unreachable, /health reports ``ledger: false``."""
        fake_engine = type("E", (), {"verify": AsyncMock(side_effect=RuntimeError("boom"))})()

        client = _build_client()
        with _patch_engine(fake_engine):
            resp = client.get("/health")
            body = resp.json()

        assert body["ledger"] is False

    def test_health_reports_ledger_false_when_engine_absent(self) -> None:
        """No MariaStorageEngine composed (engine #2 absent) — ledger reports
        False. The reachability probe is graceful when the engine slot is
        None (no crash)."""
        client = _build_client()
        with _patch_engine(None):
            resp = client.get("/health")
            body = resp.json()

        assert body["ledger"] is False


class TestHealthProbeReachesChokepoint:
    """The /health probe must reach ``_get_sql_storage()`` (the chokepoint) and
    call ``engine.verify()`` on the returned engine. Mirrors the
    engine_status.py docstring's stance: absence is a successful answer to
    'is engine #2 reachable today?'."""

    def test_probe_calls_engine_verify_when_composed(self) -> None:
        fake_engine = type("E", (), {"verify": AsyncMock(return_value={"ok": True})})()
        client = _build_client()
        with _patch_engine(fake_engine):
            client.get("/health")
        fake_engine.verify.assert_awaited_once()

    def test_probe_returns_true_when_engine_verify_ok(self) -> None:
        fake_engine = type("E", (), {"verify": AsyncMock(return_value={"ok": True})})()
        client = _build_client()
        with _patch_engine(fake_engine):
            body = client.get("/health").json()
        assert body["ledger"] is True

    def test_probe_returns_false_when_engine_verify_raises(self) -> None:
        fake_engine = type("E", (), {"verify": AsyncMock(side_effect=RuntimeError("boom"))})()
        client = _build_client()
        with _patch_engine(fake_engine):
            body = client.get("/health").json()
        assert body["ledger"] is False
