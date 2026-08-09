"""Car C2 (0047 §7 3b) — downweight penalty in ``fuse_candidates``.

The downweight disposition ranks BELOW an include-disposition page of
comparable relevance. The penalty is applied to ``placement_score`` at
``retrieval/providers/fusion.py:270`` — after the CE + prior computation
and before the sort/interleave/dedup/trim steps. The penalty hits the
actual ranking key, so it propagates through interleaving, dedup, and the
final trim without further code changes.

RED-FIRST assertion: two wiki candidates with identical CE + native_score,
one ``page_type="task_list"`` (downweight) and one ``page_type=None``
(include, DEFAULT_POLICY). The include candidate must rank ABOVE the
downweight one in the fused output. Pre-fix, the penalty is absent and the
two tie on placement_score; tie-break by id desc decides, and the test
constructs ids so the downweight page LOSES that tie-break — making the
assertion fail pre-fix and pass post-fix.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from yadgar._shared.wiki.wiki_meta import PAGE_TYPE_TASK_LIST

_DIR = "/home/test/yadgar-project"


def _make_wiki_candidate(
    content: str,
    score: float,
    page_type: str | None,
    slug: str,
):
    from yadgar.backend.retrieval.providers.base import Candidate

    return Candidate(
        type="wiki",
        id=slug,
        title=content[:20],
        content=content,
        native_score=score,
        directory_context=_DIR,
        raw={
            "slug": slug,
            "content": content,
            "_retrieval_score": score,
            "directory_context": _DIR,
            "page_type": page_type,
        },
    )


def _settings(downweight_factor: float = 0.5) -> SimpleNamespace:
    """Settings carrying the v6 T6 fusion knobs + the new downweight factor."""
    return SimpleNamespace(
        RECALL_MEMORY_QUOTA=5,
        RECALL_WIKI_QUOTA=5,
        RECALL_MEMORY_PRIOR_WEIGHT=0.1,
        RECALL_WIKI_PRIOR_WEIGHT=0.1,
        RECALL_DOWNWEIGHT_FACTOR=downweight_factor,
    )


class TestDownweightPenaltyInFusion:
    """The penalty is applied to wiki candidates' ``placement_score``."""

    def test_downweight_page_ranks_below_include_page(self):
        """Two wikis, equal CE + native_score, one downweight + one include.

        With ``RECALL_DOWNWEIGHT_FACTOR=0.5``, the downweight page's placement
        score is halved while the include page's stays full. The include page
        must rank first in the fused output. The ids are constructed so a
        pre-fix tie-break by id desc would put ``task-list-page`` first
        (id="task-list-page" > id="plain-page" lexicographically), making
        this test FAIL pre-fix and PASS post-fix.
        """
        from yadgar.backend.retrieval.providers.fusion import fuse_candidates

        # Equal scores; downweight page slug > plain slug so a tie-break
        # by id desc would (wrongly) put the downweight page first.
        plain = _make_wiki_candidate("plain page about quokkas", 0.5, None, "plain-page")
        task = _make_wiki_candidate(
            "task list page about quokkas", 0.5, PAGE_TYPE_TASK_LIST, "task-list-page"
        )

        retriever = MagicMock()
        settings = _settings(downweight_factor=0.5)

        # Monkeypatch _score_candidates_ce to return the native_score for
        # every candidate so CE doesn't differentiate.
        import yadgar.backend.retrieval.providers.fusion as fusion_mod

        original_ce = fusion_mod._score_candidates_ce

        def _native_ce(candidates, query, retriever):
            return {i: c.native_score for i, c in enumerate(candidates)}

        fusion_mod._score_candidates_ce = _native_ce
        try:
            # Insert task first so a stable pre-fix sort would put it before
            # plain (insertion order). The penalty must reorder it.
            result = fuse_candidates(
                memory_candidates=[],
                wiki_candidates=[task, plain],
                query="quokkas",
                retriever=retriever,
                max_results=5,
                settings=settings,
                profile=None,
            )
        finally:
            fusion_mod._score_candidates_ce = original_ce

        assert [c.id for c in result] == ["plain-page", "task-list-page"], (
            f"Include page must rank above downweight page; got {[c.id for c in result]}"
        )

    def test_downweight_penalty_propagates_through_interleave(self):
        """Downweight wiki sinks below an include memory of equal score.

        Pre-fix the downweight wiki ties with the memory on placement and
        the tie-break puts it ahead. Post-fix the penalty halves its
        placement score so the memory wins.
        """
        from yadgar.backend.retrieval.providers.base import Candidate
        from yadgar.backend.retrieval.providers.fusion import fuse_candidates

        mem = Candidate(
            type="memory",
            id=99,
            title=None,
            content="quokka memory",
            native_score=0.5,
            directory_context=_DIR,
            raw={"id": 99, "content": "quokka memory", "_retrieval_score": 0.5},
        )
        task = _make_wiki_candidate("quokka task list", 0.5, PAGE_TYPE_TASK_LIST, "task-list-page")

        retriever = MagicMock()
        settings = _settings(downweight_factor=0.5)

        import yadgar.backend.retrieval.providers.fusion as fusion_mod

        original_ce = fusion_mod._score_candidates_ce

        def _native_ce(candidates, query, retriever):
            return {i: c.native_score for i, c in enumerate(candidates)}

        fusion_mod._score_candidates_ce = _native_ce
        try:
            result = fuse_candidates(
                memory_candidates=[mem],
                wiki_candidates=[task],
                query="quokka",
                retriever=retriever,
                max_results=5,
                settings=settings,
                profile=None,
            )
        finally:
            fusion_mod._score_candidates_ce = original_ce

        assert [c.id for c in result] == [99, "task-list-page"], (
            f"Memory (include) must rank above the downweighted task list "
            f"wiki at equal scores; got {[c.id for c in result]}"
        )

    def test_downweight_penalty_is_noop_at_factor_one(self):
        """Factor 1.0 means no penalty — downweight page ranks with include.

        A factor of 1.0 is a no-op (downweight_multiplier returns 1.0 either
        way). The penalty must NOT corrupt ranking when factor == 1.0.
        """
        from yadgar.backend.retrieval.providers.fusion import fuse_candidates

        plain = _make_wiki_candidate("plain page", 0.5, None, "plain-page")
        task = _make_wiki_candidate("task list page", 0.5, PAGE_TYPE_TASK_LIST, "task-list-page")

        retriever = MagicMock()
        settings = _settings(downweight_factor=1.0)

        import yadgar.backend.retrieval.providers.fusion as fusion_mod

        original_ce = fusion_mod._score_candidates_ce

        def _native_ce(candidates, query, retriever):
            return {i: c.native_score for i, c in enumerate(candidates)}

        fusion_mod._score_candidates_ce = _native_ce
        try:
            result = fuse_candidates(
                memory_candidates=[],
                wiki_candidates=[task, plain],
                query="x",
                retriever=retriever,
                max_results=5,
                settings=settings,
                profile=None,
            )
        finally:
            fusion_mod._score_candidates_ce = original_ce

        # With factor=1.0, the tie-break decides — task-list-page id is
        # lexicographically > plain-page, so task-list-page comes first.
        assert [c.id for c in result] == ["task-list-page", "plain-page"], (
            f"At factor=1.0, no reordering expected; got {[c.id for c in result]}"
        )
