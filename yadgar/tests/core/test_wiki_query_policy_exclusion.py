"""Task 0134 + Car C1 — wiki_query honours per-TYPE recall exclusion.

The exclusion was applied only in the unified-recall provider; ``wiki_query``
called ``WikiStore.query`` directly and never consulted ``get_policy``, so the
same excluded pages the fanout drops ranked freely through the search tool.
``wiki_read`` / ``wiki_list`` stay untouched — those are exact / enumerative
lookups, not search.

Car C1 (0047) narrows the tag-opt-in to the TYPE's declared ``opt_in_tag``
(``WikiPolicy`` field) — the page's own tags are irrelevant. The TOC declares
``opt_in_tag=None`` (unconditional exclusion); ``agent_pattern`` declares
``"agent-prompt"``.

Car C7 (0047 §5 C7) RETIRED Car C2's ``downweight`` disposition. task_list
used to resolve to ``recall_disposition="downweight"`` (D22): visible but
ranking-penalized via a multiply on ``_retrieval_score``. That multiply was a
VERIFIED SIGN BUG — a raw cross-encoder-adjacent score is commonly negative,
and multiplying by a sub-1.0 factor moves it TOWARD ZERO, an INCREASE under
"higher ranks first" — so the penalty promoted exactly the pages it meant to
sink. task_list is now ``recall_disposition="exclude"``: dropped from
``wiki_query`` results outright, in the stage-1 SQL WHERE, before any ranking
happens. ``TestWikiQueryDownweight`` below is re-pointed onto that exclusion.
"""

from __future__ import annotations

import pytest

from yadgar._shared.wiki.wiki_meta import (
    PAGE_TYPE_AGENT_INDEX,
    PAGE_TYPE_AGENT_PATTERN,
    PAGE_TYPE_TASK_LIST,
)
from yadgar.core import server
from yadgar.tests.core.conftest import TEST_PROJECT_ID

