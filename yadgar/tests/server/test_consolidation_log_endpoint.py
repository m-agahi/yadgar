"""/api/metrics/consolidation-log endpoint contract test (Bug 9, TDD).

Regression guard for the flat-zero consolidation chart. The DB carries legacy
rows with a NONE ``timestamp`` (and NONE data columns) alongside the real
per-cycle rows. ``ORDER BY timestamp ASC LIMIT 30`` sorts the NONE-timestamp
rows FIRST (SurrealDB orders NONE before real values ascending), so the endpoint
returned 30 all-zero legacy rows → the chart plotted a permanent flat zero.

FIX (core, same raw-``_q`` seam — no new DB read, ADR-0078-clean): return the
NEWEST non-NONE-timestamp rows (``WHERE timestamp IS NOT NONE ORDER BY timestamp
DESC LIMIT N``), then reverse to ascending for the chart.

Real-storage test: seeds NONE-timestamp legacy rows + recent nonzero rows against
a live StorageEngine and asserts the endpoint returns the newest non-NONE window
in ascending order — a mock ``_q`` would ignore the SQL and prove nothing.
"""

from __future__ import annotations

from datetime import UTC

import pytest

_TEST_TOKEN = "consolidation-log-test-token"


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    """Start in-process server engines against a fresh temp DB."""
    tmp_path = tmp_path_factory.mktemp("consolidation_log_endpoint")
    from yadgar.core import server

    db_path = str(tmp_path / "consolidation_log_test.db")
    server.init_engines(db_path=db_path, embedding_model="all-MiniLM-L6-v2")
    yield
    server.shutdown()


def _make_client(monkeypatch):
    monkeypatch.setenv("YADGAR_REQUIRE_AUTH", "1")
    monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", _TEST_TOKEN)

    from starlette.testclient import TestClient

    from yadgar.core import server as _server
    from yadgar.core.auth_middleware import BearerAuthMiddleware

    asgi_app = _server.mcp_server.streamable_http_app()
    return TestClient(BearerAuthMiddleware(asgi_app), raise_server_exceptions=False)


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_TEST_TOKEN}"}


def _storage():
    import yadgar._shared.runtime.state as _st

    assert _st._storage is not None, "StorageEngine not initialized"
    return _st._storage


@pytest.fixture(autouse=True)
def _wipe_log():
    """Empty consolidation_log before each test (module-scoped DB is shared)."""
    _storage()._q("DELETE consolidation_log")
    yield


def _seed_legacy_none_rows(n: int) -> None:
    """Insert n legacy rows with NONE timestamp + NONE data columns.

    Mirrors the production legacy rows (string record-ids, all NONE) that the
    old ASC query surfaced. ``insert_consolidation_log`` always stamps a
    timestamp, so we go through raw ``_q`` to omit it (→ NONE).
    """
    storage = _storage()
    for i in range(n):
        storage._q(f"CREATE consolidation_log:legacy_{i} SET memory_note = 'legacy'")


def _seed_recent_rows(archived: list[int]) -> None:
    """Insert real per-cycle rows with strictly-ascending timestamps + archived counts."""
    from datetime import datetime, timedelta

    storage = _storage()
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    for i, arch in enumerate(archived):
        ts = (t0 + timedelta(minutes=i)).isoformat()
        storage.insert_consolidation_log(
            {
                "timestamp": ts,
                "memories_archived": arch,
                "memories_added": arch,
            }
        )


def _get_log(monkeypatch, limit: int | None = None) -> list[dict]:
    client = _make_client(monkeypatch)
    url = "/api/metrics/consolidation-log"
    if limit is not None:
        url += f"?limit={limit}"
    resp = client.get(url, headers=_auth_headers())
    assert resp.status_code == 200, f"{resp.status_code}: {resp.text[:200]}"
    return resp.json()


class TestConsolidationLogEndpoint:
    def test_excludes_none_timestamp_legacy_rows(self, monkeypatch):
        """The NONE-timestamp legacy rows must never appear in the payload."""
        _seed_legacy_none_rows(40)
        _seed_recent_rows([10, 20, 30])
        rows = _get_log(monkeypatch)
        # every returned row carries a real timestamp (no empty-string legacy row)
        assert rows, "expected non-empty payload"
        assert all(r["timestamp"] for r in rows), (
            f"NONE-timestamp legacy row leaked into payload: {rows}"
        )

    def test_returns_newest_window_when_over_limit(self, monkeypatch):
        """With >limit real rows, return the NEWEST `limit`, ascending — not the oldest."""
        _seed_legacy_none_rows(5)
        _seed_recent_rows(list(range(1, 40)))  # 39 real rows, archived 1..39
        rows = _get_log(monkeypatch, limit=30)
        assert len(rows) == 30
        archived = [r["archived"] for r in rows]
        # ascending display order (oldest→newest of the newest window)
        assert archived == sorted(archived), f"not ascending: {archived}"
        # newest window = archived 10..39 (the 30 most-recent of 1..39)
        assert archived[0] == 10 and archived[-1] == 39, archived

    def test_nonzero_archived_surfaced(self, monkeypatch):
        """The real archived counts must reach the payload (chart is not flat-zero)."""
        _seed_recent_rows([0, 5, 163])
        rows = _get_log(monkeypatch)
        assert any(r["archived"] > 0 for r in rows), (
            f"all-zero payload → chart stays flat: {[r['archived'] for r in rows]}"
        )
