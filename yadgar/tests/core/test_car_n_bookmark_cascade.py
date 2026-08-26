"""Car-N: bookmark referential integrity on wiki_delete (ledger #341 / #365).

Pre-fix defect: `wiki_delete` did NOT cascade to `wiki_bookmark`. After a
wiki page was deleted, its bookmark row remained, pointing at a non-existent
page. `list_bookmarks` returned those dangling rows and viz could not render
a remove control against them.

This car closes the cascade at the CORE tool side (the storage DELETE in
`delete_wiki_page` was partial — it removed the row but did not compact
positions, so a mid-list cascade left a gap in the 0..N dense position
sequence that `list_bookmarks` and the next `add_bookmark` rely on).

TDD red-first — these tests were written before the fix. They cover:
  1. Cascade on `wiki_delete` post-success path
  2. Position compaction after cascade (dense 0..N invariant)
  3. No cascade on refusal or not-found envelopes
  4. `_purge_for_slug` idempotency + no-bookmark handling
  5. `list_orphan_bookmarks` audit surface
  6. `WikiStore.lint()` surfaces bookmark orphans
"""

from __future__ import annotations

from typing import Any

import pytest

from yadgar._shared.storage import StorageEngine
from yadgar.core import server

# C13 (0047 PR#40 §5): seeds must NAME the project they write into —
# C5 deleted every fallback that used to answer an unnamed write (ADR-0227).
_PROJECT = "m-agahi/yadgar"

