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

Car C2 (0047 §7 3b) — ``downweight`` disposition. The task_list page type
resolves to ``recall_disposition="downweight"`` (D22): the page stays
recall-visible (the visibility filter still drops only ``"exclude"``) but
its ranking score is multiplied by ``RECALL_DOWNWEIGHT_FACTOR`` (< 1.0) at
the scoring stage so it sinks below ``"include"`` pages of comparable
relevance. ``wiki_query`` re-sorts the result list AFTER the penalty so the
reordering takes effect within one call (the cache folds subsequent calls).
"""

from __future__ import annotations

import pytest

from yadgar._shared.wiki.wiki_meta import (
    PAGE_TYPE_AGENT_INDEX,
    PAGE_TYPE_AGENT_PATTERN,
    PAGE_TYPE_TASK_LIST,
)
from yadgar.core import server

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

    The ``task_list`` page is added in Car C2 (0047 §7 3b) — it resolves to
    ``recall_disposition="downweight"`` (D22) so the visibility filter still
    passes it but the ranking score is multiplied by ``RECALL_DOWNWEIGHT_FACTOR``.
    The fixture gives every page identical content so their pre-penalty scores
    are equal — the reordering test below then proves the penalty actually fires.
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
            "wiki_schema_version": 1,
        }
        if page_type is not None:
            row["page_type"] = page_type
        storage.insert_wiki_page(row)
    yield


def _slugs(**kwargs) -> set[str]:
    from yadgar.core.server.tools.wiki import wiki_query

    return {r["slug"] for r in wiki_query("quokka marsupial", directory=_DIR, **kwargs)}


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

        Car C2: ``task_list`` (recall_disposition="downweight") also surfaces
        — downweight is a RANKING penalty, not an exclusion, so the page
        stays visible. The penalty only reorders it BELOW plain-quokka-page;
        that reordering is asserted in TestWikiQueryDownweight below.
        """
        got = _slugs(max_results=20, tags=["quokka"])
        assert "plain-quokka-page" in got
        assert "task-list-quokka" in got  # Car C2: visible (downweight, not exclude)
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


# ── G. Car C2 (0047 §7 3b) — downweight disposition in wiki_query ─────────────


class TestWikiQueryDownweight:
    """A task_list page survives the visibility filter but ranks BELOW
    include-disposition pages of comparable relevance.

    The legacy ``wiki_query`` path has no fusion / CE — ``_retrieval_score``
    IS the ranking key. The penalty multiplies it in place and re-sorts
    before the cache + truncate so the reordering takes effect on this call
    AND on any cache hit.
    """

    def test_task_list_page_visible_in_wiki_query(self):
        """The task_list page is NOT dropped — downweight is a ranking penalty.

        ``is_recall_visible`` returns True for ``recall_disposition="downweight"``
        (the visibility filter still drops only ``"exclude"``). The page must
        remain in the result set so the penalty can reorder it.
        """
        got = _slugs(max_results=20)
        assert "task-list-quokka" in got, (
            f"task_list page should be visible (downweight is a penalty, "
            f"not an exclusion); got {got}"
        )

    def test_task_list_page_ranks_below_include_page(self):
        """The plain page ranks ABOVE the task_list page (penalty + re-sort).

        Both pages carry identical content so their pre-penalty
        ``_retrieval_score`` is equal; the penalty halves the task_list score
        so it sinks below the plain page. The re-sort happens between the
        visibility filter and the truncate, so the ordering visible to the
        caller reflects the post-penalty order.

        Tight RED-first assertion: the post-penalty scores must DIFFER
        (factor 0.5 on task_list) AND plain-page must rank above task-list.
        Pre-fix: both scores are equal (no penalty applied) — the
        pytest.fail() catches the missing penalty even if a coincidence in
        FTS tie-break order happens to put plain first.
        """
        from yadgar.core.server.tools.wiki import wiki_query

        results = wiki_query("quokka marsupial", directory=_DIR, max_results=20)
        order = [r["slug"] for r in results]
        scores = {r["slug"]: r.get("_retrieval_score") for r in results}
        assert "plain-quokka-page" in order
        assert "task-list-quokka" in order
        if scores.get("task-list-quokka") == scores.get("plain-quokka-page"):
            pytest.fail(
                f"RED: downweight penalty not applied — task-list-quokka score "
                f"({scores.get('task-list-quokka')}) == plain-quokka-page score "
                f"({scores.get('plain-quokka-page')}). Expected task-list score "
                f"to be < plain score (factor < 1.0)."
            )
        plain_idx = order.index("plain-quokka-page")
        task_idx = order.index("task-list-quokka")
        assert plain_idx < task_idx, (
            f"plain page must rank above task_list page (penalty must reorder); got order={order}"
        )
