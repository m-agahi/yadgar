"""Car W2 (ledger task 219) — ``wiki_read`` must scope on the resolved project_id.

``wiki_read`` accepts ``project=``, validates it, and folds it into the CACHE
KEY — and then narrows the actual lookup on ``directory`` alone
(``wiki.py`` → ``read_by_directory(slug, _caller_dir)``).  So passing a CORRECT
``project=`` alongside a ``directory=`` belonging to a different tree yields
not-found for a page that exists.  Adding a correct scoping argument makes the
read fail.

Measured on the live corpus 2026-08-19 — same slug, same project, only
``directory=`` differs::

    wiki_read("quinyx_flux_adr-0016", project="quinyx/flux")
        -> full page (id 7952, directory_context=/home/max/quinyx/flux)
    wiki_read("quinyx_flux_adr-0016", directory="/home/max/git/yadgar",
              project="quinyx/flux")
        -> {"error": "Wiki page 'quinyx_flux_adr-0016' not found"}

ADR-0233 retires ``directory`` as a scoping key and makes ``project_id`` the
sole one; this shared read path never got the memo.  Car A6 (``2de31c0b``)
worked around it in ``adr_get`` by dropping ``directory`` entirely — a
per-caller workaround, not the fix.

BOTH DIRECTIONS ARE PINNED.  A one-direction test stays green through the whole
defect lifetime: the cross-project read must start resolving, AND the
same-project read — the live, common path — must not regress.

THE DOUBLE IS FAITHFUL IN BOTH KEYS.  ``_FakeWikiStore`` implements
``read_by_directory`` (directory-keyed, with the §25 ``'global'`` rung) AND
``read_by_project`` (project-keyed, with the Car C7 reach-tag rung), and the
tests drive the REAL ``wiki_read`` tool.  So the pre-fix RED is a behavioural
``{"error": ... not found}`` — the same shape as the measured live failure —
never an ``AttributeError`` that would only prove the new method is absent.
"""

from __future__ import annotations

from typing import Any

import pytest

# ── the corpus this car reasons about (shapes taken from the live rows) ──────

_YADGAR_PROJECT = "m-agahi/yadgar"
_YADGAR_DIR = "/home/max/git/yadgar"
_FLUX_PROJECT = "quinyx/flux"
_FLUX_DIR = "/home/max/quinyx/flux"
_FLUX_SLUG = "quinyx_flux_adr-0016"
#: Car C7: reach travels as a tag, not as ``directory_context``.
_REACH_TAG = "global"


def _page(
    slug: str,
    project_id: str,
    directory_context: str,
    *,
    content: str = "body",
    tags: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "slug": slug,
        "title": slug,
        "content": content,
        "project_id": project_id,
        "directory_context": directory_context,
        "tags": list(tags or []),
    }


class _FakeWikiStore:
    """Faithful stand-in for both resolution ladders over an in-memory corpus.

    ``pages`` is a LIST, not a slug-keyed dict, because the defect is about two
    rows sharing a slug across scopes — a dict cannot express the corpus that
    discriminates the two ladders.
    """

    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = pages
        self.dir_calls: list[tuple[str, str | None]] = []
        self.project_calls: list[tuple[str, str | None]] = []

    # §25 as it exists today: directory → 'global' → None.
    def read_by_directory(self, slug: str, caller_directory: str | None) -> dict | None:
        self.dir_calls.append((slug, caller_directory))
        rows = [p for p in self.pages if p["slug"] == slug]
        if caller_directory is None:
            return dict(rows[0]) if rows else None
        caller_dir = caller_directory.rstrip("/")
        for p in rows:
            if p["directory_context"] == caller_dir:
                return dict(p)
        for p in rows:
            if p["directory_context"] == "global":
                return dict(p)
        return None

    # §25 re-keyed onto project_id: own project → global reach tag → None.
    def read_by_project(self, slug: str, project_id: str | None) -> dict | None:
        self.project_calls.append((slug, project_id))
        rows = [p for p in self.pages if p["slug"] == slug]
        for p in rows:
            if project_id and p.get("project_id") == project_id:
                return dict(p)
        for p in rows:
            if _REACH_TAG in (p.get("tags") or []):
                return dict(p)
        return None


