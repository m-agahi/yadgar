"""Car B (#83): explicit-slug + upsert semantics on the wiki write path.

Root bug (probed live v5.160.0): ``WikiStore.add`` derives the stored slug from
the TITLE (``slug = self._slugify(title)``).  Structural pages (repo_wiki) must
land at a CALLER-SUPPLIED slug — otherwise crossrefs / stale-diff key on the
wrong slug and every cadence churns.

This suite pins the new contract:
  - ``WikiAddOptions.slug`` provided → page stored at EXACTLY that slug (no
    title derivation).
  - ``WikiAddOptions.upsert=True`` → create-or-overwrite at that slug (revision
    if present, create if absent).
  - ``upsert=False`` + slug already present → rejected (not overwritten).
  - slug absent → unchanged (title-derived), byte-for-byte backward compat.
"""

from __future__ import annotations

import pytest

from yadgar._shared.wiki.contract import WikiAddOptions
from yadgar.core import server

# C13 (0047 PR#40 §5): seeds must NAME the project they write into —
# C5 deleted every fallback that used to answer an unnamed write (ADR-0227).
# A per-file constant, deliberately NOT a shared fixture default: a new test
# that builds its own write payload still reds — the signal of the flip.
_PROJECT = "m-agahi/yadgar"

_REPO_DIR = "/home/max/git/yadgar"


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("wiki_upsert_slug")
    server.init_engines(
        db_path=str(tmp_path / "wiki_upsert_test.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


def _wiki():
    return server._wiki


def _storage():
    return server._wiki._storage


# ── A. explicit slug bypasses title derivation ───────────────────────────────


class TestExplicitSlug:
    def test_slug_provided_stores_at_that_slug_not_title(self):
        """slug='proj-mod-foo' + title 'Totally Different' → stored at proj-mod-foo."""
        _wiki().add(
            "Totally Different Title Alpha",
            "## Purpose\nA module.\n## Exports\nfoo\n## Design\nX.",
            category="reference",
            opts=WikiAddOptions(
                project_id=_PROJECT,
                slug="proj-mod-foo",
                upsert=True,
                directory_context=_REPO_DIR,
            ),
        )
        # Landed at the caller slug.
        page = _storage().get_wiki_page_by_slug("proj-mod-foo")
        assert page is not None, "page not stored at caller-supplied slug"
        assert page.get("slug") == "proj-mod-foo"
        # NOT at the title-derived slug.
        title_page = _storage().get_wiki_page_by_slug("totally-different-title-alpha")
        assert title_page is None, "page leaked to the title-derived slug (the bug)"

    def test_returned_dict_carries_caller_slug(self):
        """add() return dict reflects the caller slug (no round-trip needed)."""
        result = _wiki().add(
            "Whatever Title Beta",
            "## Purpose\nB.\n## Exports\nbar\n## Design\nY.",
            category="reference",
            opts=WikiAddOptions(
                project_id=_PROJECT,
                slug="proj-mod-bar",
                upsert=True,
                directory_context=_REPO_DIR,
            ),
        )
        assert result.get("slug") == "proj-mod-bar"


# ── B. upsert overwrite semantics ────────────────────────────────────────────


class TestUpsertOverwrite:
    def test_upsert_second_write_overwrites_same_slug(self):
        """Two writes, same explicit slug, upsert=True → one page, content updated."""
        _wiki().add(
            "Gen One",
            "## Purpose\nversion one.\n## Exports\ng\n## Design\nA.",
            category="reference",
            opts=WikiAddOptions(
                project_id=_PROJECT, slug="proj-mod-gen", upsert=True, directory_context=_REPO_DIR
            ),
        )
        _wiki().add(
            "Gen Two",
            "## Purpose\nversion two edited.\n## Exports\ng\n## Design\nB.",
            category="reference",
            opts=WikiAddOptions(
                project_id=_PROJECT, slug="proj-mod-gen", upsert=True, directory_context=_REPO_DIR
            ),
        )
        page = _storage().get_wiki_page_by_slug("proj-mod-gen")
        assert page is not None
        assert "version two edited" in page.get("content", "")
        # Still exactly one page at that slug (upsert, not create).
        assert page.get("slug") == "proj-mod-gen"

    def test_upsert_absent_creates(self):
        """upsert=True at an absent slug simply creates the page."""
        result = _wiki().add(
            "Fresh Create",
            "## Purpose\nfresh.\n## Exports\nf\n## Design\nC.",
            category="reference",
            opts=WikiAddOptions(
                project_id=_PROJECT, slug="proj-mod-fresh", upsert=True, directory_context=_REPO_DIR
            ),
        )
        assert result.get("slug") == "proj-mod-fresh"
        assert _storage().get_wiki_page_by_slug("proj-mod-fresh") is not None


# ── C. upsert=False collision rejection ──────────────────────────────────────


class TestUpsertFalseRejects:
    def test_upsert_false_existing_slug_rejected(self):
        """upsert=False + explicit slug that already exists → rejection, no overwrite."""
        _wiki().add(
            "Collide First",
            "## Purpose\noriginal.\n## Exports\nc\n## Design\nD.",
            category="reference",
            opts=WikiAddOptions(
                project_id=_PROJECT,
                slug="proj-mod-collide",
                upsert=True,
                directory_context=_REPO_DIR,
            ),
        )
        result = _wiki().add(
            "Collide Second",
            "## Purpose\nSHOULD NOT LAND.\n## Exports\nc\n## Design\nE.",
            category="reference",
            opts=WikiAddOptions(
                project_id=_PROJECT,
                slug="proj-mod-collide",
                upsert=False,
                directory_context=_REPO_DIR,
            ),
        )
        assert result.get("stored") is False, "upsert=False collision should be rejected"
        assert result.get("reason") == "slug_exists"
        # Original content preserved.
        page = _storage().get_wiki_page_by_slug("proj-mod-collide")
        assert "original" in page.get("content", "")
        assert "SHOULD NOT LAND" not in page.get("content", "")


# ── D. backward compat — slug absent → title-derived ─────────────────────────


class TestTitleDerivedBackwardCompat:
    def test_no_slug_uses_title_slug(self):
        """No slug in opts → unchanged title-derived behavior."""
        _wiki().add(
            "Legacy Title Page",
            "plain content",
            category="reference",
            opts=WikiAddOptions(project_id=_PROJECT),
        )
        assert _storage().get_wiki_page_by_slug("legacy-title-page") is not None
