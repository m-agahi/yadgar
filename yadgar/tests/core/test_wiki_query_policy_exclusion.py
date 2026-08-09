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
"""

from __future__ import annotations

import pytest

from yadgar._shared.wiki.wiki_meta import PAGE_TYPE_AGENT_INDEX, PAGE_TYPE_AGENT_PATTERN
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
    """Three pages that all match the query text; only their type/tags differ."""
    import yadgar._shared.runtime.state as _st

    storage = _st._storage
    pages = [
        ("plain-quokka-page", None, ["quokka"]),
        ("agent-prompt-quokka", PAGE_TYPE_AGENT_PATTERN, ["agent-prompt", "quokka"]),
        ("agent-prompt-toc-quokka", PAGE_TYPE_AGENT_INDEX, ["agent-prompt-toc", "quokka"]),
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
        """
        got = _slugs(max_results=20, tags=["quokka"])
        assert got == {"plain-quokka-page"}

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