_DIR = "/tmp/wiki-query-policy"


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("wiki_query_policy")
    server.init_engines(
        db_path=str(tmp_path / "test.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


@pytest.fixture(autouse=True)
def _corpus():
    """Four pages that all match the query text; only their type/tags differ.

    The ``task_list`` page was added in Car C2 (0047 §7 3b) to exercise the
    (now-retired) downweight penalty. Car C7 flipped it to
    ``recall_disposition="exclude"`` — it stays in this fixture because the
    exclusion tests below need a task_list row to prove drops.
    """
    import yadgar._shared.runtime.state as _st

    storage = _st._storage
    pages = [
        ("plain-quokka-page", None, ["quokka"]),
        ("agent-prompt-quokka", PAGE_TYPE_AGENT_PATTERN, ["agent-prompt", "quokka"]),
        ("agent-prompt-toc-quokka", PAGE_TYPE_AGENT_INDEX, ["agent-prompt-toc", "quokka"]),
        ("task-list-quokka", PAGE_TYPE_TASK_LIST, ["quokka"]),
    ]
    for slug, page_type, tags in pages:
        if storage.get_wiki_page_by_slug(slug) is not None:
            continue
        row = {
            "slug": slug,
            "title": slug,
            "content": "quokka quokka quokka marsupial notes",
            "tags": tags,
            "links": [],
            "category": "reference",
            "confidence": "high",
            "source_memory_ids": [],
            "directory_context": "global",
            "project_id": TEST_PROJECT_ID,
            "wiki_schema_version": 1,
        }
        if page_type is not None:
            row["page_type"] = page_type
        storage.insert_wiki_page(row)
    yield


def _slugs(**kwargs) -> set[str]:
    from yadgar.core.server.tools.wiki import wiki_query

    return {
        r["slug"]
        for r in wiki_query("quokka marsupial", directory=_DIR, project=TEST_PROJECT_ID, **kwargs)
    }


class TestWikiQueryHonoursExclusion:
    def test_bare_query_drops_excluded_pages(self):
        got = _slugs(max_results=20)
        assert "plain-quokka-page" in got
        assert "agent-prompt-quokka" not in got
        assert "agent-prompt-toc-quokka" not in got

    def test_tagged_query_reaches_opted_in_pages(self):
        got = _slugs(max_results=20, tags=["agent-prompt"])
        assert "agent-prompt-quokka" in got

    def test_tagged_query_still_drops_non_matching_excluded_pages(self):
        """THE REGRESSION: the opt-in is per page, not a blanket kill-switch."""
        got = _slugs(max_results=20, tags=["agent-prompt"])
        assert "agent-prompt-toc-quokka" not in got

    def test_opt_in_must_match_type_own_tag(self):
        """Car C1: opt-in is the TYPE's declared opt_in_tag, not the page's tags.

        0134 unlocked both excluded pages on ``tags=["quokka"]`` because each
        carried the ``quokka`` tag. C1 reverses that: the type's opt-in tag is
        ``"agent-prompt"`` for ``agent_pattern`` and ``None`` (unconditional)
        for ``agent_index`` — so a non-opt-in tag no longer unlocks them.

        Car C7: ``task_list`` (recall_disposition="exclude", opt_in_tag=None)
        no longer surfaces even under its own content tag ``"quokka"`` — the
        C2 "downweight" value that stayed visible-but-penalized is retired.
        See ``TestWikiQueryDownweight`` below for the exclusion pin.
        """
        got = _slugs(max_results=20, tags=["quokka"])
        assert "plain-quokka-page" in got
        assert "task-list-quokka" not in got  # Car C7: excluded (was: visible/downweighted)
        assert "agent-prompt-quokka" not in got
        assert "agent-prompt-toc-quokka" not in got

    def test_type_opt_in_tag_unlocks(self):
        """Car C1: ``agent_pattern`` reachable on ``tags=["agent-prompt"]`` (its
        type's opt-in tag). The TOC (``agent_index``, opt_in_tag=None) stays
        excluded even on its own tag or the library tag — no caller tag
        unlocks it.
        """
        got = _slugs(max_results=20, tags=["agent-prompt"])
        assert "agent-prompt-quokka" in got
        assert "agent-prompt-toc-quokka" not in got

    def test_toc_unconditional_via_wiki_query(self):
        """Car C1: TOC dropped under bare recall, library opt-in, and own tag."""
        toc_only = {"agent-prompt-toc-quokka"}
        for tags in [None, ["agent-prompt"], ["agent-prompt-toc"]]:
            got = _slugs(max_results=20, tags=tags)
            assert "agent-prompt-toc-quokka" not in got, (
                f"TOC surfaced under tags={tags!r}: {toc_only & got}"
            )


# ── G. Car C7 (0047 §5 C7) — task_list is excluded, not downweighted ──────────


class TestWikiQueryDownweight:
    """RE-POINTED (was "TestWikiQueryDownweight" pinning Car C2's penalty).

    Car C7 retired the "downweight" disposition; task_list now resolves to
    ``recall_disposition="exclude"``. The class name is kept (renaming a test
    is fine; the file's method/class-count discipline is what matters) but
    every assertion below pins the OPPOSITE of the old contract: the page is
    dropped from ``wiki_query`` output entirely, in the stage-1 SQL WHERE,
    before the legacy ``_retrieval_score``-based ranking even runs — there is
    no reordering left to assert on, because there is no penalty left to fire.
    """

    def test_task_list_page_excluded_from_wiki_query(self):
        """The task_list page is DROPPED from ``wiki_query`` results.

        Was: ``is_recall_visible`` returns True for
        ``recall_disposition="downweight"`` — the page had to survive so a
        penalty could reorder it. Now ``recall_disposition="exclude"`` (with
        ``opt_in_tag=None``) drops it outright.
        """
        got = _slugs(max_results=20)
        assert "task-list-quokka" not in got, (
            f"task_list page must be excluded from wiki_query (Car C7 flipped "
            f"task_list to recall_disposition='exclude'); got {got}"
        )

    def test_task_list_excluded_regardless_of_tags(self):
        """``opt_in_tag=None`` means NO tag unlocks task_list — not even its own.

        Was: ``test_task_list_page_ranks_below_include_page`` (bare query,
        penalty reorders it below plain-quokka-page). Re-pointed to the
        stronger and now-only-meaningful claim: unlike ``agent_pattern``
        (opt_in_tag="agent-prompt"), no caller tag — not even the content tag
        every fixture page in this corpus carries ("quokka") — reaches a
        task_list page through search.
        """
        for tags in [None, ["quokka"], ["agent-prompt"]]:
            got = _slugs(max_results=20, tags=tags) if tags else _slugs(max_results=20)
            assert "task-list-quokka" not in got, (
                f"task_list must stay excluded under tags={tags!r}; got {got}"
            )

    def test_task_list_reachable_by_exact_slug_despite_exclusion(self):
        """Exclusion is a SEARCH-only filter — ``wiki_read`` bypasses it entirely.

        New assertion (the module docstring's claim was previously untested
        for task_list specifically): ``wiki_read``/``wiki_get``/``wiki_list``
        never apply ``recall_disposition`` — only search paths
        (``wiki_query``, unified recall fanout) do. Confirms the exclusion
        tested above is a ranking/discovery gate, not a data-loss bug.
        """
        from yadgar.core.server.tools.wiki import wiki_read

        page = wiki_read("task-list-quokka", directory=_DIR, project=TEST_PROJECT_ID)
        assert page is not None
        assert page.get("slug") == "task-list-quokka"
