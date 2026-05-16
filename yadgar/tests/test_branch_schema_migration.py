"""Tests for branch column schema migration (v5.0 §25, Stage 7).

Covers:
- Migration idempotent: run twice → same row count, no errors
- Backfill: pre-existing row without branch field → after migration has branch='master'
- New row with explicit branch='feat/v5.0' → stored verbatim
- New row without branch param → branch remains NONE

Migration is tested by calling the migration function directly
(bypassing the version-tracking layer) so tests remain isolated.
"""

import pytest


@pytest.fixture
def storage(tmp_path):
    from yadgar.storage import StorageEngine

    engine = StorageEngine(str(tmp_path / "test_branch.db"))
    yield engine
    engine.close()


def _insert_bare_memory(storage, content: str) -> int:
    """Insert a memory without setting the branch field, simulating pre-v5 data."""
    mid = storage._next_id("memory")
    now = storage._now_iso()
    storage._q(
        "CREATE type::record('memory', $id) SET "
        "content = $content, tags = $tags, directory_context = $dir, "
        "created_at = $ts, last_accessed = $ts, heat = $heat, "
        "is_stale = false, plasticity = 1.0, stability = 0.0, "
        "excitability = 1.0, store_type = $st, compression_level = 0, "
        "sr_x = 0.0, sr_y = 0.0, reconsolidation_count = 0, "
        "provenance_agent = $agent, vector_clock = $vc, is_protected = false",
        {
            "id": mid,
            "content": content,
            "tags": [],
            "dir": "/tmp",
            "ts": now,
            "heat": 1.0,
            "st": "episodic",
            "agent": "default",
            "vc": "{}",
        },
    )
    return mid


def _insert_bare_wiki_page(storage, slug: str) -> int:
    """Insert a wiki page without setting the branch field, simulating pre-v5 data."""
    pid = storage._next_id("wiki_page")
    now = storage._now_iso()
    storage._q(
        "CREATE type::record('wiki_page', $id) SET "
        "slug = $slug, title = $title, content = $content, "
        "tags = $tags, links = $links, confidence = 1.0, "
        "source_memory_ids = [], "
        "created_at = $ts, updated_at = $ts",
        {
            "id": pid,
            "slug": slug,
            "title": slug,
            "content": "pre-v5 content",
            "tags": [],
            "links": [],
            "ts": now,
        },
    )
    return pid


class TestBranchMigrationIdempotent:
    """Running the migration twice produces same result with no errors."""

    def test_double_run_no_error(self, storage):
        from yadgar.storage import _migration_004_branch_field

        _migration_004_branch_field(storage)
        _migration_004_branch_field(storage)  # second run must not raise

    def test_double_run_same_row_count(self, storage):
        from yadgar.storage import _migration_004_branch_field

        _insert_bare_memory(storage, "idempotent test memory")
        _insert_bare_wiki_page(storage, "idempotent-slug")

        _migration_004_branch_field(storage)
        rows_after_first = storage._q("SELECT count() AS c FROM memory GROUP ALL")
        count_first = int(rows_after_first[0]["c"]) if rows_after_first else 0

        _migration_004_branch_field(storage)
        rows_after_second = storage._q("SELECT count() AS c FROM memory GROUP ALL")
        count_second = int(rows_after_second[0]["c"]) if rows_after_second else 0

        assert count_first == count_second


class TestBranchBackfill:
    """Pre-existing rows without branch get backfilled to 'master'."""

    def test_memory_backfill(self, storage):
        from yadgar.storage import _migration_004_branch_field

        mid = _insert_bare_memory(storage, "pre-v5 memory backfill test")

        _migration_004_branch_field(storage)

        rows = storage._q(f"SELECT branch FROM memory:{mid}")
        assert rows, "memory row not found after migration"
        assert rows[0].get("branch") == "master", (
            f"expected 'master', got {rows[0].get('branch')!r}"
        )

    def test_wiki_page_backfill(self, storage):
        from yadgar.storage import _migration_004_branch_field

        pid = _insert_bare_wiki_page(storage, "pre-v5-wiki-backfill")

        _migration_004_branch_field(storage)

        rows = storage._q(f"SELECT branch FROM wiki_page:{pid}")
        assert rows, "wiki_page row not found after migration"
        assert rows[0].get("branch") == "master", (
            f"expected 'master', got {rows[0].get('branch')!r}"
        )

    def test_already_master_row_stays_master(self, storage):
        """Row already tagged master must remain master after re-run."""
        from yadgar.storage import _migration_004_branch_field

        _migration_004_branch_field(storage)

        mid = storage.insert_memory(
            {
                "content": "already-tagged memory",
                "directory_context": "/tmp",
                "tags": [],
            },
            branch="master",
        )

        _migration_004_branch_field(storage)

        rows = storage._q(f"SELECT branch FROM memory:{mid}")
        assert rows[0].get("branch") == "master"


class TestBranchStorageHelpers:
    """insert_memory and insert_wiki_page accept optional branch kwarg."""

    def test_insert_memory_with_branch(self, storage):
        mid = storage.insert_memory(
            {
                "content": "feature branch memory",
                "directory_context": "/tmp",
                "tags": [],
            },
            branch="feat/v5.0",
        )
        rows = storage._q(f"SELECT branch FROM memory:{mid}")
        assert rows, "memory row not found"
        assert rows[0].get("branch") == "feat/v5.0"

    def test_insert_memory_no_branch_stays_none(self, storage):
        mid = storage.insert_memory(
            {
                "content": "no-branch memory",
                "directory_context": "/tmp",
                "tags": [],
            }
        )
        rows = storage._q(f"SELECT branch FROM memory:{mid}")
        assert rows, "memory row not found"
        # branch should be absent (NONE) — not the string 'None'
        branch = rows[0].get("branch")
        assert branch is None, f"expected None (NONE), got {branch!r}"

    def test_insert_wiki_page_with_branch(self, storage):
        pid = storage.insert_wiki_page(
            {
                "slug": "test-wiki-with-branch",
                "title": "Test Wiki",
                "content": "content",
                "tags": [],
                "links": [],
            },
            branch="feat/v5.0",
        )
        rows = storage._q(f"SELECT branch FROM wiki_page:{pid}")
        assert rows, "wiki_page row not found"
        assert rows[0].get("branch") == "feat/v5.0"

    def test_insert_wiki_page_no_branch_stays_none(self, storage):
        pid = storage.insert_wiki_page(
            {
                "slug": "test-wiki-no-branch",
                "title": "No Branch Wiki",
                "content": "content",
                "tags": [],
                "links": [],
            }
        )
        rows = storage._q(f"SELECT branch FROM wiki_page:{pid}")
        assert rows, "wiki_page row not found"
        branch = rows[0].get("branch")
        assert branch is None, f"expected None (NONE), got {branch!r}"

    def test_update_memory_fields_with_branch(self, storage):
        """update_memory_fields accepts branch kwarg via _MEMORY_UPDATABLE_FIELDS."""
        mid = storage.insert_memory(
            {
                "content": "memory for update-branch test",
                "directory_context": "/tmp",
                "tags": [],
            }
        )
        # Should not raise; branch in _MEMORY_UPDATABLE_FIELDS
        storage.update_memory_fields(mid, branch="feat/v5.0")

        rows = storage._q(f"SELECT branch FROM memory:{mid}")
        assert rows[0].get("branch") == "feat/v5.0"
