"""Task 0134 regression — policy recall-exclusion must be per PAGE, not per CALL.

The defect: ``WikiProvider.candidates`` gated the whole exclusion on
``if not self._tags`` — so passing ANY tag to ``recall()`` disabled the
``recall_disposition="exclude"`` filter for EVERY page in the result set, not
just the ones the caller opted into. The documented
``recall(tags=["agent-prompt"])`` lookup is an opt-in to agent-prompt pages; it
is not an opt-in to every excluded page type that happens to rank.

Correct rule (this file pins it): an excluded page survives only when it
actually carries one of the requested tags.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from yadgar._shared.wiki.wiki_meta import (
    PAGE_TYPE_AGENT_INDEX,
    PAGE_TYPE_AGENT_PATTERN,
)
from yadgar.backend.retrieval.providers.base import Scope
from yadgar.backend.retrieval.providers.wiki import WikiProvider

_DIR = "/home/user/project"


def _page(slug: str, page_type: str | None, tags: list[str] | None = None) -> dict:
    return {
        "id": abs(hash(slug)) % 100000,
        "slug": slug,
        "title": slug,
        "content": f"body of {slug}",
        "tags": tags or [],
        "directory_context": "global",
        "branch": None,
        "page_type": page_type,
        "_retrieval_score": 0.5,
    }


def _scope() -> Scope:
    return Scope(directory=_DIR)


def _slugs(provider: WikiProvider) -> list[str]:
    return [c.id for c in provider.candidates("q", _scope(), limit=10)]


class TestExclusionWithoutTags:
    """Baseline (already correct before the fix) — bare recall excludes."""

    def test_excluded_page_dropped(self):
        wiki = MagicMock()
        wiki.query.return_value = [
            _page("agent-prompt-fix-bug", PAGE_TYPE_AGENT_PATTERN, ["agent-prompt"]),
            _page("plain-page", None, ["yadgar"]),
        ]
        assert _slugs(WikiProvider(wiki)) == ["plain-page"]

    def test_toc_dropped(self):
        """agent-prompt-toc: null page_type was the 0134 hole; typed now."""
        wiki = MagicMock()
        wiki.query.return_value = [
            _page("agent-prompt-toc", PAGE_TYPE_AGENT_INDEX, ["agent-prompt-toc"]),
            _page("plain-page", None, ["yadgar"]),
        ]
        assert _slugs(WikiProvider(wiki)) == ["plain-page"]


class TestExclusionWithTags:
    """The 0134 fix: the tag opt-in is per page, not a blanket kill-switch."""

    def test_opted_in_page_survives(self):
        """recall(tags=["agent-prompt"]) must still reach agent-prompt pages."""
        wiki = MagicMock()
        wiki.query.return_value = [
            _page("agent-prompt-fix-bug", PAGE_TYPE_AGENT_PATTERN, ["agent-prompt"]),
        ]
        assert _slugs(WikiProvider(wiki, tags=["agent-prompt"])) == ["agent-prompt-fix-bug"]

    def test_non_matching_excluded_page_still_dropped(self):
        """THE REGRESSION: an excluded page NOT carrying the requested tag.

        Before the fix, passing any tag disabled the exclusion wholesale, so
        this page ranked. The TOC is tagged 'agent-prompt-toc', NOT
        'agent-prompt' — a targeted prompt lookup must not surface the index.
        """
        wiki = MagicMock()
        wiki.query.return_value = [
            _page("agent-prompt-fix-bug", PAGE_TYPE_AGENT_PATTERN, ["agent-prompt"]),
            _page("agent-prompt-toc", PAGE_TYPE_AGENT_INDEX, ["agent-prompt-toc"]),
        ]
        assert _slugs(WikiProvider(wiki, tags=["agent-prompt"])) == ["agent-prompt-fix-bug"]

    def test_unrelated_tag_does_not_unlock_excluded_pages(self):
        """recall(tags=["yadgar"]) is not an opt-in to the prompt library.

        The opt-in is the TAG INTERSECTION, so an excluded page that does not
        carry the requested tag stays excluded. (An excluded page that DOES
        carry it is consented to — see test_opt_in_is_tag_intersection.)
        """
        wiki = MagicMock()
        wiki.query.return_value = [
            _page("agent-prompt-fix-bug", PAGE_TYPE_AGENT_PATTERN, ["agent-prompt"]),
            _page("plain-page", None, ["yadgar"]),
        ]
        assert _slugs(WikiProvider(wiki, tags=["yadgar"])) == ["plain-page"]

    def test_opt_in_is_tag_intersection(self):
        """Consent is per page: any requested tag the page carries unlocks it.

        Deliberately NOT "only a tag that names the excluded family" — that
        would need a tag→page_type map, i.e. the string-matching ADR-0209
        removes. The caller's own tag filter is the consent signal.
        """
        wiki = MagicMock()
        wiki.query.return_value = [
            _page("agent-prompt-fix-bug", PAGE_TYPE_AGENT_PATTERN, ["agent-prompt", "yadgar"]),
        ]
        assert _slugs(WikiProvider(wiki, tags=["yadgar"])) == ["agent-prompt-fix-bug"]

    def test_included_page_never_gated_by_tags(self):
        """A non-excluded page passes regardless of tag intersection."""
        wiki = MagicMock()
        wiki.query.return_value = [_page("plain-page", None, ["something-else"])]
        assert _slugs(WikiProvider(wiki, tags=["agent-prompt"])) == ["plain-page"]

    def test_legacy_agent_prompt_type_still_excluded(self):
        """Un-migrated rows (page_type='agent_prompt') keep their exclusion."""
        wiki = MagicMock()
        wiki.query.return_value = [
            _page("agent-prompt-legacy", "agent_prompt", ["agent-prompt-toc"]),
            _page("plain-page", None, []),
        ]
        assert _slugs(WikiProvider(wiki, tags=["agent-prompt"])) == ["plain-page"]
