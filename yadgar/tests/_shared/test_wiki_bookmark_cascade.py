"""Task #341: ``delete_wiki_page`` must cascade-delete matching ``wiki_bookmark`` rows.

Pre-task behaviour: deleting a wiki page left its bookmark row behind, so the
viz couldn't render a remove control and the bookmark referenced a slug that
no longer had a backing page. Two such dangling bookmarks exist in the live
corpus at the time the task was filed.

This test seeds a wiki_page + wiki_bookmark pair, deletes the page by id via
the storage layer, and asserts the wiki_bookmark row is gone. A second test
verifies the cascade scopes to the deleted page's slug and does not wipe
bookmarks of unrelated pages.
"""

from __future__ import annotations

import pytest

from yadgar._shared.storage import StorageEngine

# C13 (0047 PR#40 §5): seeds must NAME the project they write into —
# C5 deleted every fallback that used to answer an unnamed write (ADR-0227).
_PROJECT = "m-agahi/yadgar"


@pytest.fixture(scope="module")
def storage(module_storage):  # noqa: ARG001 — delegation pattern
    return module_storage


def _insert_wiki_page(
    storage: StorageEngine,
    slug: str,
    *,
    directory_context: str = "/tmp/cascade-test",
) -> int:
    """Seed a wiki_page row. ``_sanctioned=True`` so the test isn't gated."""
    page: dict = {
        "slug": slug,
        "title": f"Test page {slug}",
        "content": f"# {slug}\n\nbody",
        "category": "reference",
        "tags": [],
        "confidence": "high",
        "source_memory_ids": [],
        "links": [],
        "directory_context": directory_context,
        "project_id": _PROJECT,
        "_sanctioned": True,
    }
    return storage.insert_wiki_page(page)


class TestWikiBookmarkCascade:
    """Deleting a wiki_page row must also delete any wiki_bookmark row whose
    slug matches the deleted page."""

    def test_delete_wiki_page_cascades_to_bookmark(self, storage: StorageEngine) -> None:
        """Insert page + bookmark, delete the page, assert bookmark is gone."""
        slug = "task-341-cascade-page"
        pid = _insert_wiki_page(storage, slug)

        # Add a bookmark pointing at the page's slug
        storage.add_bookmark(slug, label_override="Task 341 target")
        assert storage.get_bookmark(slug) is not None, "bookmark should exist pre-delete"

        # Delete the page by id
        assert storage.delete_wiki_page(pid) is True

        # Cascade: the bookmark row must be gone, not orphaned
        assert storage.get_bookmark(slug) is None, (
            "wiki_bookmark row orphaned after wiki_delete — "
            "delete_wiki_page must cascade to wiki_bookmark"
        )

    def test_delete_wiki_page_does_not_touch_other_bookmarks(self, storage: StorageEngine) -> None:
        """Cascade must scope to the deleted page's slug, not wipe every bookmark."""
        target = "task-341-cascade-keep-target"
        bystander = "task-341-cascade-keep-bystander"

        target_pid = _insert_wiki_page(storage, target)
        _insert_wiki_page(storage, bystander)

        storage.add_bookmark(target, label_override="will be deleted")
        storage.add_bookmark(bystander, label_override="must survive")

        assert storage.delete_wiki_page(target_pid) is True

        assert storage.get_bookmark(target) is None, "bookmark of deleted page must be gone"
        assert storage.get_bookmark(bystander) is not None, (
            "cascade must not delete bookmarks of OTHER slugs"
        )
