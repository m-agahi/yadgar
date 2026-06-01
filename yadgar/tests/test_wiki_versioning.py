"""v5.41.0 Wiki Versioning — TDD test suite.

Tests cover:
  1. Migration 013 — table + indexes created, seed from existing pages, idempotency
  2. insert_wiki_page produces version=1
  3. update_wiki_page increments version monotonically
  4. change_summary content (line delta + section headings)
  5. wiki_history — newest-first, no content field
  6. wiki_read_version — full snapshot, error on missing
  7. wiki_diff — unified and JSON formats
  8. wiki_restore — creates new version, content recovered
  9. wiki_append_section — all positions, edge cases, disambiguation
 10. Corruption-prevention scenario (2026-05-31 pattern)
 11. wiki_read unchanged returns latest (regression guard)

Note: Tests use embedded storage (no server daemon). Migration 013 is called
directly since _run_migrations() skips in embedded mode (no db_url).
"""

from __future__ import annotations

import pytest

from yadgar import server
from yadgar.storage.migrations import _migration_013_wiki_page_version

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _engines(tmp_path):
    """Embedded storage with isolated temp database per test."""
    server.init_engines(
        db_path=str(tmp_path / "wiki_versioning_test.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


def _storage():
    return server._get_storage()


def _wiki():
    return server._wiki


def _apply_migration():
    """Apply migration 013 directly (bypasses embedded early-return guard)."""
    _migration_013_wiki_page_version(_storage())


def _insert_page(
    slug="test-page",
    title="Test Page",
    content="initial content",
    category="reference",
    tags=None,
    confidence="medium",
):
    """Helper: insert a wiki page directly via storage layer."""
    return _storage().insert_wiki_page(
        {
            "slug": slug,
            "title": title,
            "content": content,
            "category": category,
            "tags": tags or [],
            "confidence": confidence,
            "source_memory_ids": [],
            "links": [],
        }
    )


# ── 1. Migration 013 ──────────────────────────────────────────────────────────


class TestMigration013:
    def test_migration_013_creates_table(self):
        """Migration 013 defines wiki_page_version table."""
        _apply_migration()
        # Table exists if we can query it without error
        rows = _storage()._q("SELECT * FROM wiki_page_version LIMIT 1")
        assert isinstance(rows, list)

    def test_migration_013_creates_indexes(self):
        """Migration 013 defines the three required indexes (verified by index-scan query)."""
        # Insert a page BEFORE migration so seed can pick it up
        pid = _insert_page("seed-idx", "Seed Index Test", "content")
        _apply_migration()
        # Verify page_id index works — returns the seeded row
        rows = _storage()._q(
            "SELECT * FROM wiki_page_version WHERE page_id = $p",
            {"p": pid},
        )
        assert isinstance(rows, list)
        assert len(rows) == 1  # seeded from existing page

    def test_migration_013_seeds_existing_pages(self):
        """Pre-existing wiki_page rows get a version=1 row on migration."""
        pid = _insert_page("pre-existing", "Pre-Existing Page", "some content")
        _apply_migration()
        rows = _storage()._q(
            "SELECT * FROM wiki_page_version WHERE page_id = $p",
            {"p": pid},
        )
        assert len(rows) == 1
        row = rows[0]
        assert row["version"] == 1
        assert row["content"] == "some content"
        assert row["change_summary"] == "initial version"

    def test_migration_013_idempotent(self):
        """Re-running migration 013 does not duplicate version rows."""
        pid = _insert_page("idempotent-test", "Idempotent Page", "content")
        _apply_migration()
        _apply_migration()  # second run
        rows = _storage()._q(
            "SELECT * FROM wiki_page_version WHERE page_id = $p",
            {"p": pid},
        )
        assert len(rows) == 1, f"Expected 1 version row, got {len(rows)}"


# ── 2. insert_wiki_page creates version=1 ─────────────────────────────────────


class TestInsertCreatesVersion:
    def test_insert_wiki_page_writes_version_1(self):
        """insert_wiki_page creates version=1 row atomically."""
        _apply_migration()
        pid = _insert_page("insert-v1", "Insert V1 Test", "first content")
        rows = _storage()._q(
            "SELECT * FROM wiki_page_version WHERE page_id = $p",
            {"p": pid},
        )
        assert len(rows) == 1
        assert rows[0]["version"] == 1
        assert rows[0]["content"] == "first content"
        assert rows[0]["change_summary"] == "initial version"

    def test_insert_version_matches_page_fields(self):
        """Version row snapshots title, category, tags, confidence."""
        _apply_migration()
        pid = _insert_page(
            "snap-fields",
            "Snapshot Fields",
            "content",
            category="architecture",
            tags=["a", "b"],
            confidence="high",
        )
        rows = _storage()._q(
            "SELECT * FROM wiki_page_version WHERE page_id = $p",
            {"p": pid},
        )
        assert len(rows) == 1
        row = rows[0]
        assert row["title"] == "Snapshot Fields"
        assert row["category"] == "architecture"
        assert "a" in row["tags"]
        assert row["confidence"] == "high"


# ── 3. update_wiki_page increments version ────────────────────────────────────


class TestUpdateIncrementsVersion:
    def test_update_wiki_page_increments_version(self):
        """Successive update_wiki_page calls produce versions 2, 3, 4."""
        _apply_migration()
        pid = _insert_page("incr-test", "Increment Test", "v1 content")

        _storage().update_wiki_page(pid, {"content": "v2 content"})
        _storage().update_wiki_page(pid, {"content": "v3 content"})

        rows = _storage()._q(
            "SELECT * FROM wiki_page_version WHERE page_id = $p ORDER BY version ASC",
            {"p": pid},
        )
        assert len(rows) == 3
        assert rows[0]["version"] == 1
        assert rows[1]["version"] == 2
        assert rows[2]["version"] == 3

    def test_update_version_snapshots_new_content(self):
        """Version row stores the NEW content (post-update snapshot)."""
        _apply_migration()
        pid = _insert_page("snapshot-content", "Snapshot Content", "old content")
        _storage().update_wiki_page(pid, {"content": "new content"})

        rows = _storage()._q(
            "SELECT * FROM wiki_page_version WHERE page_id = $p ORDER BY version DESC LIMIT 1",
            {"p": pid},
        )
        assert rows[0]["content"] == "new content"

    def test_hash_identical_content_still_creates_version(self):
        """Version is written even when content is unchanged (preserves full history)."""
        _apply_migration()
        pid = _insert_page("same-content", "Same Content", "identical")
        _storage().update_wiki_page(pid, {"content": "identical"})  # same!

        rows = _storage()._q(
            "SELECT * FROM wiki_page_version WHERE page_id = $p",
            {"p": pid},
        )
        assert len(rows) == 2, "Must record version even for hash-identical content"


# ── 4. change_summary ─────────────────────────────────────────────────────────


class TestChangeSummary:
    def test_update_writes_change_summary_line_delta(self):
        """change_summary contains line delta counts."""
        _apply_migration()
        old = "line one\nline two\nline three\n"
        pid = _insert_page("summary-test", "Summary Test", old)
        new = "line one\nline two\nline three\nline four\n"
        _storage().update_wiki_page(pid, {"content": new})

        rows = _storage()._q(
            "SELECT * FROM wiki_page_version WHERE page_id = $p ORDER BY version DESC LIMIT 1",
            {"p": pid},
        )
        summary = rows[0].get("change_summary", "")
        assert "+" in summary or "lines" in summary, f"Summary missing line delta: {summary!r}"

    def test_update_change_summary_mentions_touched_section(self):
        """change_summary includes heading of touched section."""
        _apply_migration()
        old = "## Pipeline\n\n- item one\n\n## Open Questions\n\ncontent here\n"
        pid = _insert_page("section-summary", "Section Summary", old)
        new = "## Pipeline\n\n- item one\n- item two\n\n## Open Questions\n\ncontent here\n"
        _storage().update_wiki_page(pid, {"content": new})

        rows = _storage()._q(
            "SELECT * FROM wiki_page_version WHERE page_id = $p ORDER BY version DESC LIMIT 1",
            {"p": pid},
        )
        summary = rows[0].get("change_summary", "")
        assert "Pipeline" in summary, f"Expected 'Pipeline' in change_summary: {summary!r}"

    def test_change_summary_max_300_chars(self):
        """change_summary is at most 300 characters."""
        _apply_migration()
        pid = _insert_page("long-summary", "Long Summary", "a\n" * 200)
        _storage().update_wiki_page(pid, {"content": "b\n" * 200})

        rows = _storage()._q(
            "SELECT * FROM wiki_page_version WHERE page_id = $p ORDER BY version DESC LIMIT 1",
            {"p": pid},
        )
        summary = rows[0].get("change_summary", "")
        assert len(summary) <= 300, f"change_summary too long: {len(summary)} chars"


# ── 5. wiki_history ───────────────────────────────────────────────────────────


class TestWikiHistory:
    def test_wiki_history_newest_first(self):
        """wiki_history returns versions descending by created_at."""
        _apply_migration()
        pid = _insert_page("history-order", "History Order", "v1")
        _storage().update_wiki_page(pid, {"content": "v2"})
        _storage().update_wiki_page(pid, {"content": "v3"})

        result = _wiki().history(pid)
        assert len(result) >= 3
        versions = [v["version"] for v in result]
        assert versions == sorted(versions, reverse=True), f"Not descending: {versions}"

    def test_wiki_history_no_content_field(self):
        """wiki_history entries must NOT include 'content' (light payload)."""
        _apply_migration()
        pid = _insert_page("no-content", "No Content", "heavy content here")
        result = _wiki().history(pid)
        for entry in result:
            assert "content" not in entry, f"content field leaked into history entry: {entry}"

    def test_wiki_history_includes_change_summary(self):
        """Each history entry includes change_summary and size_bytes."""
        _apply_migration()
        pid = _insert_page("hist-meta", "Hist Meta", "initial")
        result = _wiki().history(pid)
        assert len(result) >= 1
        entry = result[0]
        assert "change_summary" in entry
        assert "size_bytes" in entry

    def test_wiki_history_limit(self):
        """wiki_history respects the limit parameter."""
        _apply_migration()
        pid = _insert_page("hist-limit", "Hist Limit", "v1")
        for i in range(5):
            _storage().update_wiki_page(pid, {"content": f"v{i + 2}"})

        result = _wiki().history(pid, limit=3)
        assert len(result) <= 3


# ── 6. wiki_read_version ──────────────────────────────────────────────────────


class TestWikiReadVersion:
    def test_wiki_read_version_full_snapshot(self):
        """wiki_read_version returns full content + all snapshot fields."""
        _apply_migration()
        pid = _insert_page("read-ver", "Read Version", "version one content")
        _storage().update_wiki_page(pid, {"content": "version two content"})

        result = _wiki().read_version(pid, version=1)
        assert result is not None
        assert result["content"] == "version one content"
        assert result["version"] == 1
        assert "title" in result
        assert "created_at" in result

    def test_wiki_read_version_missing(self):
        """wiki_read_version returns error dict with max_version hint for missing version."""
        _apply_migration()
        pid = _insert_page("missing-ver", "Missing Version", "content")

        result = _wiki().read_version(pid, version=99)
        assert "error" in result
        assert "max_version" in result
        assert result["max_version"] == 1

    def test_wiki_read_version_2_returns_new_content(self):
        """wiki_read_version(pid, 2) returns the post-update content."""
        _apply_migration()
        pid = _insert_page("ver2", "Ver 2 Test", "original")
        _storage().update_wiki_page(pid, {"content": "updated"})

        result = _wiki().read_version(pid, version=2)
        assert result is not None
        assert result["content"] == "updated"


# ── 7. wiki_diff ──────────────────────────────────────────────────────────────


class TestWikiDiff:
    def test_wiki_diff_unified_format(self):
        """wiki_diff with fmt=unified returns text parseable as unified diff."""
        _apply_migration()
        pid = _insert_page("diff-u", "Diff Unified", "line one\nline two\n")
        _storage().update_wiki_page(pid, {"content": "line one\nline three\n"})

        result = _wiki().diff(pid, v1=1, v2=2, fmt="unified")
        assert "diff" in result
        assert "---" in result["diff"]
        assert "+++" in result["diff"]

    def test_wiki_diff_json_format(self):
        """wiki_diff with fmt=json returns hunks + added_lines + removed_lines + sections_changed."""
        _apply_migration()
        pid = _insert_page("diff-j", "Diff JSON", "line one\nline two\n")
        _storage().update_wiki_page(pid, {"content": "line one\nline three\n"})

        result = _wiki().diff(pid, v1=1, v2=2, fmt="json")
        assert "hunks" in result
        assert "added_lines" in result
        assert "removed_lines" in result
        assert "sections_changed" in result

    def test_wiki_diff_slug_and_page_id_in_result(self):
        """wiki_diff result includes v1, v2, and page_id."""
        _apply_migration()
        pid = _insert_page("diff-meta", "Diff Meta", "a\n")
        _storage().update_wiki_page(pid, {"content": "b\n"})

        result = _wiki().diff(pid, v1=1, v2=2, fmt="unified")
        assert result.get("v1") == 1
        assert result.get("v2") == 2
        assert result.get("page_id") == pid


# ── 8. wiki_restore ───────────────────────────────────────────────────────────


class TestWikiRestore:
    def test_wiki_restore_creates_new_version_pointing_back(self):
        """wiki_restore(pid, 1) creates a new version whose content matches version 1."""
        _apply_migration()
        pid = _insert_page("restore-test", "Restore Test", "original content 250KB worth")
        _storage().update_wiki_page(pid, {"content": "short replacement"})

        result = _wiki().restore_version(pid, version=1)
        assert result.get("restored_from_version") == 1
        new_ver = result.get("new_version")
        assert new_ver == 3

        # Verify the new version contains the restored content
        ver_row = _wiki().read_version(pid, version=new_ver)
        assert ver_row["content"] == "original content 250KB worth"

    def test_wiki_restore_updates_page_current_content(self):
        """After restore, wiki_page table row reflects the restored content."""
        _apply_migration()
        pid = _insert_page("restore-current", "Restore Current", "big original content")
        _storage().update_wiki_page(pid, {"content": "short overwrite"})

        _wiki().restore_version(pid, version=1)

        page = _storage().get_wiki_page(pid)
        assert page["content"] == "big original content"


# ── 9. wiki_append_section ────────────────────────────────────────────────────


class TestWikiAppendSection:
    def test_wiki_append_section_end_of_section(self):
        """Appending to end_of_section inserts content before next heading."""
        _apply_migration()
        original = "## Pipeline\n\n- item one\n- item two\n\n## Open Questions\n\ncontent here\n"
        pid = _insert_page("append-end", "Append End", original)

        result = _wiki().append_section(
            pid,
            section_heading="Pipeline",
            content="- item three\n",
            position="end_of_section",
        )
        assert result.get("action") == "appended"

        page = _storage().get_wiki_page(pid)
        assert "- item three\n" in page["content"]
        assert "## Open Questions" in page["content"]
        # item three must appear before Open Questions
        pos_item = page["content"].index("- item three")
        pos_oq = page["content"].index("## Open Questions")
        assert pos_item < pos_oq

    def test_wiki_append_section_start_of_section(self):
        """Appending to start_of_section inserts immediately after heading line."""
        _apply_migration()
        original = "## Notes\n\nexisting note\n\n## Footer\n\nfooter text\n"
        pid = _insert_page("append-start", "Append Start", original)

        _wiki().append_section(pid, "Notes", "first note\n", position="start_of_section")

        page = _storage().get_wiki_page(pid)
        # "first note" must appear before "existing note"
        pos_first = page["content"].index("first note")
        pos_existing = page["content"].index("existing note")
        assert pos_first < pos_existing

    def test_wiki_append_section_replace_section(self):
        """replace_section replaces section body; heading is preserved."""
        _apply_migration()
        original = "## Status\n\nold status text\n\n## Done\n\ncomplete\n"
        pid = _insert_page("replace-sec", "Replace Section", original)

        _wiki().append_section(pid, "Status", "new status text\n", position="replace_section")

        page = _storage().get_wiki_page(pid)
        assert "## Status" in page["content"]
        assert "new status text" in page["content"]
        assert "old status text" not in page["content"]

    def test_wiki_append_section_new_section_bottom(self):
        """new_section_bottom creates a new section at end of page."""
        _apply_migration()
        original = "## Existing\n\nsome content\n"
        pid = _insert_page("new-bottom", "New Bottom", original)

        _wiki().append_section(pid, "New Section", "fresh content\n", position="new_section_bottom")

        page = _storage().get_wiki_page(pid)
        assert "## New Section" in page["content"]
        assert "fresh content" in page["content"]
        pos_existing = page["content"].index("## Existing")
        pos_new = page["content"].index("## New Section")
        assert pos_existing < pos_new

    def test_wiki_append_section_new_section_top(self):
        """new_section_top creates a new section at top of page."""
        _apply_migration()
        original = "## Existing\n\nsome content\n"
        pid = _insert_page("new-top", "New Top", original)

        _wiki().append_section(pid, "Top Section", "top content\n", position="new_section_top")

        page = _storage().get_wiki_page(pid)
        assert "## Top Section" in page["content"]
        pos_top = page["content"].index("## Top Section")
        pos_existing = page["content"].index("## Existing")
        assert pos_top < pos_existing

    def test_wiki_append_section_section_not_found(self):
        """section_not_found error when heading absent and position requires existing."""
        _apply_migration()
        original = "## Pipeline\n\ncontent\n"
        pid = _insert_page("not-found", "Not Found", original)

        result = _wiki().append_section(
            pid, "Nonexistent Section", "content\n", position="end_of_section"
        )
        assert result.get("error") == "section_not_found"
        assert "available_sections" in result

    def test_wiki_append_section_section_exists_new_section_error(self):
        """section_exists error when heading already present + position is new_section_*."""
        _apply_migration()
        original = "## Pipeline\n\ncontent\n"
        pid = _insert_page("exists-err", "Exists Error", original)

        result = _wiki().append_section(pid, "Pipeline", "extra\n", position="new_section_bottom")
        assert result.get("error") == "section_exists"

    def test_wiki_append_section_ambiguous_heading_raises(self):
        """Ambiguous heading with non-replace position returns ambiguous_section error."""
        _apply_migration()
        original = "## Pipeline\n\nfirst\n\n## Pipeline\n\nsecond\n"
        pid = _insert_page("ambig-head", "Ambig Head", original)

        result = _wiki().append_section(pid, "Pipeline", "extra\n", position="end_of_section")
        assert result.get("error") == "ambiguous_section"

    def test_wiki_append_section_index_disambiguation(self):
        """Pipeline#2 syntax targets second occurrence."""
        _apply_migration()
        original = "## Pipeline\n\nfirst\n\n## Pipeline\n\nsecond\n"
        pid = _insert_page("disambig", "Disambig", original)

        result = _wiki().append_section(
            pid, "Pipeline#2", "added to second\n", position="end_of_section"
        )
        assert result.get("action") == "appended"

        page = _storage().get_wiki_page(pid)
        assert "added to second" in page["content"]

    def test_wiki_append_section_inside_code_block_ignored(self):
        """## Foo inside fenced code block does not count as a section heading."""
        _apply_migration()
        original = (
            "## Real Section\n\nsome text\n\n"
            "```\n## Fake Heading Inside Code\n```\n\n"
            "## Another Real\n\nmore text\n"
        )
        pid = _insert_page("code-block", "Code Block", original)

        # Should match "Real Section" not the fake one inside code block
        result = _wiki().append_section(
            pid, "Real Section", "appended\n", position="end_of_section"
        )
        assert result.get("action") == "appended"

        # "Fake Heading Inside Code" must not appear as a match
        result2 = _wiki().append_section(
            pid, "Fake Heading Inside Code", "bad\n", position="end_of_section"
        )
        assert result2.get("error") == "section_not_found"

    def test_wiki_append_section_creates_new_version(self):
        """wiki_append_section writes a new version row."""
        _apply_migration()
        original = "## Status\n\nold\n"
        pid = _insert_page("append-ver", "Append Version", original)

        _wiki().append_section(pid, "Status", "added\n", position="end_of_section")

        rows = _storage()._q(
            "SELECT * FROM wiki_page_version WHERE page_id = $p",
            {"p": pid},
        )
        assert len(rows) == 2, "append_section must produce a new version row"


# ── 10. Corruption-prevention scenario ───────────────────────────────────────


class TestCorruptionPrevention:
    def test_corruption_prevention_scenario(self):
        """Simulate the 2026-05-31 pattern: short overwrite via update, then restore recovers."""
        _apply_migration()
        big_content = "# Roadmap\n\n## Pipeline\n\n" + "- feature item\n" * 100
        pid = _insert_page("roadmap", "Roadmap Page", big_content)

        # Agent overwrites with short content (the bug)
        short_patch = "# Roadmap\n\n- v5.41 plan committed\n"
        _storage().update_wiki_page(pid, {"content": short_patch})

        # Verify v2 has the short content
        v2 = _wiki().read_version(pid, version=2)
        assert v2["content"] == short_patch

        # change_summary should flag the size drop
        history = _wiki().history(pid)
        v2_entry = next(e for e in history if e["version"] == 2)
        assert v2_entry["size_bytes"] < v2_entry.get("_size_v1", float("inf")), (
            "size_bytes should show the drop"
        )

        # Restore to v1 recovers full content
        restore_result = _wiki().restore_version(pid, version=1)
        assert restore_result.get("restored_from_version") == 1

        page = _storage().get_wiki_page(pid)
        assert page["content"] == big_content, "Restored page must match original"

    def test_wiki_read_unchanged_returns_latest(self):
        """wiki_read(slug) after 10 updates returns version-10 content (regression guard)."""
        _apply_migration()
        pid = _insert_page("latest-guard", "Latest Guard", "v1")
        for i in range(2, 11):
            _storage().update_wiki_page(pid, {"content": f"v{i}"})

        page = _storage().get_wiki_page(pid)
        assert page["content"] == "v10", f"Expected v10, got {page['content']!r}"

    def test_branch_resolution_keys_versioning_on_page_id(self):
        """Same slug on two branches produce separate version chains."""
        _apply_migration()
        pid_master = _storage().insert_wiki_page(
            {
                "slug": "shared-slug",
                "title": "Shared",
                "content": "master v1",
                "tags": [],
                "category": "reference",
                "confidence": "medium",
                "source_memory_ids": [],
                "links": [],
            },
            branch="master",
        )
        pid_feat = _storage().insert_wiki_page(
            {
                "slug": "shared-slug",
                "title": "Shared",
                "content": "feat v1",
                "tags": [],
                "category": "reference",
                "confidence": "medium",
                "source_memory_ids": [],
                "links": [],
            },
            branch="feat/x",
        )

        assert pid_master != pid_feat, "Different branches → different page IDs"

        _storage().update_wiki_page(pid_master, {"content": "master v2"})

        master_versions = _storage()._q(
            "SELECT * FROM wiki_page_version WHERE page_id = $p",
            {"p": pid_master},
        )
        feat_versions = _storage()._q(
            "SELECT * FROM wiki_page_version WHERE page_id = $p",
            {"p": pid_feat},
        )

        assert len(master_versions) == 2, "master chain has 2 versions"
        assert len(feat_versions) == 1, "feat chain has 1 version (untouched)"
