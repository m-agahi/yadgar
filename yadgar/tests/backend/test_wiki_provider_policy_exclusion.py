"""Task 0134 + Car C1 — policy recall-exclusion is per PAGE_TYPE, not per page tag.

The original 0134 defect: ``WikiProvider.candidates`` gated the whole exclusion
on ``if not self._tags`` — so passing ANY tag to ``recall()`` disabled the
``recall_disposition="exclude"`` filter for EVERY page in the result set, not
just the ones the caller opted into. 0134 narrowed it to a per-page tag
intersection: an excluded page survived iff any requested tag was in
``page.tags``.

Car C1 narrows it one step further (§1.4 of the master plan): the unlock key is
the page TYPE's declared ``opt_in_tag`` (a per-policy field), NOT the page's
own tag set. ADR-0209 makes page_type the policy lever; the opt-in follows.

Correct rule (this file pins it):
- An excluded page survives iff ``policy.opt_in_tag`` is not None AND that tag
  is in the caller's ``opt_in_tags``. The page's own tags are irrelevant — the
  type owns the opt-in key.
- A type with ``opt_in_tag=None`` is excluded unconditionally — no tag
  unlocks it. ``agent_index`` (the TOC) is unconditional.
- ``agent_pattern`` / ``agent_discipline`` / legacy ``agent_prompt`` declare
  ``opt_in_tag="agent-prompt"`` — the documented ``recall(tags=["agent-prompt"])``
  lookup reaches ONLY those types, never an unrelated excluded type.
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

    def test_toc_unconditional_exclusion(self):
        """Car C1: agent_index declares opt_in_tag=None — no tag unlocks it.

        Regression under bare recall (no tag), under ``agent-prompt`` (the
        library opt-in tag), AND under the TOC's own tag (``agent-prompt-toc``).
        The type owns the opt-in key, not the page's tags.
        """
        toc = _page("agent-prompt-toc", PAGE_TYPE_AGENT_INDEX, ["agent-prompt-toc"])
        for tags in [None, ["agent-prompt"], ["agent-prompt-toc"]]:
            wiki = MagicMock()
            wiki.query.return_value = [toc, _page("plain-page", None, ["yadgar"])]
            assert _slugs(WikiProvider(wiki, tags=tags) if tags else WikiProvider(wiki)) == [
                "plain-page"
            ]


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

    def test_opt_in_must_match_type_own_tag(self):
        """Car C1: opt-in is the TYPE's declared opt_in_tag, not the page's tags.

        The 0134 rule unlocked an excluded page on any tag intersection — so an
        ``agent_pattern`` page carrying ``["agent-prompt", "yadgar"]`` survived
        ``recall(tags=["yadgar"])``. C1 reverses that: the page is DROPPED on
        a non-opt-in tag and SURVIVES only on the type's own opt-in tag.
        """
        wiki = MagicMock()
        wiki.query.return_value = [
            _page("agent-prompt-fix-bug", PAGE_TYPE_AGENT_PATTERN, ["agent-prompt", "yadgar"]),
        ]
        # yadgar is NOT the type's opt_in_tag → page dropped (reverse of 0134).
        assert _slugs(WikiProvider(wiki, tags=["yadgar"])) == []
        # agent-prompt IS the type's opt_in_tag → page survives.
        assert _slugs(WikiProvider(wiki, tags=["agent-prompt"])) == ["agent-prompt-fix-bug"]

    def test_included_page_never_gated_by_tags(self):
        """A non-excluded page passes regardless of tag intersection."""
        wiki = MagicMock()
        wiki.query.return_value = [_page("plain-page", None, ["something-else"])]
        assert _slugs(WikiProvider(wiki, tags=["agent-prompt"])) == ["plain-page"]

    def test_legacy_type_survives_own_opt_in_tag(self):
        """Car C1: legacy ``agent_prompt`` type declares opt_in_tag="agent-prompt".

        Pre-migration-028 rows (page_type='agent_prompt') now share the same
        opt-in tag as the split library types, so the documented
        ``recall(tags=["agent-prompt"])`` lookup reaches them. Bare recall still
        drops them.
        """
        legacy = _page("agent-prompt-legacy", "agent_prompt", ["agent-prompt-toc"])
        plain = _page("plain-page", None, [])
        wiki = MagicMock()
        wiki.query.return_value = [legacy, plain]
        # Bare recall → legacy still excluded; plain-page (include) survives.
        assert _slugs(WikiProvider(wiki)) == ["plain-page"]
        # Tagged with the type's opt_in_tag → legacy surfaces alongside plain.
        assert _slugs(WikiProvider(wiki, tags=["agent-prompt"])) == [
            "agent-prompt-legacy",
            "plain-page",
        ]


# ── F. Car C2 — downweight penalty at the provider is NOT applied ──────────────


class TestDownweightPenalty:
    """Car C2 (0047 §7 3b): a downweight page survives the visibility filter.

    The penalty is applied DOWNSTREAM (in ``fuse_candidates`` and in
    ``wiki_query``), NOT in the provider — the provider's ``native_score`` is
    the raw retrieval observation and must stay score-agnostic to avoid
    double-penalization (provider + fusion) and to keep the provider simple.

    This file pins that contract: ``WikiProvider.candidates`` returns a
    downweight-disposition page UNCHANGED in ``native_score`` and with no
    field carrying the multiplier — the penalty is the SCORING stage's job.
    The behavioural reordering is asserted in ``test_fusion_tiebreak.py``
    (fusion path) and ``test_wiki_query_policy_exclusion.py`` (legacy search).
    """

    def test_task_list_page_survives_visibility_filter(self):
        """A task_list page is NOT excluded — downweight is a ranking penalty.

        ``is_recall_visible`` returns True for ``recall_disposition="downweight"``,
        so the provider returns the page. Its ``native_score`` is the raw
        retrieval score, UNMODIFIED by any policy-aware multiplier.
        """
        from yadgar._shared.wiki.wiki_meta import PAGE_TYPE_TASK_LIST

        wiki = MagicMock()
        wiki.query.return_value = [
            _page("open-tasks", PAGE_TYPE_TASK_LIST, []),
            _page("plain-page", None, []),
        ]
        assert _slugs(WikiProvider(wiki)) == ["open-tasks", "plain-page"]

    def test_task_list_native_score_unchanged_in_provider(self):
        """``Candidate.native_score`` is the raw retrieval score, not penalized.

        The penalty is applied at the ranking-decision point (fusion's
        ``placement_score`` and wiki_query's ``_retrieval_score``); the
        provider returns observations, not rankings. Double-penalization
        (provider + fusion) is the failure mode this pins against.
        """
        from yadgar._shared.wiki.wiki_meta import PAGE_TYPE_TASK_LIST

        wiki = MagicMock()
        wiki.query.return_value = [_page("open-tasks", PAGE_TYPE_TASK_LIST, [])]
        provider = WikiProvider(wiki)
        cands = provider.candidates("q", _scope(), limit=10)
        assert len(cands) == 1
        # The retrieval score was 0.5 in _page(...); the provider must NOT
        # have applied any downweight factor.
        assert cands[0].native_score == 0.5, (
            f"Provider must not apply downweight penalty; got native_score="
            f"{cands[0].native_score} (expected 0.5, the raw retrieval score)"
        )

    def test_task_list_raw_carries_page_type_for_fusion(self):
        """The Candidate's ``raw`` dict carries ``page_type`` for downstream.

        ``downweight_multiplier`` reads ``page.get("page_type")`` — the
        provider's ``raw`` dict must carry it through (set at
        ``providers/wiki.py:104``: ``raw = dict(page)``). This pins that
        contract so the fusion penalty has the data it needs without
        re-querying.
        """
        from yadgar._shared.wiki.wiki_meta import PAGE_TYPE_TASK_LIST

        wiki = MagicMock()
        wiki.query.return_value = [_page("open-tasks", PAGE_TYPE_TASK_LIST, [])]
        cands = WikiProvider(wiki).candidates("q", _scope(), limit=10)
        assert cands[0].raw.get("page_type") == PAGE_TYPE_TASK_LIST
