"""RED tests for v5.42.6 — migration 018 directory backfill repair.

TDD: written BEFORE implementation. These tests start RED and go GREEN once
migration 018 and the migration 016 source fix are implemented.

Coverage:
T1. Field-absent wiki_page row (no directory_context key) is backfilled by
    migration 018 to the correct heuristic value.
T2. Migration 018 is idempotent — running twice produces no duplicate or error.
T3. After migration 018, wiki_list(directory="…") returns backfilled rows.
T4. Memory rows with field-absent directory_context are backfilled to 'global'.
T5. Migration 016 source fix: Python-side filter catches field-absent rows
    (the IS NONE query alone misses them — verified empirically).
"""

from __future__ import annotations

import pytest

from yadgar._shared.storage.migrations import (
    _migration_016_directory_context,
    _migration_018_directory_context_backfill_repair,
)
from yadgar.core import server

# ── fixture ────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("v5_42_6_directory_backfi")
    server.init_engines(
        db_path=str(tmp_path / "test_018.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


def _storage():
    import yadgar._shared.runtime.state as _st

    return _st._storage


def _insert_legacy_wiki_page(title: str, tags: list[str], slug: str | None = None) -> str:
    """Insert a wiki_page WITHOUT directory_context to simulate a pre-migration-016 row.

    Uses raw SurrealDB query to bypass the application-level insert_wiki_page helper
    (which always writes directory_context).  The resulting row is field-absent —
    not directory_context=NONE but literally lacking the key — matching the bug.

    Temporarily relaxes the schema constraint (from migration 016) to allow the
    field-absent insert — this mirrors the real-world situation where the rows were
    created before migration 016 ran.
    """
    st = _storage()
    pid = st._next_id("wiki_page")
    if slug is None:
        import re

        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:64]

    # Temporarily relax schema so we can insert a row without directory_context
    # (simulates rows created before migration 016 was applied).
    st._q("DEFINE FIELD OVERWRITE directory_context ON TABLE wiki_page TYPE option<string>")
    try:
        st._q(
            "CREATE type::record('wiki_page', $pid) SET "
            "title = $title, slug = $slug, content = $content, "
            "category = 'reference', tags = $tags, links = [], "
            "confidence = 'medium', source_memory_ids = [], "
            "created_at = time::now(), updated_at = time::now()",
            {"pid": pid, "title": title, "slug": slug, "content": "test content", "tags": tags},
        )
    finally:
        # Restore the strict schema (as migration 016 left it).
        st._q(
            "DEFINE FIELD OVERWRITE directory_context ON TABLE wiki_page TYPE string "
            "ASSERT $value != NONE AND string::len($value) > 0"
        )
    return slug


def _insert_legacy_memory(content: str = "legacy mem") -> None:
    """Insert a memory row WITHOUT directory_context (field-absent).

    Temporarily relaxes the schema constraint to allow the field-absent insert.
    """
    st = _storage()
    pid = st._next_id("memory")
    st._q("DEFINE FIELD OVERWRITE directory_context ON TABLE memory TYPE option<string>")
    try:
        st._q(
            "CREATE type::record('memory', $pid) SET "
            "content = $content, heat = 1.0, "
            "created_at = time::now(), updated_at = time::now()",
            {"pid": pid, "content": content},
        )
    finally:
        st._q(
            "DEFINE FIELD OVERWRITE directory_context ON TABLE memory TYPE string "
            "ASSERT $value != NONE AND string::len($value) > 0"
        )


# ── T1: field-absent wiki_page row gets correct heuristic value ───────────────


class TestMigration018BackfillsFieldAbsentWikiPage:
    """T1 — migration 018 assigns correct directory to field-absent rows."""

    def test_yadgar_tagged_row_gets_yadgar_directory(self):
        """Row tagged 'yadgar' → /home/max/git/yadgar."""
        slug = _insert_legacy_wiki_page("Yadgar Roadmap Legacy", ["yadgar", "wiki"])
        st = _storage()

        # Confirm field is absent (not just NONE)
        rows = st._q("SELECT directory_context FROM wiki_page WHERE slug = $s", {"s": slug})
        assert rows, "Row should exist"
        # Field-absent rows: the key is missing from the dict entirely
        assert rows[0].get("directory_context") is None, (
            "Row should have no directory_context key (field-absent) before migration 018"
        )

        _migration_018_directory_context_backfill_repair(st)

        rows_after = st._q("SELECT directory_context FROM wiki_page WHERE slug = $s", {"s": slug})
        assert rows_after[0].get("directory_context") == "/home/max/git/yadgar"

    def test_aws_tagged_row_gets_aws_work_directory(self):
        """Row tagged with AWS infra tag → /home/max/aws-work."""
        slug = _insert_legacy_wiki_page("IAM Policy Audit Legacy", ["iam", "aws", "inventory"])
        st = _storage()

        _migration_018_directory_context_backfill_repair(st)

        rows = st._q("SELECT directory_context FROM wiki_page WHERE slug = $s", {"s": slug})
        assert rows[0].get("directory_context") == "/home/max/aws-work"

    def test_untagged_row_gets_global(self):
        """Row with no recognisable tags → 'global'."""
        slug = _insert_legacy_wiki_page("Random Notes Legacy", ["misc"])
        st = _storage()

        _migration_018_directory_context_backfill_repair(st)

        rows = st._q("SELECT directory_context FROM wiki_page WHERE slug = $s", {"s": slug})
        assert rows[0].get("directory_context") == "global"

    def test_already_backfilled_row_is_untouched(self):
        """Row with a valid directory_context already set is not overwritten."""
        st = _storage()
        # Insert a properly-formed row via the normal helper (has directory_context).
        st.insert_wiki_page(
            {
                "slug": "already-backfilled",
                "title": "Already Backfilled",
                "content": "test",
                "category": "reference",
                "tags": ["yadgar"],
                "links": [],
                "source_memory_ids": [],
                "confidence": "medium",
                "directory_context": "/home/max/git/yadgar",
            }
        )

        _migration_018_directory_context_backfill_repair(st)

        rows = st._q("SELECT directory_context FROM wiki_page WHERE slug = 'already-backfilled'")
        # Must not be changed — still the original value
        assert rows[0].get("directory_context") == "/home/max/git/yadgar"


# ── T2: idempotency ───────────────────────────────────────────────────────────


class TestMigration018Idempotent:
    """T2 — migration 018 can run twice without error or double-counting."""

    def test_double_run_no_error(self):
        """Running migration 018 twice raises no exception."""
        slug = _insert_legacy_wiki_page("Idempotent Test Legacy", ["yadgar"])
        st = _storage()

        _migration_018_directory_context_backfill_repair(st)
        # Second run: field is no longer absent — should be skipped
        _migration_018_directory_context_backfill_repair(st)

        rows = st._q("SELECT directory_context FROM wiki_page WHERE slug = $s", {"s": slug})
        assert rows[0].get("directory_context") == "/home/max/git/yadgar"

    def test_double_run_row_count_unchanged(self):
        """Row count remains the same after two runs."""
        st = _storage()
        _insert_legacy_wiki_page("Count Test A", ["yadgar"], slug="count-test-a")
        _insert_legacy_wiki_page("Count Test B", ["iam"], slug="count-test-b")

        before = st._q("SELECT count() AS c FROM wiki_page GROUP ALL")
        _migration_018_directory_context_backfill_repair(st)
        _migration_018_directory_context_backfill_repair(st)
        after = st._q("SELECT count() AS c FROM wiki_page GROUP ALL")

        assert before[0]["c"] == after[0]["c"]


# ── T3: wiki_list returns backfilled rows ─────────────────────────────────────


class TestMigration018UnbricksWikiList:
    """T3 — after migration 018, wiki_list(directory=...) returns previously-absent rows."""

    def test_wiki_list_yadgar_returns_backfilled_rows(self):
        """wiki_list(directory=/home/max/git/yadgar) returns yadgar-tagged legacy pages."""
        from yadgar.core.server.tools.wiki import wiki_list

        _insert_legacy_wiki_page("Legacy Yadgar Doc", ["yadgar", "wiki"], slug="legacy-yadgar-doc")
        st = _storage()

        # Before migration: this page has no directory_context, so wiki_list returns empty
        pages_before = wiki_list(directory="/home/max/git/yadgar")
        slugs_before = [p["slug"] for p in pages_before]
        assert "legacy-yadgar-doc" not in slugs_before, (
            "Pre-migration: legacy row should not appear in directory-scoped list"
        )

        _migration_018_directory_context_backfill_repair(st)

        pages_after = wiki_list(directory="/home/max/git/yadgar")
        slugs_after = [p["slug"] for p in pages_after]
        assert "legacy-yadgar-doc" in slugs_after, (
            "Post-migration: backfilled row should appear in wiki_list"
        )


# ── T4: memory rows backfilled ────────────────────────────────────────────────


class TestMigration018BackfillsMemoryRows:
    """T4 — migration 018 backfills field-absent memory rows to 'global'."""

    def test_field_absent_memory_gets_global(self):
        """Memory row without directory_context field gets backfilled to 'global'."""
        st = _storage()
        _insert_legacy_memory("some legacy memory content")

        # Confirm field is absent before migration
        rows = st._q("SELECT directory_context FROM memory")
        assert rows, "Memory row should exist"
        dc_before = rows[0].get("directory_context")
        assert dc_before is None, (
            f"Memory row should lack directory_context before migration; got {dc_before!r}"
        )

        _migration_018_directory_context_backfill_repair(st)

        rows_after = st._q("SELECT directory_context FROM memory")
        for row in rows_after:
            assert row.get("directory_context") is not None and row["directory_context"] != "", (
                f"All memory rows should have non-empty directory_context after migration; "
                f"got {row.get('directory_context')!r}"
            )


# ── T5: migration 016 source fix (Python-side filter catches field-absent rows) ──


class TestMigration016SourceFix:
    """T5 — the corrected migration 016 Phase A catches field-absent rows.

    This test verifies the IS NONE bug: the original query misses rows where the
    field is completely absent. The fix fetches all rows and filters in Python.
    We test this by directly calling the corrected _migration_016 function
    (which must include the Python-side filter) against a schema that has NOT
    yet had the directory_context DEFINE FIELD applied.
    """

    def test_python_filter_catches_field_absent_row(self):
        """After 016 with Python-side filter, field-absent rows are backfilled."""
        st = _storage()
        _insert_legacy_wiki_page("016 Source Fix Test", ["yadgar"], slug="016-src-fix-test")

        # Verify field is absent before migration
        rows = st._q("SELECT directory_context FROM wiki_page WHERE slug = '016-src-fix-test'")
        assert rows[0].get("directory_context") is None

        # Run 016 (which must use Python-filter in its corrected form)
        _migration_016_directory_context(st)

        rows_after = st._q(
            "SELECT directory_context FROM wiki_page WHERE slug = '016-src-fix-test'"
        )
        assert rows_after[0].get("directory_context") == "/home/max/git/yadgar", (
            "migration 016 Phase A with Python-side filter should backfill field-absent row"
        )