@pytest.fixture
def wiki_tool(monkeypatch):
    """Return ``(wiki_module, wire)``; ``wire(pages)`` installs the fake store."""
    import yadgar._shared.runtime.state as _state
    from yadgar._shared.runtime import cache_epoch as _epoch_bus
    from yadgar.core.server.tools import wiki as wtool

    # The read cache folds the GLOBAL wiki epoch into its key; pin the bus so a
    # sibling test's write cannot move it mid-test (same reset the Car 2 cache
    # suite uses).
    _epoch_bus._reset_for_test()
    wtool._wiki_read_cache.clear()
    wtool._wiki_read_cache.hits = wtool._wiki_read_cache.misses = 0

    def wire(pages: list[dict[str, Any]]) -> _FakeWikiStore:
        fake = _FakeWikiStore(pages)
        monkeypatch.setattr(_state, "_wiki", fake)
        return fake

    yield wtool, wire
    wtool._wiki_read_cache.clear()
    _epoch_bus._reset_for_test()


# ── the defect: a correct project= must not be defeated by a foreign dir ─────


class TestWikiReadIsProjectKeyed:
    def test_cross_project_read_resolves_with_a_foreign_directory(self, wiki_tool):
        """THE MEASURED FAILURE.

        The flux page exists.  The caller names the CORRECT ``project=`` and a
        ``directory=`` belonging to another tree.  Today the lookup narrows on
        the directory and the page is structurally unreachable.
        """
        wtool, wire = wiki_tool
        wire([_page(_FLUX_SLUG, _FLUX_PROJECT, _FLUX_DIR, content="# ADR-0016")])

        page = wtool.wiki_read(_FLUX_SLUG, directory=_YADGAR_DIR, project=_FLUX_PROJECT)

        assert "error" not in page, (
            f"a correct project= was defeated by a foreign directory= — got {page!r}"
        )
        assert page["content"] == "# ADR-0016"
        assert page["project_id"] == _FLUX_PROJECT

    def test_same_project_read_still_resolves(self, wiki_tool):
        """The direction that WORKS today and must not regress."""
        wtool, wire = wiki_tool
        wire([_page("m-agahi_yadgar_adr-0233", _YADGAR_PROJECT, _YADGAR_DIR, content="# 0233")])

        page = wtool.wiki_read(
            "m-agahi_yadgar_adr-0233", directory=_YADGAR_DIR, project=_YADGAR_PROJECT
        )

        assert "error" not in page, f"same-project read regressed: {page!r}"
        assert page["content"] == "# 0233"

    def test_directory_argument_cannot_change_the_resolved_page(self, wiki_tool):
        """ADR-0233's property, stated as a test.

        Same slug, same ``project=``; only ``directory=`` differs — including
        the no-directory call.  All four must resolve to the SAME page.

        THE CACHE IS CLEARED BETWEEN VARIANTS ON PURPOSE.  Once ``directory``
        leaves the cache KEY, a warm entry from the first variant answers every
        later one, and the assertion would pass with the old directory-keyed
        LOOKUP still in place — the test would be measuring the cache, not the
        resolution.  ``TestReadCacheStaysProjectScoped`` covers the key; this
        covers the lookup.
        """
        wtool, wire = wiki_tool
        wire([_page(_FLUX_SLUG, _FLUX_PROJECT, _FLUX_DIR, content="# ADR-0016")])

        calls: list[dict[str, Any]] = [
            {"project": _FLUX_PROJECT},
            {"directory": _FLUX_DIR, "project": _FLUX_PROJECT},
            {"directory": _YADGAR_DIR, "project": _FLUX_PROJECT},
            {"directory": "/tmp/some/other/tree", "project": _FLUX_PROJECT},
        ]
        variants = []
        for kwargs in calls:
            wtool._wiki_read_cache.clear()
            variants.append(wtool.wiki_read(_FLUX_SLUG, **kwargs))

        assert all("error" not in v for v in variants), (
            f"directory= changed whether the page resolved: {[v.get('error') for v in variants]!r}"
        )
        assert all(v["content"] == "# ADR-0016" for v in variants)

    def test_lookup_is_never_narrowed_to_the_caller_directory(self, wiki_tool):
        """The discriminating structural assertion.

        No lookup may be issued against the caller's directory.  Pins the fix
        at the seam so a future edit cannot reintroduce the narrowing while
        the behavioural tests stay green through a lucky corpus.
        """
        wtool, wire = wiki_tool
        fake = wire([_page(_FLUX_SLUG, _FLUX_PROJECT, _FLUX_DIR)])

        wtool.wiki_read(_FLUX_SLUG, directory=_YADGAR_DIR, project=_FLUX_PROJECT)

        assert not fake.dir_calls, f"lookup narrowed on directory: {fake.dir_calls!r}"
        assert fake.project_calls == [(_FLUX_SLUG, _FLUX_PROJECT)], (
            f"expected one project-keyed lookup; got {fake.project_calls!r}"
        )


