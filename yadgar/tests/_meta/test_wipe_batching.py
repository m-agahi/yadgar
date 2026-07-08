"""PIECE A (v5.104): per-test SurrealDB wipe must be BATCHED, not per-table.

TDD red-first: before the batching change the wipe issued one round-trip per
table in ``_WIPE_TABLES`` (~26 HTTP DELETEs per test) — this dominated CI
teardown (44% of shard wall). These tests pin the round-trip count to ONE per
namespace and assert data isolation is preserved (write -> wipe -> clean, plus a
cross-test-leak guard), so the batching refactor is an exact-parity change.
"""

from __future__ import annotations

import pytest

from yadgar.tests import conftest as C

# ---------------------------------------------------------------------------
# Round-trip count (the core PIECE A win) — no live DB needed.
# ---------------------------------------------------------------------------


class _CountingStorage:
    """Fake StorageEngine capturing every _q call for round-trip counting."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    def _q(self, surql: str, params=None):
        self.queries.append(surql)
        return []


def test_wipe_via_live_storage_batches_deletes_into_one_query(monkeypatch):
    """_wipe_via_live_storage must issue ONE _q call, not one per table."""
    # Stub the "main" HTTP wipe so we only measure the live-storage path here.
    monkeypatch.setattr(C, "_wipe_namespace_via_http", lambda *a, **k: None)

    fake = _CountingStorage()
    C._wipe_via_live_storage(fake, "http://127.0.0.1:9")

    assert len(fake.queries) == 1, (
        f"expected 1 batched _q call, got {len(fake.queries)}: {fake.queries}"
    )
    # The single statement must cover every wipe table.
    combined = fake.queries[0]
    for table in C._WIPE_TABLES:
        assert f"DELETE {table}" in combined, f"missing DELETE for {table}"


class _CountingClient:
    """Fake httpx.Client capturing every POST for round-trip counting."""

    def __init__(self) -> None:
        self.posts: list[str] = []
        self.closed = False

    def post(self, path, content=None, **kw):
        self.posts.append(content if content is not None else "")

    def close(self) -> None:
        self.closed = True


def test_wipe_tables_with_client_batches_deletes_into_one_post(monkeypatch):
    """The HTTP-fallback ("main"/shutdown) path must issue ONE POST, not 26."""
    fake = _CountingClient()
    # _wipe_tables_with_client imports httpx locally; patch the module attr.
    import httpx as _httpx

    monkeypatch.setattr(_httpx, "Client", lambda *a, **k: fake)

    C._wipe_tables_with_client("http://127.0.0.1:9", "somedb", "auth")

    assert len(fake.posts) == 1, f"expected 1 batched POST, got {len(fake.posts)}: {fake.posts}"
    combined = fake.posts[0]
    for table in C._WIPE_TABLES:
        assert f"DELETE {table}" in combined, f"missing DELETE for {table}"
    assert fake.closed is True


# ---------------------------------------------------------------------------
# Data isolation must survive the batching change (needs live server surreal).
# ---------------------------------------------------------------------------


@pytest.fixture
def _server_url():
    import os

    url = os.environ.get("YADGAR_DB_URL")
    if not url:
        pytest.skip("server-mode surreal not available (embedded mode)")
    return url


def test_batched_wipe_clears_written_data(_server_url):
    """Write a row, run the wipe, assert the table is empty afterward."""
    from yadgar._shared.storage import StorageEngine

    engine = StorageEngine("/tmp/yadgar_piecea_wipe_a.db")
    try:
        engine.add_bookmark("piecea-wipe-slug")
        assert engine.get_bookmark("piecea-wipe-slug") is not None

        C._wipe_via_live_storage(engine, _server_url)

        assert engine.get_bookmark("piecea-wipe-slug") is None, (
            "batched wipe did not clear the written bookmark row"
        )
    finally:
        engine.close()


def test_batched_wipe_no_cross_namespace_leak(_server_url):
    """Two engines (distinct namespaces): wiping one must not clear the other."""
    from yadgar._shared.storage import StorageEngine

    a = StorageEngine("/tmp/yadgar_piecea_leak_a.db")
    b = StorageEngine("/tmp/yadgar_piecea_leak_b.db")
    try:
        a.add_bookmark("leak-a")
        b.add_bookmark("leak-b")

        # Wipe only A's namespace (A is the live storage here).
        C._wipe_via_live_storage(a, _server_url)

        assert a.get_bookmark("leak-a") is None, "A should be wiped"
        assert b.get_bookmark("leak-b") is not None, (
            "B must survive — wipe leaked across namespaces"
        )
    finally:
        a.close()
        b.close()


# ---------------------------------------------------------------------------
# PIECE B — module-scoped `storage` registry: schema once, data wiped per test.
# ---------------------------------------------------------------------------


def test_module_storage_registered_for_wipe(module_storage):
    """The shared module-scoped engine is registered so teardown wipes it."""
    assert module_storage in C._MODULE_SCOPED_STORAGE_ENGINES


class TestModuleStorageIsolation:
    """Two ordered tests share ONE module-scoped engine; test 2 must see clean
    data — proving the per-test wipe reaches the module-scoped namespace even
    though the v5.56 snapshot guard preserves it in the HTTP fallback."""

    def test_a_writes_then_leaves_residue(self, module_storage, _server_url):
        module_storage.add_bookmark("modscope-leak-probe")
        assert module_storage.get_bookmark("modscope-leak-probe") is not None

    def test_b_sees_clean_data(self, module_storage, _server_url):
        # Same engine object as test_a (module scope) — but the row must be gone.
        assert module_storage.get_bookmark("modscope-leak-probe") is None, (
            "module-scoped storage leaked data across tests — per-test wipe "
            "did not reach the registered engine's namespace"
        )


# ---------------------------------------------------------------------------
# PIECE C — a test that monkeypatches YADGAR_DB_URL to an UNREACHABLE host must
# not make the post-test wipe hang trying to connect to it (the 114.8s teardown
# outlier in test_admin_config::test_config_gauge_skips_string_entries — it sets
# YADGAR_DB_URL=http://yadgar-backend:8000, a Docker-internal host unreachable
# from the runner; the HTTP-fallback wipe then blocked on connect per namespace).
# The wipe must target the REAL session surreal, captured at fixture setup, not
# whatever a test left in os.environ.
# ---------------------------------------------------------------------------


def test_authoritative_db_url_ignores_monkeypatched_env(monkeypatch, _server_url):
    """_authoritative_db_url() returns the real session surreal URL even when a
    test has monkeypatched YADGAR_DB_URL to an unreachable host."""
    monkeypatch.setenv("YADGAR_DB_URL", "http://yadgar-backend:8000")
    assert C._authoritative_db_url() == C._REAL_DB_URL
    assert C._authoritative_db_url() != "http://yadgar-backend:8000"


def test_wipe_uses_authoritative_url_not_env(monkeypatch, _server_url):
    """The wipe teardown reads the authoritative URL, so a bogus env value can't
    redirect (and hang) the HTTP-fallback wipe."""
    monkeypatch.setenv("YADGAR_DB_URL", "http://yadgar-backend:8000")
    # Force the shut-down (HTTP-fallback) branch by clearing server._storage.
    import yadgar.core.server as _srv

    monkeypatch.setattr(_srv, "_storage", None, raising=False)
    seen: list[str] = []
    monkeypatch.setattr(C, "_wipe_namespace_via_http", lambda url, ns: seen.append(url))

    C._do_wipe_after_test(C._authoritative_db_url(), frozenset())

    assert seen, "expected the HTTP-fallback wipe to run"
    assert all("yadgar-backend" not in u for u in seen), (
        f"wipe targeted the unreachable monkeypatched host: {seen}"
    )
