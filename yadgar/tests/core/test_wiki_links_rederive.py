"""Car W3 — ledger tasks 216 + 218: ``wiki_page.links`` derivation defects.

Two defects on the same derivation path:

  task 218 — ``_extract_wikilinks`` ran every ``[[...]]`` body through
    ``slugify``, which collapses ``_`` to ``-``.  Post-ADR-0211 canonical
    slugs are ``{project with / -> _}_{name}``, so EVERY canonical ADR/task
    slug came out corrupted (``m-agahi_yadgar_adr-0253`` →
    ``m-agahi-yadgar-adr-0253``).  Both ``wiki_page.links`` AND the
    ``wiki_crossref`` table were fed the same corrupted list, so backlink
    lookups (``get_wiki_backlinks``, exact ``to_slug`` match) never resolved.

  task 216 — the surgical edit primitives rewrote page content without
    re-deriving ``links``.  ``_apply_text_edit`` wrote ``{"content": ...}``
    only (no crossref sync at all); ``append_section`` synced crossrefs but
    left ``links`` stale.  A link ADDED or REMOVED by a surgical edit was
    invisible in the denormalised array.

Test partition is deliberate so the mutation checks stay sensitive:
  * ``TestWikilinkUnderscorePreservation`` is a pure unit test — reds for 218
    only.
  * ``TestSurgicalEditRederivesLinks`` uses HYPHEN-ONLY slugs — reds for 216
    only.
  * ``TestCanonicalSlugThroughSurgicalEdit`` is the end-to-end check and may
    red for either.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from yadgar._shared.storage.migrations import _migration_013_wiki_page_version
from yadgar.core import server
from yadgar.tests.core.conftest import TEST_PROJECT_ID

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_script():
    """Load ``scripts/rederive_wiki_links.py`` by path.

    ``scripts/`` is not a package, so a ``from scripts.x import y`` would make
    mypy see the file under two module names and abandon the run. Same
    file-location loader the other ``scripts/`` tests use.
    """
    path = _REPO_ROOT / "scripts" / "rederive_wiki_links.py"
    spec = importlib.util.spec_from_file_location("rederive_wiki_links", path)
    assert spec and spec.loader, f"Cannot load {path}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# R3 Car 3c: the wiki-edit primitives forward their DB write to the backend
# /admin endpoint.  Route _forward_admin → run_admin_op directly (no HTTP).
pytestmark = pytest.mark.usefixtures("admin_backend_bypass")

#: A REAL canonical slug shape (ADR-0211): ``owner_repo_kind-id`` with ``/``
#: replaced by ``_``.  Not a synthetic ``a_b`` — the underscores are what the
#: slugifier ate.
CANONICAL_SLUG = "m-agahi_yadgar_adr-0253"
CANONICAL_SLUG_2 = "m-agahi_yadgar_task-218"


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("wiki_links_rederive")
    server.init_engines(
        db_path=str(tmp_path / "wiki_links_rederive.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    _migration_013_wiki_page_version(_storage())
    yield
    server.shutdown()


def _storage():
    return server._get_storage()


def _wiki():
    return server._wiki


def _insert_page(slug: str, content: str, links: list[str] | None = None) -> int:
    return _storage().insert_wiki_page(
        {
            "slug": slug,
            "title": slug,
            "content": content,
            "category": "reference",
            "tags": [],
            "confidence": "medium",
            "source_memory_ids": [],
            "links": links if links is not None else [],
            "project_id": TEST_PROJECT_ID,
            "directory_context": "global",
        }
    )


def _crossrefs(from_slug: str) -> list[str]:
    """Read the ``wiki_crossref`` rows FROM *from_slug* (the denormalised
    array's table-side counterpart)."""
    rows = _storage()._q(
        "SELECT to_slug FROM wiki_crossref WHERE from_slug = $s",
        {"s": from_slug},
    )
    return sorted(r["to_slug"] for r in rows if "to_slug" in r)


def _links(page_id: int) -> list[str]:
    page = _storage().get_wiki_page(page_id)
    assert page is not None
    return sorted(page.get("links") or [])


# ── task 218: underscores survive wikilink extraction ────────────────────────


class TestWikilinkUnderscorePreservation:
    """A ``[[slug]]`` whose body is already slug-shaped is passed through
    verbatim — underscores included."""

    def test_canonical_slug_underscores_preserved(self):
        links = _wiki()._extract_wikilinks(f"See [[{CANONICAL_SLUG}]] for details.")
        assert links == [CANONICAL_SLUG]

    def test_canonical_slug_not_hyphenated(self):
        links = _wiki()._extract_wikilinks(f"[[{CANONICAL_SLUG}]]")
        assert "m-agahi-yadgar-adr-0253" not in links

    def test_long_canonical_slug_not_truncated_at_64(self):
        """Slug-shaped passthrough also escapes ``slugify``'s 64-char cap —
        ADR-0211 permits 256."""
        long_slug = "some-very-long-owner-name_some-very-long-repository-name_adr-0253"
        assert len(long_slug) > 64
        assert _wiki()._extract_wikilinks(f"[[{long_slug}]]") == [long_slug]

    def test_prose_title_still_slugified(self):
        """Non-slug-shaped bracket bodies keep the legacy slugify behaviour."""
        assert _wiki()._extract_wikilinks("See [[My Cool Page]].") == ["my-cool-page"]


# ── task 216: surgical edits re-derive links + crossrefs ─────────────────────


class TestSurgicalEditRederivesLinks:
    """HYPHEN-ONLY slugs on purpose: this class must be blind to defect 218 so
    the mutation check for 216 is meaningful."""

    def test_replace_text_adds_and_removes_link(self):
        pid = _insert_page(
            "rederive-replace",
            "Body links to [[target-a]] here.",
            links=["target-a"],
        )
        _wiki()._sync_crossrefs("rederive-replace", ["target-a"])

        result = _wiki().replace_text(pid, "[[target-a]]", "[[target-b]]")
        assert result.get("ok") is True

        # ADDED link is visible.
        assert _links(pid) == ["target-b"]
        # REMOVED link is gone — the direction that silently rots.
        assert "target-a" not in _links(pid)
        # Table and array AGREE, and agree on the RIGHT value.
        assert _crossrefs("rederive-replace") == ["target-b"]
        assert _crossrefs("rederive-replace") == _links(pid)

    def test_insert_after_adds_link(self):
        pid = _insert_page("rederive-insert-after", "anchor text here")
        result = _wiki().insert_after(pid, "anchor text", " [[added-page]]")
        assert result.get("ok") is True
        assert _links(pid) == ["added-page"]
        assert _crossrefs("rederive-insert-after") == ["added-page"]

    def test_insert_before_adds_link(self):
        pid = _insert_page("rederive-insert-before", "anchor text here")
        result = _wiki().insert_before(pid, "anchor text", "[[before-page]] ")
        assert result.get("ok") is True
        assert _links(pid) == ["before-page"]
        assert _crossrefs("rederive-insert-before") == ["before-page"]

    def test_delete_text_removes_link(self):
        pid = _insert_page(
            "rederive-delete",
            "keep this [[doomed-page]] tail",
            links=["doomed-page"],
        )
        _wiki()._sync_crossrefs("rederive-delete", ["doomed-page"])
        result = _wiki().delete_text(pid, "[[doomed-page]] ")
        assert result.get("ok") is True
        assert _links(pid) == []
        assert _crossrefs("rederive-delete") == []

    def test_replace_at_rederives(self):
        pid = _insert_page("rederive-replace-at", "line one has [[old-target]] in it")
        result = _wiki().replace_at(
            pid,
            line=1,
            col=14,
            length=len("[[old-target]]"),
            new_text="[[new-target]]",
            anchor_hint="[[old-target]] in it",
        )
        assert result.get("ok") is True, result
        assert _links(pid) == ["new-target"]
        assert _crossrefs("rederive-replace-at") == ["new-target"]

    def test_delete_at_rederives(self):
        pid = _insert_page(
            "rederive-delete-at",
            "line one has [[gone-target]] in it",
            links=["gone-target"],
        )
        _wiki()._sync_crossrefs("rederive-delete-at", ["gone-target"])
        result = _wiki().delete_at(
            pid,
            line=1,
            col=14,
            length=len("[[gone-target]]"),
            anchor_hint="[[gone-target]] in it",
        )
        assert result.get("ok") is True, result
        assert _links(pid) == []
        assert _crossrefs("rederive-delete-at") == []

    def test_insert_at_rederives(self):
        pid = _insert_page("rederive-insert-at", "a line long enough to anchor on")
        result = _wiki().insert_at(
            pid,
            line=1,
            col=32,
            new_text=" [[appended-target]]",
            anchor_hint="a line long enough to anchor on",
        )
        assert result.get("ok") is True, result
        assert _links(pid) == ["appended-target"]
        assert _crossrefs("rederive-insert-at") == ["appended-target"]

    def test_replace_markdown_block_rederives(self):
        pid = _insert_page(
            "rederive-md-block",
            "para one with [[block-old]]\n\npara two\n",
            links=["block-old"],
        )
        _wiki()._sync_crossrefs("rederive-md-block", ["block-old"])
        result = _wiki().replace_markdown_block(pid, "paragraph", 0, "para one with [[block-new]]")
        assert result.get("ok") is True, result
        assert _links(pid) == ["block-new"]
        assert _crossrefs("rederive-md-block") == ["block-new"]

    def test_append_section_rederives_links_array(self):
        """``append_section`` already synced crossrefs but left ``links`` stale
        — the table/array pair was inconsistent."""
        pid = _insert_page(
            "rederive-append-section",
            "## Notes\n\nexisting body\n",
        )
        result = _wiki().append_section(pid, "Notes", "and [[section-link]]")
        assert "error" not in result, result
        assert _links(pid) == ["section-link"]
        assert _crossrefs("rederive-append-section") == _links(pid)


# ── End-to-end: canonical slug through a surgical edit ───────────────────────


class TestCanonicalSlugThroughSurgicalEdit:
    """The reported symptom, whole: a canonical slug added by a surgical edit
    must land verbatim in BOTH surfaces, and resolve as a backlink."""

    def test_canonical_link_added_by_edit_is_verbatim_everywhere(self):
        pid = _insert_page("e2e-canonical", "body text placeholder")
        result = _wiki().replace_text(
            pid, "placeholder", f"[[{CANONICAL_SLUG}]] and [[{CANONICAL_SLUG_2}]]"
        )
        assert result.get("ok") is True

        expected = sorted([CANONICAL_SLUG, CANONICAL_SLUG_2])
        assert _links(pid) == expected
        assert _crossrefs("e2e-canonical") == expected
        # The user-visible symptom: exact-match backlink lookup now resolves.
        assert "e2e-canonical" in _storage().get_wiki_backlinks(CANONICAL_SLUG)


# ── Bulk corpus repair (scripts/rederive_wiki_links.py) ──────────────────────


class TestBulkRederive:
    """The forward fix does not repair rows already written; the bulk path does.

    Lives here rather than in ``yadgar/tests/scripts/`` because it needs the
    real storage engine this module already stands up.
    """

    @staticmethod
    def _rederive(**kwargs):
        kwargs.setdefault("apply_changes", False)
        return _load_script().rederive(_storage(), _wiki(), **kwargs)

    def test_dry_run_reports_but_does_not_write(self):
        pid = _insert_page(
            "bulk-dry",
            f"body links [[{CANONICAL_SLUG}]]",
            links=["m-agahi-yadgar-adr-0253"],  # the corrupted shape
        )
        _wiki()._sync_crossrefs("bulk-dry", ["m-agahi-yadgar-adr-0253"])

        tally = self._rederive(slug_prefix="bulk-dry")
        assert tally["changed"] >= 1
        assert tally["repaired"] == 0
        # Nothing written.
        assert _links(pid) == ["m-agahi-yadgar-adr-0253"]
        assert _crossrefs("bulk-dry") == ["m-agahi-yadgar-adr-0253"]

    def test_apply_repairs_both_surfaces(self):
        pid = _insert_page(
            "bulk-apply",
            f"body links [[{CANONICAL_SLUG}]]",
            links=["m-agahi-yadgar-adr-0253"],
        )
        _wiki()._sync_crossrefs("bulk-apply", ["m-agahi-yadgar-adr-0253"])

        tally = self._rederive(slug_prefix="bulk-apply", apply_changes=True)
        assert tally["repaired"] >= 1
        assert tally["failed"] == 0
        assert _links(pid) == [CANONICAL_SLUG]
        assert _crossrefs("bulk-apply") == [CANONICAL_SLUG]
        assert _crossrefs("bulk-apply") == _links(pid)

    def test_second_run_is_a_no_op(self):
        """Idempotence, observably: after a repair the dry run reports 0."""
        _insert_page(
            "bulk-idem",
            f"body links [[{CANONICAL_SLUG}]]",
            links=["m-agahi-yadgar-adr-0253"],
        )
        _wiki()._sync_crossrefs("bulk-idem", ["m-agahi-yadgar-adr-0253"])

        self._rederive(slug_prefix="bulk-idem", apply_changes=True)
        assert self._rederive(slug_prefix="bulk-idem")["changed"] == 0
        assert self._rederive(slug_prefix="bulk-idem", apply_changes=True)["repaired"] == 0

    def test_stale_links_from_surgical_edit_are_repaired(self):
        """The task-216 damage shape: content has no link, ``links`` still does."""
        pid = _insert_page("bulk-stale", "content with no links at all", links=["ghost-page"])
        _wiki()._sync_crossrefs("bulk-stale", ["ghost-page"])

        tally = self._rederive(slug_prefix="bulk-stale", apply_changes=True)
        assert tally["repaired"] >= 1
        assert _links(pid) == []
        assert _crossrefs("bulk-stale") == []

    def test_clean_page_is_not_touched(self):
        pid = _insert_page("bulk-clean", "links to [[already-right]]", links=["already-right"])
        _wiki()._sync_crossrefs("bulk-clean", ["already-right"])
        assert self._rederive(slug_prefix="bulk-clean")["changed"] == 0
        assert _links(pid) == ["already-right"]
