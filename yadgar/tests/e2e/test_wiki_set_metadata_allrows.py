"""E2E tests for BC-G10: wiki_set_metadata must update ALL rows of a slug.

Root cause (fixed in this PR): _resolve_page_id_by_slug returns ONE page_id
via LIMIT 1 → only one row updated. A slug can have MANY rows (per-branch +
global stragglers). The fix introduces WikiStore.set_metadata_by_slug +
storage.get_wiki_page_ids_by_slug.

Test design:
  - RED on current (unfixed) code: only 1 of ≥2 rows updated.
  - GREEN after fix: all rows for the slug carry the new value.

Placement: yadgar/tests/e2e/ → collected by `make e2e`.
Uses @pytest.mark.e2e for live-surreal DB.

BC-G10 ref: docs/BEHAVIOR_CONTRACT.md
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e

# Canonical test project dir (must be a real absolute path)
YADGAR_DIR = "/home/test/yadgar-project"


def _insert_wiki_page_direct(storage, slug: str, directory_context: str, branch=None) -> int:
    """Seed a wiki_page row directly via storage (bypasses WikiStore add() de-dup).

    Returns the integer page_id. Used to manufacture the multi-row/straggler
    state that triggers BC-G10 (same slug, different directory_context/branch).
    """
    return storage.insert_wiki_page(
        {
            "slug": slug,
            "title": f"Test page {slug}",
            "content": f"content for {slug} in {directory_context}",
            "category": "reference",
            "tags": [],
            "confidence": "high",
            "source_memory_ids": [],
            "links": [],
            "directory_context": directory_context,
        },
        branch=branch,
    )


class TestWikiSetMetadataAllRows:
    """BC-G10: wiki_set_metadata reaches ALL rows of a slug (multi-row fix)."""

    def test_set_metadata_updates_all_rows_for_slug(self, e2e_engines, monkeypatch):
        """Calling wiki_set_metadata once updates EVERY row sharing that slug.

        Discriminating test:
          - Seed 2 rows with the same slug but different directory_context.
          - Call wiki_set_metadata once.
          - Assert BOTH rows now have the new directory_context.
          - RED on old code (only 1 row changes); GREEN after fix.

        Ref: BC-G10.
        """
        from yadgar.core.server.tools.wiki import wiki_set_metadata

        monkeypatch.setattr("yadgar.core.server._detect_branch", lambda _d: "master")
        monkeypatch.setattr("yadgar.core.server._get_default_branch", lambda _d: "master")

        storage = e2e_engines["storage"]

        slug = "bc-g10-allrows-test-slug"
        target_dir = "/home/test/project"

        # Seed row 1: global straggler (directory_context="global", branch=None)
        pid1 = _insert_wiki_page_direct(storage, slug, directory_context="global", branch=None)

        # Seed row 2: project-scoped row (directory_context=YADGAR_DIR, branch="master")
        pid2 = _insert_wiki_page_direct(
            storage, slug, directory_context=YADGAR_DIR, branch="master"
        )

        assert pid1 != pid2, "Expected two distinct page_ids for the same slug"

        # Confirm initial state: rows have DIFFERENT directory_context
        page1_before = storage.get_wiki_page(pid1)
        page2_before = storage.get_wiki_page(pid2)
        assert page1_before["directory_context"] == "global"
        assert page2_before["directory_context"] == YADGAR_DIR

        # Call the MCP tool once — should reach ALL rows
        result = wiki_set_metadata(slug, "directory_context", target_dir)

        assert result.get("ok") is True, f"wiki_set_metadata returned error: {result}"
        assert result.get("slug") == slug

        # Assert BOTH rows now carry the new directory_context
        page1_after = storage.get_wiki_page(pid1)
        page2_after = storage.get_wiki_page(pid2)

        assert page1_after["directory_context"] == target_dir, (
            f"Row pid1={pid1} (was 'global') still has "
            f"directory_context={page1_after['directory_context']!r}; "
            f"expected {target_dir!r}. "
            f"This is RED on old code (LIMIT 1 in _resolve_page_id_by_slug misses this row)."
        )
        assert page2_after["directory_context"] == target_dir, (
            f"Row pid2={pid2} (was {YADGAR_DIR!r}) still has "
            f"directory_context={page2_after['directory_context']!r}; "
            f"expected {target_dir!r}."
        )

    def test_rows_updated_count_reflects_actual_changes(self, e2e_engines, monkeypatch):
        """rows_updated in the result reflects the count of rows that changed.

        Seeds 3 rows for the same slug, all with different directory_context.
        After the call, rows_updated must be 3 (all changed), page_ids must
        list all 3 page_ids.

        Ref: BC-G10.
        """
        from yadgar.core.server.tools.wiki import wiki_set_metadata

        monkeypatch.setattr("yadgar.core.server._detect_branch", lambda _d: "master")
        monkeypatch.setattr("yadgar.core.server._get_default_branch", lambda _d: "master")

        storage = e2e_engines["storage"]

        slug = "bc-g10-three-rows-slug"
        target_dir = "/home/test/target-project"

        pid1 = _insert_wiki_page_direct(storage, slug, "global", branch=None)
        pid2 = _insert_wiki_page_direct(storage, slug, YADGAR_DIR, branch="master")
        pid3 = _insert_wiki_page_direct(storage, slug, "/home/test/other-dir", branch="feat/x")

        result = wiki_set_metadata(slug, "directory_context", target_dir)

        assert result.get("ok") is True, f"Expected ok:True, got: {result}"
        assert result.get("rows_updated") == 3, (
            f"Expected rows_updated=3 (all 3 rows changed), got {result.get('rows_updated')}. "
            f"Full result: {result}"
        )
        returned_ids = set(result.get("page_ids", []))
        assert {pid1, pid2, pid3} == returned_ids, (
            f"Expected page_ids={{{pid1},{pid2},{pid3}}}, got {returned_ids}"
        )

        # Verify all 3 rows have the new value in DB
        for pid in (pid1, pid2, pid3):
            page = storage.get_wiki_page(pid)
            assert page["directory_context"] == target_dir, (
                f"pid={pid} still has directory_context={page['directory_context']!r}"
            )

    def test_idempotent_noop_rows_updated_zero(self, e2e_engines, monkeypatch):
        """rows_updated=0 when all rows already have the target value (idempotent).

        Ref: BC-G10.
        """
        from yadgar.core.server.tools.wiki import wiki_set_metadata

        monkeypatch.setattr("yadgar.core.server._detect_branch", lambda _d: "master")
        monkeypatch.setattr("yadgar.core.server._get_default_branch", lambda _d: "master")

        storage = e2e_engines["storage"]

        slug = "bc-g10-idempotent-slug"
        target_dir = "/home/test/already-set-dir"

        # Both rows already carry target_dir
        _insert_wiki_page_direct(storage, slug, target_dir, branch=None)
        _insert_wiki_page_direct(storage, slug, target_dir, branch="master")

        result = wiki_set_metadata(slug, "directory_context", target_dir)

        assert result.get("ok") is True, f"Expected ok:True on no-op, got: {result}"
        assert result.get("rows_updated") == 0, (
            f"Expected rows_updated=0 (idempotent, nothing changed), "
            f"got {result.get('rows_updated')}. Full result: {result}"
        )
