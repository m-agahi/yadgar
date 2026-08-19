"""W1 (ledger task 220) — ``wiki_add(append=True)`` must reach the caller's slug.

The defect: ``run_wiki_add_replay``'s append branch called
``WikiStore.ingest(content, title, tags, source_memory_ids, project_id=...)``
and passed NONE of ``slug`` / ``directory_context`` / ``category`` /
``confidence`` / ``page_type`` / ``upsert``. ``ingest`` therefore re-derived the
slug from the TITLE, so an explicitly-slugged append either

  * merged into whatever page happened to sit at ``slugify(title)``, or
  * (the common case) created a brand-new page there — a SHADOW page carrying
    the appended content, ``directory_context="global"`` and the default
    ``category="reference"``

while the response handed the caller back the slug it asked for. Measured live
on 2026-08-19: page 8208 at ``m-agahi_yadgar_scratch-gate-probe-219b`` never
moved across three ``append=True`` calls; page 8209 at the title-derived
``scratch-gate-probe-219b`` held all three appends.

The defect is INVISIBLE exactly when ``slug == slugify(title)``, which is why it
read as intermittent. Every project-prefixed slug (``{project}_{name}`` — the
whole post-ADR-0211 scheme) diverges from its title and is therefore always hit.

**Every assertion below reads the page ACTUALLY WRITTEN** — the dict handed to
``insert_wiki_page`` / the id handed to ``update_wiki_page`` — never the
``slug`` key of the returned dict. The return value is the thing that lied: it
carries the caller's slug on both the fixed and the unfixed code, so a test
asserting on it passes green against the bug.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from yadgar._shared.wiki.contract import WikiAddOptions
from yadgar._shared.wiki.store import WikiStore
from yadgar.backend.write_exec import wiki_add_impl
from yadgar.tests.backend._mint_absent import identity_mint_absent

#: A title whose slug does NOT equal the slug the caller asks for. That gap is
#: the whole defect — a test using a title that slugifies to the explicit slug
#: passes against the unfixed code.
TITLE = "Scratch Gate Probe 219b"
TITLE_SLUG = "scratch-gate-probe-219b"
EXPLICIT_SLUG = "m-agahi_yadgar_scratch-gate-probe-219b"
PROJECT_ID = "m-agahi/yadgar"
DIRECTORY = "/home/max/git/yadgar"

SEEDED_PAGE_ID = 8208


class _FakeWikiStorage:
    """Records what was actually written, and resolves slugs DISCRIMINATINGLY.

    ``get_wiki_page_by_slug`` returns a row ONLY for a slug that was seeded.
    A fake that answered every slug with the same row would let the unfixed
    code look up ``TITLE_SLUG``, find the seeded page anyway, and update it —
    green against the bug. Same failure class as asserting on the returned
    slug, wearing a different hat.
    """

    def __init__(self, seeded: dict[str, dict] | None = None) -> None:
        self.by_slug: dict[str, dict] = dict(seeded or {})
        self.inserted: list[dict] = []
        self.updated: list[tuple[Any, dict]] = []

    def get_wiki_page_by_slug(self, slug: str) -> dict | None:
        return self.by_slug.get(slug)

    def insert_wiki_page(self, page: dict, **_: Any) -> int:
        self.inserted.append(dict(page))
        return 9000 + len(self.inserted)

    def update_wiki_page(self, page_id: Any, updates: dict, **_: Any) -> None:
        self.updated.append((page_id, dict(updates)))


def _wiki_store(
    storage: _FakeWikiStorage,
    crossref_calls: list[tuple[str, list[str]]] | None = None,
) -> WikiStore:
    """A WikiStore over the fake, with the embedding + memory-link edges stubbed.

    ``_sync_crossrefs`` is RECORDED into *crossref_calls* rather than stubbed to
    a no-op: the merge branch's link re-derivation is correct behaviour that must
    not regress, and the slug it syncs against is a second independent witness of
    where the write landed.
    """
    embeddings = MagicMock()
    embeddings.encode.return_value = b""
    embeddings.get_model_name.return_value = "fake"
    store = WikiStore(storage, embeddings)
    sink: list[tuple[str, list[str]]] = [] if crossref_calls is None else crossref_calls
    store._compute_embedding = lambda title, content: None  # type: ignore[method-assign]
    store._link_memories = lambda slug, memory_ids: None  # type: ignore[method-assign]
    store._sync_crossrefs = lambda slug, links: sink.append((slug, list(links)))  # type: ignore[method-assign]
    return store


def _append_payload(**overrides: Any) -> dict:
    payload = {
        "title": TITLE,
        "content": "appended body",
        "slug": EXPLICIT_SLUG,
        "append": True,
        "category": "decision",
        "confidence": "high",
        "tags": ["probe"],
        "source_memory_ids": [],
        "directory_context": DIRECTORY,
        "page_type": None,
        "project_id": PROJECT_ID,
        "upsert": True,
    }
    payload.update(overrides)
    return payload


def _replay(store: WikiStore, payload: dict) -> dict:
    with (
        patch("yadgar._shared.runtime.state._wiki", store),
        patch("yadgar._shared.runtime.state._file_queue", None),
        patch("yadgar.backend.write_exec.wiki_add_impl._push_event", lambda event: None),
        identity_mint_absent(),
    ):
        return wiki_add_impl.run_wiki_add_replay(payload)


def _seeded_page(content: str = "original body") -> dict:
    return {
        "id": SEEDED_PAGE_ID,
        "slug": EXPLICIT_SLUG,
        "title": TITLE,
        "content": content,
        "category": "decision",
        "tags": ["seed"],
        "source_memory_ids": [],
        "confidence": "high",
        "directory_context": DIRECTORY,
        "project_id": PROJECT_ID,
    }


# ── merge branch: the target page already exists at the explicit slug ─────────


class TestAppendMergesIntoTheExplicitlySluggedPage:
    """An append whose slug names an existing page must UPDATE that page."""

    def test_the_row_updated_is_the_explicitly_slugged_page(self) -> None:
        storage = _FakeWikiStorage({EXPLICIT_SLUG: _seeded_page()})
        _replay(_wiki_store(storage), _append_payload())

        assert [pid for pid, _ in storage.updated] == [SEEDED_PAGE_ID], (
            "append did not reach the page at the caller's slug"
        )

    def test_no_shadow_page_is_manufactured_at_the_title_slug(self) -> None:
        storage = _FakeWikiStorage({EXPLICIT_SLUG: _seeded_page()})
        _replay(_wiki_store(storage), _append_payload())

        assert storage.inserted == [], (
            f"append inserted a page instead of merging: {[p['slug'] for p in storage.inserted]}"
        )

    def test_existing_content_is_preserved_ahead_of_the_appended_body(self) -> None:
        storage = _FakeWikiStorage({EXPLICIT_SLUG: _seeded_page()})
        _replay(_wiki_store(storage), _append_payload())

        _, updates = storage.updated[0]
        assert updates["content"].startswith("original body")
        assert updates["content"].endswith("appended body")

    def test_links_are_re_derived_from_the_whole_merged_page(self) -> None:
        """The merge branch's link re-derivation is correct — do not regress it.

        ``links`` must come from existing + appended content, not from the new
        fragment alone: a page whose only ``[[alpha]]`` lives in the pre-existing
        body would silently lose that crossref.
        """
        storage = _FakeWikiStorage({EXPLICIT_SLUG: _seeded_page("body with [[alpha]]")})
        crossref_calls: list[tuple[str, list[str]]] = []
        _replay(
            _wiki_store(storage, crossref_calls),
            _append_payload(content="new body with [[beta]]"),
        )

        _, updates = storage.updated[0]
        assert updates["links"] == ["alpha", "beta"]
        assert crossref_calls == [(EXPLICIT_SLUG, ["alpha", "beta"])]

    def test_metadata_of_the_existing_page_is_left_alone(self) -> None:
        """Merge writes CONTENT, not scope/category — see the module docstring
        of ``WikiStore.ingest``: re-stamping an existing page's owner/scope on
        append lets a second writer rename a page it did not create, and the
        ``category`` default (``"reference"``) would downgrade a ``decision``
        page on every append.
        """
        storage = _FakeWikiStorage({EXPLICIT_SLUG: _seeded_page()})
        _replay(_wiki_store(storage), _append_payload(category="reference", directory_context=None))

        _, updates = storage.updated[0]
        assert "category" not in updates
        assert "directory_context" not in updates
        assert "project_id" not in updates


class TestIngestSlugResolutionInIsolation:
    """``ingest``'s OWN slug resolution, with ``add`` out of the picture.

    Every other merge assertion in this file goes through
    ``run_wiki_add_replay``, where TWO sites resolve a slug: ``ingest`` and —
    on the create fall-through — ``add``. That makes those tests pass whenever
    EITHER site is right: breaking only ``ingest``'s resolution still lands on
    the correct row, because ``add`` re-resolves ``opts.slug`` and merges
    there. (It lands with the WRONG content and the WRONG links, which is what
    the content/links assertions above catch — but the row id alone does not
    isolate the defect.) These two assertions touch ``ingest`` directly, so
    they red when and only when ``ingest``'s resolution breaks.
    """

    def test_ingest_merges_at_opts_slug_not_at_the_title_slug(self) -> None:
        storage = _FakeWikiStorage({EXPLICIT_SLUG: _seeded_page()})
        store = _wiki_store(storage)

        store.ingest(
            "appended body",
            TITLE,
            None,
            None,
            opts=WikiAddOptions(slug=EXPLICIT_SLUG, project_id=PROJECT_ID),
        )

        assert [pid for pid, _ in storage.updated] == [SEEDED_PAGE_ID]
        assert storage.inserted == []

    def test_ingest_without_a_slug_still_derives_from_the_title(self) -> None:
        storage = _FakeWikiStorage({TITLE_SLUG: {**_seeded_page(), "slug": TITLE_SLUG}})
        store = _wiki_store(storage)

        store.ingest("appended body", TITLE, None, None, opts=WikiAddOptions(project_id=PROJECT_ID))

        assert [pid for pid, _ in storage.updated] == [SEEDED_PAGE_ID]


# ── create branch: nothing exists yet at the explicit slug ────────────────────


class TestAppendCreateBranchCarriesTheWholeContract:
    """The create branch is where the ``global``-scoped shadow row came from."""

    @pytest.fixture
    def created(self) -> dict:
        storage = _FakeWikiStorage()
        _replay(_wiki_store(storage), _append_payload())
        assert len(storage.inserted) == 1
        return storage.inserted[0]

    def test_page_lands_at_the_explicit_slug(self, created: dict) -> None:
        assert created["slug"] == EXPLICIT_SLUG
        assert created["slug"] != TITLE_SLUG

    def test_page_keeps_the_callers_directory_scope(self, created: dict) -> None:
        assert created["directory_context"] == DIRECTORY, (
            "append-create reset the page's scope to a sentinel"
        )

    def test_page_keeps_the_callers_category_and_confidence(self, created: dict) -> None:
        assert created["category"] == "decision"
        assert created["confidence"] == "high"

    def test_page_keeps_the_enqueue_project_id(self, created: dict) -> None:
        assert created["project_id"] == PROJECT_ID

    def test_page_keeps_the_callers_tags(self, created: dict) -> None:
        assert created["tags"] == ["probe"]

    def test_page_type_reaches_the_inserted_row(self) -> None:
        storage = _FakeWikiStorage()
        _replay(_wiki_store(storage), _append_payload(page_type="analysis"))
        assert storage.inserted[0]["page_type"] == "analysis"


# ── back-compat: a payload with no explicit slug still derives from the title ─


class TestLegacyTitleDerivedAppendIsUnchanged:
    """``slug=None`` keeps the pre-Car-B title-derived behaviour byte-for-byte."""

    def test_merge_targets_the_title_slug(self) -> None:
        seeded = {**_seeded_page(), "slug": TITLE_SLUG}
        storage = _FakeWikiStorage({TITLE_SLUG: seeded})
        _replay(_wiki_store(storage), _append_payload(slug=None))

        assert [pid for pid, _ in storage.updated] == [SEEDED_PAGE_ID]
        assert storage.inserted == []

    def test_create_lands_at_the_title_slug(self) -> None:
        storage = _FakeWikiStorage()
        _replay(_wiki_store(storage), _append_payload(slug=None))

        assert storage.inserted[0]["slug"] == TITLE_SLUG