# ── the reach rung: a global-reach page stays visible from every project ─────


class TestGlobalReachSurvivesTheReKey:
    def test_global_reach_page_resolves_from_another_project(self, wiki_tool):
        """ADR-0171/ADR-0159: the agent-prompt library is cross-project.

        Its rows carry ANOTHER project's ``project_id`` (ownership) plus the
        Car C7 reach tag (reach).  Rung 2 is what keeps them reachable — a fix
        that only implements rung 1 makes the whole library invisible.
        """
        wtool, wire = wiki_tool
        wire(
            [
                _page(
                    "agent-prompt-pr-review",
                    "local/aws-work",
                    "global",
                    content="# pr-review",
                    tags=[_REACH_TAG, "agent-prompt"],
                )
            ]
        )

        page = wtool.wiki_read(
            "agent-prompt-pr-review", directory=_YADGAR_DIR, project=_YADGAR_PROJECT
        )

        assert "error" not in page, f"global-reach page unreachable: {page!r}"
        assert page["content"] == "# pr-review"

    def test_own_project_page_wins_over_a_global_reach_page_of_the_same_slug(self, wiki_tool):
        """§25's precedence: own project FIRST, reach as the FALLBACK.

        Reds on a single-clause ``(project_id = $p OR $reach IN tags)`` lookup,
        which with ``LIMIT 1`` picks between the two rows arbitrarily.
        """
        wtool, wire = wiki_tool
        wire(
            [
                _page(
                    "shared-slug",
                    "local/aws-work",
                    "global",
                    content="REACH",
                    tags=[_REACH_TAG],
                ),
                _page("shared-slug", _YADGAR_PROJECT, _YADGAR_DIR, content="OWN"),
            ]
        )

        page = wtool.wiki_read("shared-slug", directory=_YADGAR_DIR, project=_YADGAR_PROJECT)

        assert page.get("content") == "OWN", (
            f"own-project rung must win over the reach rung; got {page!r}"
        )

    def test_absent_page_still_reports_not_found(self, wiki_tool):
        wtool, wire = wiki_tool
        wire([])

        page = wtool.wiki_read("nope", directory=_YADGAR_DIR, project=_YADGAR_PROJECT)

        assert page.get("error") == "Wiki page 'nope' not found"


# ── the cache must not serve one project's page to another ───────────────────


class TestReadCacheStaysProjectScoped:
    def test_two_projects_sharing_a_slug_each_get_their_own_row(self, wiki_tool):
        """A fixed lookup must not be able to serve a stale cross-project entry.

        Same slug in two projects, read back-to-back through the SAME caller
        directory: each read must return its OWN project's row.  This is the
        cache-hole check the re-key needs — dropping ``directory`` from the key
        is only safe while ``project_id`` remains in it.  Pre-fix it reds for a
        different reason (the directory-keyed lookup answered both reads with
        the row that happened to live in that tree), which is the defect.
        """
        wtool, wire = wiki_tool
        wire(
            [
                _page("dupe", _YADGAR_PROJECT, _YADGAR_DIR, content="YADGAR"),
                _page("dupe", _FLUX_PROJECT, _FLUX_DIR, content="FLUX"),
            ]
        )

        first = wtool.wiki_read("dupe", directory=_YADGAR_DIR, project=_YADGAR_PROJECT)
        second = wtool.wiki_read("dupe", directory=_YADGAR_DIR, project=_FLUX_PROJECT)

        assert first["content"] == "YADGAR"
        assert second["content"] == "FLUX", (
            f"the second project's read served the first project's row: {second!r}"
        )

    def test_identical_read_is_served_from_cache(self, wiki_tool):
        """The Car 2 cache must survive the re-key (no per-call store hit)."""
        wtool, wire = wiki_tool
        fake = wire([_page(_FLUX_SLUG, _FLUX_PROJECT, _FLUX_DIR)])

        wtool.wiki_read(_FLUX_SLUG, directory=_YADGAR_DIR, project=_FLUX_PROJECT)
        wtool.wiki_read(_FLUX_SLUG, directory=_YADGAR_DIR, project=_FLUX_PROJECT)

        assert len(fake.project_calls) + len(fake.dir_calls) == 1, (
            "second identical read must be served from cache"
        )
