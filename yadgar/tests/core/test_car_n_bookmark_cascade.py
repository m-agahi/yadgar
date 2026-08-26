"""Car-N: bookmark referential integrity on wiki_delete (ledger #341 / #365).

Pre-fix defect: `wiki_delete` did NOT cascade to `wiki_bookmark`. After a
wiki page was deleted, its bookmark row remained, pointing at a non-existent
page. `list_bookmarks` returned those dangling rows and viz could not render
a remove control against them.

Car-N closed the cascade at the CORE tool side; car A had already closed a
PARTIAL one at the storage layer (a raw ``DELETE FROM wiki_bookmark`` that
removed the row without compacting positions). The two cancelled out: the
storage DELETE runs FIRST (core ``wiki_delete`` → ``_forward_admin`` → backend
``wiki_delete`` → ``WikiStore.delete`` → ``delete_wiki_page``), so by the time
car-N's ``remove_bookmark`` ran at the core shell the row was already gone and
its ``get_bookmark(slug) is None -> return False`` guard short-circuited before
the compaction. Net: a mid-list delete left a HOLE in the dense 0..N-1 position
sequence, and the next ``add_bookmark`` (``position = count()``) minted a
DUPLICATE position.

Car P (this file's current shape) keeps ONE cascade, at the storage layer in
``delete_wiki_page`` — the layer every delete path funnels through, including
backend-initiated deletes the core-side hook never saw.

The pre-car-P tests could not see the corruption because neither exercised the
COMPOSED path: the ``_shared`` suite calls ``storage.delete_wiki_page`` directly,
and the cascade tests below stubbed ``_forward_admin``, so car A's DELETE never
ran. ``TestWikiDeleteCascade`` now drives ``wiki_delete`` end-to-end against the
real backend op (``admin_backend_bypass``), with no stubbed forwarder.

Coverage:
  1. End-to-end cascade + position compaction on `wiki_delete` (no stub)
  2. The dense 0..N-1 invariant survives, and the next `add_bookmark` does not
     collide with a surviving row
  3. No cascade on refusal or not-found envelopes (stubbed forwarder — those
     envelopes are what is under test, not the storage path)
  4. `remove_bookmark` idempotency + no-bookmark handling
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
# A. Storage layer — remove_bookmark + list_orphan_bookmarks
# ---------------------------------------------------------------------------
class TestRemoveBookmark:
    """The storage-layer cascade primitive.

    ``remove_bookmark`` deletes the row AND compacts positions in one
    operation — which is why ``delete_wiki_page`` calls it rather than
    issuing its own ``DELETE`` (car P). There is no ``_purge_for_slug``
    wrapper: an earlier revision of this docstring named one, and no such
    symbol has ever existed in the codebase.
    """

    def test_remove_bookmark_removes_existing_bookmark(self, storage: StorageEngine) -> None:
        """Returns True when a bookmark row was deleted."""
        storage.add_bookmark("p-s1")
        assert storage.remove_bookmark("p-s1") is True
        assert storage.get_bookmark("p-s1") is None

    def test_remove_bookmark_no_bookmark_returns_false(self, storage: StorageEngine) -> None:
        """Returns False when no row exists (no error)."""
        assert storage.remove_bookmark("never-bookmarked") is False

    def test_remove_bookmark_idempotent(self, storage: StorageEngine) -> None:
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
    """``wiki_delete`` end-to-end: the storage cascade must reach the bookmark.

    NO STUBBED FORWARDER on the success-path tests. ``admin_backend_bypass``
    dispatches ``_forward_admin`` straight into the real backend op, so the
    call travels the production chain — core ``wiki_delete`` → backend
    ``wiki_delete`` → ``WikiStore.delete`` → ``storage.delete_wiki_page`` →
    ``remove_bookmark``. Stubbing the forwarder is exactly what let the
    pre-car-P suite pass over the position corruption: the stub skipped
    ``delete_wiki_page``, so only the (then core-side) cascade ran and the
    partial storage DELETE that shadowed it was never executed.

    The position assertions read the WHOLE table rather than a filtered
    subset. The module-scoped engine is shared with the other classes here,
    so a subset assertion would be hostage to test ordering; the dense
    ``0..N-1`` sequence is the actual invariant ``add_bookmark``
    (``position = count()``) and ``reorder_bookmark`` depend on, and it is
    what the bug broke.
    """

    @staticmethod
    def _positions(storage: StorageEngine) -> list[int]:
        """``list_bookmarks`` orders by position ASC — so a dense table reads
        as ``range(n)`` and any hole or duplicate shows up as a deviation."""
        return [int(r["position"]) for r in storage.list_bookmarks()]

    def test_cascade_no_bookmark_is_noop(self, storage: StorageEngine) -> None:
        """Page with no bookmark → delete succeeds, table untouched."""
        from yadgar.core.server.tools import wiki as wtool

        _seed_pages(storage, ["c-clean"])
        before = self._positions(storage)
        result = wtool.wiki_delete("c-clean")
        assert result.get("deleted") is True
        assert self._positions(storage) == before

    def test_cascade_removes_bookmark_end_to_end(self, storage: StorageEngine) -> None:
        """Bookmarked page → delete cascade removes the row, table stays dense."""
        from yadgar.core.server.tools import wiki as wtool

        _seed_pages(storage, ["c-only"])
        storage.add_bookmark("c-only")
        assert any(r["slug"] == "c-only" for r in storage.list_bookmarks())
        result = wtool.wiki_delete("c-only")
        assert result.get("deleted") is True
        # Cascade must have removed the bookmark row.
        assert storage.get_bookmark("c-only") is None
        assert all(r["slug"] != "c-only" for r in storage.list_bookmarks())
        positions = self._positions(storage)
        assert positions == list(range(len(positions))), (
            f"bookmark positions went sparse after cascade: {positions}"
        )

    def test_cascade_compacts_positions_when_mid_list(self, storage: StorageEngine) -> None:
        """The corruption this car fixes, driven through the composed path.

        Four bookmarks; delete the page behind the middle one. Pre-car-P the
        storage DELETE removed the row without compacting and the core-side
        ``remove_bookmark`` short-circuited on the already-gone row, so the
        table went sparse (…, k-1, k+1, …) and the NEXT ``add_bookmark``
        computed ``position = count()`` — a value a surviving row already
        held. Two rows then tie on ``position`` and
        ``list_bookmarks ORDER BY position`` is non-deterministic between
        them.
        """
        from yadgar.core.server.tools import wiki as wtool

        slugs = ["c-a", "c-b", "c-mid", "c-d"]
        _seed_pages(storage, slugs)
        for slug in slugs:
            storage.add_bookmark(slug)

        before = self._positions(storage)
        assert before == list(range(len(before))), (
            f"pre-condition violated — table was already sparse: {before}"
        )

        result = wtool.wiki_delete("c-mid")
        assert result.get("deleted") is True

        after_rows = storage.list_bookmarks()
        assert all(r["slug"] != "c-mid" for r in after_rows), "cascade did not remove the row"
        assert len(after_rows) == len(before) - 1
        after = [int(r["position"]) for r in after_rows]
        assert after == list(range(len(after))), (
            f"positions left sparse after a mid-list cascade: {after} "
            f"(expected dense 0..{len(after) - 1})"
        )

        # The consequence assertion: the next add_bookmark must land at the
        # tail, not collide with a surviving row.
        _seed_pages(storage, ["c-next"])
        added = storage.add_bookmark("c-next")
        assert int(added["position"]) == len(after), (
            f"add_bookmark minted position {added['position']} on a "
            f"{len(after)}-row table — duplicate position"
        )
        final = self._positions(storage)
        assert final == list(range(len(final))), f"table not dense after re-add: {final}"
        assert len(set(final)) == len(final), f"duplicate positions present: {final}"

    def test_cascade_does_not_run_on_refusal(self, storage: StorageEngine, monkeypatch) -> None:
        """Refused envelope → wiki_delete returns envelope, no bookmark touched.

        STUBBED FORWARDER on purpose: the envelope is what is under test, and
        a refusal means the backend op never reached ``delete_wiki_page`` at
        all. The bookmark assertion is the standing guard that no future car
        re-adds a cascade at the core shell ABOVE the envelope branch, where
        it would fire on a write the backend refused.
        """
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
        """Not-found envelope → no error, no cascade, no bookmark churn.

        STUBBED FORWARDER for the same reason as the refusal case above: the
        not-found envelope is the subject, and the bookmark assertion pins
        that an unrelated slug's row is never collateral.
        """
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