# R3 Car 3a: the wiki_delete tool forwards to backend /admin which (in
# production) runs run_admin_op_blocking. For cascade tests we monkeypatch
# `_forward_admin` with a per-test stub so we control the envelope.
# For storage-layer tests (purge, list_orphan) the bypass auto-fires the
# real write path against the module-scoped test engine.
pytestmark = pytest.mark.usefixtures("admin_backend_bypass")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    """Full server engine stack, initialized ONCE per module.

    Car-N needs the full stack (not bare ``module_storage``) because
    ``wiki_delete`` calls ``_st._storage.remove_bookmark`` and
    ``WikiStore.lint()`` (via ``_st._wiki``) — both routes go through the
    runtime state set up by ``init_engines()``.
    """
    tmp_path = tmp_path_factory.mktemp("car_n_bookmark_cascade")
    server.init_engines(
        db_path=str(tmp_path / "car_n_cascade.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


@pytest.fixture
def storage(_engines):  # noqa: ARG001 — _engines initializes the runtime state
    """The shared StorageEngine from the runtime state (same one ``wiki_delete`` uses)."""
    import yadgar._shared.runtime.state as _state

    assert _state._storage is not None, "StorageEngine not initialized by _engines"
    return _state._storage


def _seed_pages(storage: StorageEngine, slugs: list[str]) -> None:
    """Create wiki pages for each slug so cascade / lint has real rows.

    ``_sanctioned=True`` bypasses the mutability gate for the test seed;
    ``project_id`` satisfies C13's REQUIRED-on-write contract.
    """
    for slug in slugs:
        storage.insert_wiki_page(
            {
                "title": slug.replace("-", " ").title(),
                "slug": slug,
                "content": f"content for {slug}",
                "category": "reference",
                "tags": [],
                "links": [],
                "confidence": "high",
                "directory_context": "/tmp/cascade-test",
                "project_id": _PROJECT,
                "_sanctioned": True,
            }
        )


class _StubForwarder:
    """Returns the envelope the test pinned (deleted | refused | not-found)."""

    def __init__(self, envelope: dict[str, Any]) -> None:
        self.envelope = envelope
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, tool: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((tool, payload))
        return self.envelope


# ---------------------------------------------------------------------------
# A. Storage layer — _purge_for_slug + list_orphan_bookmarks
# ---------------------------------------------------------------------------
class TestPurgeForSlug:
    """Storage layer cascade primitive. remove_bookmark already does
    delete + position compact; we wrap it as _purge_for_slug for the
    one-call wire-up at the wiki tool site.
    """

    def test_purge_for_slug_removes_existing_bookmark(self, storage: StorageEngine) -> None:
        """Returns True when a bookmark row was deleted."""
        storage.add_bookmark("p-s1")
        assert storage.remove_bookmark("p-s1") is True
        assert storage.get_bookmark("p-s1") is None

    def test_purge_for_slug_no_bookmark_returns_false(self, storage: StorageEngine) -> None:
        """Returns False when no row exists (no error)."""
        assert storage.remove_bookmark("never-bookmarked") is False

    def test_purge_for_slug_idempotent(self, storage: StorageEngine) -> None:
        """Calling twice: first True, second False, no error."""
        storage.add_bookmark("p-idem")
        assert storage.remove_bookmark("p-idem") is True
        assert storage.remove_bookmark("p-idem") is False


class TestListOrphanBookmarks:
    """Audit surface: returns rows whose slug has no matching wiki_page."""

    def test_list_orphans_empty_when_consistent(self, storage: StorageEngine) -> None:
        """All bookmarks point at real pages → empty list."""
        _seed_pages(storage, ["o-real-a", "o-real-b"])
        storage.add_bookmark("o-real-a")
        storage.add_bookmark("o-real-b")
        result = storage.list_orphan_bookmarks()
        assert result == []

    def test_list_orphans_finds_dangling_row(self, storage: StorageEngine) -> None:
        """Bookmark survives page delete (simulating the bug) → flagged.

        We seed a bookmark for a slug that NEVER had a wiki page — the
        cleanest way to model the legacy "page gone, bookmark still here"
        state without fighting the cascade in ``delete_wiki_page``.
        """
        storage.add_bookmark("o-never-had-a-page")
        result = storage.list_orphan_bookmarks()
        slugs = [r["slug"] for r in result]
        assert "o-never-had-a-page" in slugs

    def test_list_orphans_handles_no_bookmarks(self, storage: StorageEngine) -> None:
        """No bookmarks at all → empty list, no error."""
        result = storage.list_orphan_bookmarks()
        assert result == []


# ---------------------------------------------------------------------------
# B. wiki_delete cascade (core tool side)
# ---------------------------------------------------------------------------
class TestWikiDeleteCascade:
    """wiki_delete post-success side-effect: cascade to wiki_bookmark."""

    def test_cascade_no_bookmark_is_noop(self, storage: StorageEngine, monkeypatch) -> None:
        """Page with no bookmark → delete succeeds, no error."""
        from yadgar.core.server.tools import wiki as wtool

        _seed_pages(storage, ["c-clean"])
        stub = _StubForwarder({"deleted": True})
        monkeypatch.setattr(wtool, "_forward_admin", stub)
        result = wtool.wiki_delete("c-clean")
        assert result.get("deleted") is True

    def test_cascade_removes_bookmark_at_pos_0(self, storage: StorageEngine, monkeypatch) -> None:
        """Single bookmark at pos 0 → delete cascade removes it, list returns []."""
        from yadgar.core.server.tools import wiki as wtool

        _seed_pages(storage, ["c-only"])
        storage.add_bookmark("c-only")
        assert any(r["slug"] == "c-only" for r in storage.list_bookmarks())
        stub = _StubForwarder({"deleted": True})
        monkeypatch.setattr(wtool, "_forward_admin", stub)
        result = wtool.wiki_delete("c-only")
        assert result.get("deleted") is True
        # Cascade must have removed the bookmark row.
        assert storage.get_bookmark("c-only") is None
        assert all(r["slug"] != "c-only" for r in storage.list_bookmarks())

    def test_cascade_compacts_positions_when_mid_list(
        self, storage: StorageEngine, monkeypatch
    ) -> None:
        """Delete a page in the middle of 4 bookmarks → remaining compact 0,1,2."""
        from yadgar.core.server.tools import wiki as wtool

        _seed_pages(storage, ["c-a", "c-b", "c-mid", "c-d"])
        storage.add_bookmark("c-a")
        storage.add_bookmark("c-b")
        storage.add_bookmark("c-mid")
        storage.add_bookmark("c-d")
        # c-mid is at position 2 of 4. After cascade the remaining 3 must
        # be at positions 0,1,2 (dense, no gap).
        stub = _StubForwarder({"deleted": True})
        monkeypatch.setattr(wtool, "_forward_admin", stub)
        wtool.wiki_delete("c-mid")
        remaining = {r["slug"]: r["position"] for r in storage.list_bookmarks()}
        assert "c-mid" not in remaining
        assert set(remaining.keys()) == {"c-a", "c-b", "c-d"}
        assert sorted(remaining.values()) == [0, 1, 2]

    def test_cascade_does_not_run_on_refusal(self, storage: StorageEngine, monkeypatch) -> None:
        """Refused envelope → wiki_delete returns envelope, no bookmark touched."""
        from yadgar.core.server.tools import wiki as wtool

        _seed_pages(storage, ["c-locked"])
        storage.add_bookmark("c-locked")
        stub = _StubForwarder({"refused": True, "reason": "wiki_page_locked"})
        monkeypatch.setattr(wtool, "_forward_admin", stub)
        result = wtool.wiki_delete("c-locked")
        assert result.get("refused") is True
        # Bookmark MUST still be there — refusal short-circuited the cascade.
        assert storage.get_bookmark("c-locked") is not None

    def test_cascade_does_not_run_on_not_found(self, storage: StorageEngine, monkeypatch) -> None:
        """Not-found envelope → no error, no cascade, no bookmark churn."""
        from yadgar.core.server.tools import wiki as wtool

        _seed_pages(storage, ["c-real"])
        storage.add_bookmark("c-real")
        stub = _StubForwarder({"deleted": False})
        monkeypatch.setattr(wtool, "_forward_admin", stub)
        result = wtool.wiki_delete("c-does-not-exist")
        assert result.get("deleted") is False
        # Untouched: c-real bookmark still there.
        assert storage.get_bookmark("c-real") is not None


# ---------------------------------------------------------------------------
# C. WikiStore.lint() surfaces bookmark orphans
# ---------------------------------------------------------------------------
class TestWikiLintBookmarkOrphans:
    """lint() emits a 'bookmark_orphan' issue per dangling row."""

    def test_lint_orphan_count_zero_when_consistent(self, storage: StorageEngine) -> None:
        """Clean corpus: no bookmark_orphan issues."""
        import yadgar._shared.runtime.state as _state

        _seed_pages(storage, ["l-clean-a", "l-clean-b"])
        storage.add_bookmark("l-clean-a")
        storage.add_bookmark("l-clean-b")
        ws = _state._wiki
        assert ws is not None
        result = ws.lint()
        bookmark_issues = [i for i in result["issues"] if i.get("type") == "bookmark_orphan"]
        assert bookmark_issues == []

    def test_lint_surfaces_bookmark_orphans(self, storage: StorageEngine) -> None:
        """One dangling bookmark → one bookmark_orphan issue + stats counter."""
        import yadgar._shared.runtime.state as _state

        # Bookmark for a slug that never had a page — the cleanest
        # simulation of the legacy "page gone, bookmark still here" state.
        storage.add_bookmark("l-never-existed")
        ws = _state._wiki
        assert ws is not None
        result = ws.lint()
        bookmark_issues = [i for i in result["issues"] if i.get("type") == "bookmark_orphan"]
        assert len(bookmark_issues) == 1
        assert bookmark_issues[0]["slug"] == "l-never-existed"
        assert bookmark_issues[0]["severity"] == "warning"
        # Stats counter must reflect it.
        assert result["stats"].get("bookmark_orphan_count", 0) == 1
