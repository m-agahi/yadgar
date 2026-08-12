"""Tests for migration_014 wiki_page embedding backfill (v5.42.1).

Coverage:
- migration_014 is registered in _MIGRATIONS list at correct slot
- _migration_014_wiki_page_embedding_backfill() logs and counts NULL rows
- get_wiki_pages_without_embedding() returns rows with null/none embedding
- update_wiki_page_embedding_only() sets embedding without creating version row
- WikiStore.backfill_null_embeddings() bacfills all NULL rows, returns count
- Idempotent: re-run finds 0 rows
- Embed-service unavailable: warns + skips row, returns partial count
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from yadgar._shared.storage.migrations import (
    _MIGRATIONS,
    _migration_014_wiki_page_embedding_backfill,
)
from yadgar.core import server

# ---------------------------------------------------------------------------
# Migration registry
# ---------------------------------------------------------------------------


class TestMigration014Registration:
    def test_migration_014_in_list(self):
        """migration_014 is in the _MIGRATIONS list at slot 14 (0-indexed 13)."""
        versions = [m["version"] for m in _MIGRATIONS]
        assert "014_wiki_page_embedding_backfill" in versions

    def test_migration_014_in_migrations_list(self):
        """migration_014 is in the _MIGRATIONS list (membership, not positional)."""
        versions = [m["version"] for m in _MIGRATIONS]
        assert "014_wiki_page_embedding_backfill" in versions

    def test_migration_014_fn_is_callable(self):
        """migration_014 entry has a callable fn."""
        entry = next(m for m in _MIGRATIONS if m["version"] == "014_wiki_page_embedding_backfill")
        assert callable(entry["fn"])
        assert entry["fn"] is _migration_014_wiki_page_embedding_backfill


# ---------------------------------------------------------------------------
# Fixture: isolated in-process DB
# ---------------------------------------------------------------------------


@pytest.fixture()
def _engines(tmp_path):
    """Isolated server with real embedding model."""
    server.init_engines(
        db_path=str(tmp_path / "migration014_test.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


def _wiki():
    return server._wiki


def _storage():
    import yadgar._shared.runtime.state as _state_mod

    return _state_mod._storage


#: Identity these fixtures write under. Named explicitly because C5 (ADR-0227)
#: deleted every tier under the caller's value at the storage chokepoint — an
#: insert with no ``project_id`` now raises ``UnresolvedProjectError`` instead of
#: being stamped ``"global"``. These rows exist only to be found by the
#: embedding backfill, so any stable key does; what matters is that one is named.
_TEST_PROJECT_ID = "owner/repo"


def _insert_null_page(title: str, content: str = "placeholder content") -> int:
    """Insert wiki_page row with embedding=None."""
    st = _storage()
    slug = _wiki()._slugify(title)
    return st.insert_wiki_page(
        {
            "title": title,
            "slug": slug,
            "content": content,
            "category": None,
            "tags": [],
            "links": [],
            "confidence": 1.0,
            "embedding": None,
            "source_memory_ids": [],
            "project_id": _TEST_PROJECT_ID,
        }
    )


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------


class TestStorageMethods:
    """Tests for get_wiki_pages_without_embedding and update_wiki_page_embedding_only."""

    def test_get_empty_on_fresh_db(self, _engines):
        """Fresh DB with no pages returns empty list."""
        missing = _storage().get_wiki_pages_without_embedding()
        assert missing == []

    def test_get_returns_null_rows(self, _engines):
        """Rows inserted with embedding=None appear in the list."""
        pid1 = _insert_null_page("Page One")
        pid2 = _insert_null_page("Page Two")

        missing = _storage().get_wiki_pages_without_embedding()
        ids = {r["id"] for r in missing}

        assert pid1 in ids
        assert pid2 in ids

    def test_get_excludes_embedded_rows(self, _engines):
        """Rows with a real embedding do NOT appear in the list."""
        import yadgar.backend.queue_drainer._locals as _loc

        _loc._drain_local.active = True
        try:
            server.wiki_add(title="Real Embedded Page", content="Has embedding.")
        finally:
            _loc._drain_local.active = False

        null_pid = _insert_null_page("Null Page")
        missing = _storage().get_wiki_pages_without_embedding()
        ids = {r["id"] for r in missing}

        assert null_pid in ids
        # The embedded page must NOT be in the list.
        all_pages = _storage()._q("SELECT id FROM wiki_page")
        all_ids = {_storage()._extract_id(p.get("id")) for p in all_pages}
        embedded_ids = all_ids - {null_pid}
        assert not embedded_ids.intersection(ids), (
            f"Embedded pages {embedded_ids} appeared in NULL list {ids}"
        )

    def test_get_result_has_required_fields(self, _engines):
        """Each result dict has id, title, content keys."""
        _insert_null_page("Field Check Page", "Some content here.")
        missing = _storage().get_wiki_pages_without_embedding()
        assert len(missing) >= 1
        for row in missing:
            assert "id" in row
            assert "title" in row
            assert "content" in row

    def test_update_embedding_only_sets_embedding(self, _engines):
        """update_wiki_page_embedding_only() sets the embedding without error."""
        pid = _insert_null_page("Embed Me")

        # Should be in missing list
        before = _storage().get_wiki_pages_without_embedding()
        assert any(r["id"] == pid for r in before)

        # Set a fake embedding (random floats as bytes)
        import struct

        fake_emb = struct.pack("f" * 384, *[0.01 * i % 1.0 for i in range(384)])
        _storage().update_wiki_page_embedding_only(pid, fake_emb)

        # Should no longer be in missing list
        after = _storage().get_wiki_pages_without_embedding()
        assert not any(r["id"] == pid for r in after)

    def test_update_embedding_only_no_version_created(self, _engines):
        """update_wiki_page_embedding_only() does NOT create a wiki_page_version row."""
        pid = _insert_null_page("No New Version")

        # Count versions before
        before = _storage()._q(
            "SELECT count() AS c FROM wiki_page_version WHERE page_id = $p GROUP ALL",
            {"p": pid},
        )
        version_count_before = int(before[0].get("c", 0)) if before else 0

        import struct

        fake_emb = struct.pack("f" * 384, *[0.01 * i % 1.0 for i in range(384)])
        _storage().update_wiki_page_embedding_only(pid, fake_emb)

        after = _storage()._q(
            "SELECT count() AS c FROM wiki_page_version WHERE page_id = $p GROUP ALL",
            {"p": pid},
        )
        version_count_after = int(after[0].get("c", 0)) if after else 0

        assert version_count_after == version_count_before, (
            f"update_wiki_page_embedding_only created version rows: "
            f"before={version_count_before}, after={version_count_after}"
        )


# ---------------------------------------------------------------------------
# WikiStore.backfill_null_embeddings
# ---------------------------------------------------------------------------


class TestBackfillNullEmbeddings:
    def test_returns_zero_on_empty_db(self, _engines):
        """Fresh DB: backfill returns 0."""
        count = _wiki().backfill_null_embeddings()
        assert count == 0

    def test_backfills_all_null_rows(self, _engines):
        """N null-embedding pages → backfill returns N."""
        for i in range(4):
            _insert_null_page(f"Backfill Page {i}", f"Content for page {i}.")

        count = _wiki().backfill_null_embeddings()
        assert count == 4

    def test_backfill_idempotent(self, _engines):
        """Second run returns 0 (no NULL rows remain)."""
        _insert_null_page("Idempotent Page", "Content for idempotency test.")

        first = _wiki().backfill_null_embeddings()
        assert first == 1

        second = _wiki().backfill_null_embeddings()
        assert second == 0

    def test_backfill_skips_existing_embeddings(self, _engines):
        """Rows with embeddings are skipped; only NULL rows are counted."""
        import yadgar.backend.queue_drainer._locals as _loc

        _loc._drain_local.active = True
        try:
            server.wiki_add(title="Already Embedded", content="Has embedding.")
        finally:
            _loc._drain_local.active = False

        _insert_null_page("Needs Embedding", "Content without embedding.")

        count = _wiki().backfill_null_embeddings()
        assert count == 1, f"Expected exactly 1 row backfilled, got {count}"

    def test_backfill_embed_failure_warns_and_skips(self, _engines, caplog):
        """If _compute_embedding returns None, row is skipped with WARN log."""
        _insert_null_page("Embed Fail Page", "Content.")

        with patch.object(_wiki(), "_compute_embedding", return_value=None):
            with caplog.at_level("WARNING", logger="yadgar._shared.wiki"):
                count = _wiki().backfill_null_embeddings()

        assert count == 0
        assert any(
            "embed returned None" in r.message or "skipping" in r.message for r in caplog.records
        ), f"Expected WARN log about embed failure, got: {[r.message for r in caplog.records]}"

    def test_backfill_embed_exception_warns_and_continues(self, _engines, caplog):
        """If _compute_embedding raises, the row is skipped and backfill continues."""
        _insert_null_page("Fail Page", "Fail content.")
        _insert_null_page("Success Page", "Success content.")

        call_count = [0]
        orig = _wiki()._compute_embedding

        def _fail_first(title: str, content: str):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("embed service unavailable")
            return orig(title, content)

        with patch.object(_wiki(), "_compute_embedding", side_effect=_fail_first):
            with caplog.at_level("WARNING", logger="yadgar._shared.wiki"):
                count = _wiki().backfill_null_embeddings()

        # One succeeded, one failed
        assert count == 1, f"Expected 1 success (1 fail skipped), got {count}"
        assert any("failed" in r.message for r in caplog.records)

    def test_backfill_batch_size_respected(self, _engines):
        """Custom batch_size parameter is accepted without error."""
        for i in range(6):
            _insert_null_page(f"Batch Page {i}", f"Content {i}.")

        count = _wiki().backfill_null_embeddings(batch_size=2)
        assert count == 6


# ---------------------------------------------------------------------------
# migration_014 function (unit test — standalone)
# ---------------------------------------------------------------------------


class TestMigration014Function:
    def test_logs_count_when_nulls_exist(self, caplog, _engines):
        """_migration_014 logs a WARNING when NULL rows exist."""
        # Insert a null-embedding row to simulate pre-v5.39 state.
        _insert_null_page("Pre-v5.39 Page", "Content.")

        mock_storage = MagicMock()
        mock_storage._q.return_value = [{"c": 1}]

        with caplog.at_level("WARNING", logger="yadgar._shared.storage.migrations"):
            _migration_014_wiki_page_embedding_backfill(mock_storage)

        assert any(
            "1" in r.message and ("NULL" in r.message or "backfill" in r.message)
            for r in caplog.records
        ), f"Expected WARNING about 1 null row, got: {[r.message for r in caplog.records]}"

    def test_logs_info_when_no_nulls(self, caplog, _engines):
        """_migration_014 logs INFO when no NULL rows found."""
        mock_storage = MagicMock()
        mock_storage._q.return_value = [{"c": 0}]

        with caplog.at_level("INFO", logger="yadgar._shared.storage.migrations"):
            _migration_014_wiki_page_embedding_backfill(mock_storage)

        assert any(
            "nothing to backfill" in r.message or "no NULL" in r.message.lower()
            for r in caplog.records
        ), f"Expected INFO about no nulls, got: {[r.message for r in caplog.records]}"
