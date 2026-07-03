"""RED tests for v5.42.6 — §25 wiki_read resolution hole (Bug 2).

TDD: written BEFORE implementation. These tests start RED and go GREEN once
the wiki_read branch_hint parameter is added.

The root cause: wiki_read calls _detect_branch(os.getcwd()) daemon-side
(returns None in container). With _current_branch=None:
- Step 1 is skipped (guarded by `if current_branch is not None`).
- Step 2 matches branch IS NONE only — misses rows with branch="master".
- Step 3 matches directory='global' only.
So any post-v5.42.3 write (branch="master") is unreachable via wiki_read
when running in a container.

Fix: add branch_hint: str | None = None to wiki_read (symmetric with
wiki_add and _resolve_page_id_by_slug).

Coverage:
T5. wiki_read(slug, directory=..., branch_hint="master") returns the page
    when the row has branch="master" and daemon-side detect returns None.
T6. wiki_read(slug, directory=...) without branch_hint still finds a
    branch=None (canonical-slot) page via step 2 fallback.
T7. wiki_read(slug, directory=..., branch_hint="feature-x") returns NOT
    FOUND when only branch="master" row exists (branch isolation enforced).
T8. wiki_read(slug) without directory still works (legacy branch-only path,
    no regression).
"""

from __future__ import annotations

import re
from unittest.mock import patch

import pytest

from yadgar import server

# ── fixture ────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("v5_42_6_resolution_hole")
    server.init_engines(
        db_path=str(tmp_path / "test_resolution.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


def _storage():
    import yadgar.server._state as _st

    return _st._storage


def _slugify(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:64]


def _insert_wiki_page(
    title: str,
    directory_context: str,
    branch: str | None = None,
    slug: str | None = None,
) -> str:
    """Insert a wiki page directly to storage with given directory and branch."""
    st = _storage()
    slug = slug or _slugify(title)
    st.insert_wiki_page(
        {
            "slug": slug,
            "title": title,
            "content": f"Content of {title}",
            "category": "reference",
            "tags": ["test", "v5-42-6"],
            "links": [],
            "source_memory_ids": [],
            "confidence": "medium",
            "directory_context": directory_context,
        },
        branch=branch,
    )
    return slug


# ── T5: wiki_read with branch_hint finds post-v5.42.3 writes ──────────────────


class TestWikiReadBranchHint:
    """T5 — wiki_read(branch_hint=...) finds branch-scoped rows."""

    def test_branch_hint_finds_master_row_when_detect_returns_none(self):
        """wiki_read(directory=..., branch_hint="master") finds branch="master" row.

        Simulates the container scenario: daemon _detect_branch returns None,
        so without branch_hint the row is unreachable via step 1.
        With branch_hint="master", step 1 uses the hint and finds the row.
        """
        from yadgar.server.tools.wiki import wiki_read

        slug = _insert_wiki_page(
            "Branch Hint Test Page",
            directory_context="/home/max/git/yadgar",
            branch="master",
        )

        # Simulate container: _detect_branch always returns None
        with patch("yadgar.server._detect_branch", return_value=None):
            # Without branch_hint: should NOT find the page (step 2 needs branch IS NULL)
            # RED: wiki_read currently lacks branch_hint param — this verifies the hole
            result_no_hint = wiki_read(slug, directory="/home/max/git/yadgar")
            assert "error" in result_no_hint, (
                "Without branch_hint and _detect_branch=None, branch='master' row should not "
                "be found via steps 2+3 (which only match branch IS NULL)"
            )

            # With branch_hint: MUST find the page (uses hint in step 1)
            # RED: wiki_read(branch_hint=...) call will fail until branch_hint param is added
            result_with_hint = wiki_read(
                slug, directory="/home/max/git/yadgar", branch_hint="master"
            )
            assert "error" not in result_with_hint, (
                f"wiki_read with branch_hint='master' should find the page; got: {result_with_hint}"
            )
            assert result_with_hint.get("slug") == slug

    def test_branch_hint_propagates_to_storage(self):
        """branch_hint passed to wiki_read reaches the storage resolution call."""
        from yadgar.server.tools.wiki import wiki_read

        slug = _insert_wiki_page(
            "Branch Propagation Test",
            directory_context="/home/max/git/yadgar",
            branch="master",
        )

        # Spy on read_by_directory_branch to verify branch_hint is used
        import yadgar.server._state as _st

        wiki_store = _st._wiki
        calls = []
        original = wiki_store.read_by_directory_branch

        def _spy(s, caller_directory, current_branch):
            calls.append({"slug": s, "directory": caller_directory, "branch": current_branch})
            return original(s, caller_directory, current_branch)

        wiki_store.read_by_directory_branch = _spy

        try:
            with patch("yadgar.server._detect_branch", return_value=None):
                wiki_read(slug, directory="/home/max/git/yadgar", branch_hint="master")
        finally:
            wiki_store.read_by_directory_branch = original

        assert len(calls) >= 1
        # The branch passed to storage should be "master" (from branch_hint)
        assert calls[0].get("branch") == "master", (
            f"Expected branch='master' from branch_hint; storage saw branch={calls[0].get('branch')!r}"
        )


# ── T6: wiki_read without branch_hint still finds canonical-slot page ─────────


class TestWikiReadCanonicalSlotFallback:
    """T6 — wiki_read without branch_hint finds branch=NULL pages via step 2."""

    def test_no_branch_hint_finds_null_branch_page(self):
        """wiki_read(directory=...) without branch_hint finds canonical-slot page."""
        from yadgar.server.tools.wiki import wiki_read

        # Insert a page with branch=None (canonical slot)
        slug = _insert_wiki_page(
            "Canonical Slot Page",
            directory_context="/home/max/git/yadgar",
            branch=None,
        )

        with patch("yadgar.server._detect_branch", return_value=None):
            result = wiki_read(slug, directory="/home/max/git/yadgar")

        assert "error" not in result, (
            f"wiki_read without branch_hint should find canonical-slot (branch=NULL) page; got: {result}"
        )
        assert result.get("slug") == slug


# ── T7: wrong branch_hint returns NOT FOUND ───────────────────────────────────


class TestWikiReadBranchIsolation:
    """T7 — branch isolation: wrong branch_hint does not return other-branch page."""

    def test_wrong_branch_hint_returns_not_found(self):
        """wiki_read(branch_hint="feature-x") returns NOT FOUND when only master row exists."""
        from yadgar.server.tools.wiki import wiki_read

        slug = _insert_wiki_page(
            "Branch Isolation Test",
            directory_context="/home/max/git/yadgar",
            branch="master",
        )

        with patch("yadgar.server._detect_branch", return_value=None):
            result = wiki_read(slug, directory="/home/max/git/yadgar", branch_hint="feature-x")

        assert "error" in result, (
            "branch_hint='feature-x' should NOT find a branch='master' row (isolation)"
        )


# ── T8: legacy no-directory path is not broken ────────────────────────────────


class TestWikiReadLegacyPath:
    """T8 — wiki_read(slug) without directory uses legacy branch-only resolution."""

    def test_no_directory_uses_legacy_resolution(self):
        """wiki_read(slug) without directory still returns the page (no regression)."""
        from yadgar.server.tools.wiki import wiki_read

        slug = _insert_wiki_page(
            "Legacy Path Test",
            directory_context="global",
            branch=None,
        )

        # Call without directory — should still work (legacy path)
        result = wiki_read(slug)

        assert "error" not in result, (
            f"wiki_read(slug) without directory should use legacy resolution; got: {result}"
        )
        assert result.get("slug") == slug
