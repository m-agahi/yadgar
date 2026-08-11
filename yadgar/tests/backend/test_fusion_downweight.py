"""Car C7 (0047 §5 C7) — the downweight multiply is DELETED from ``fuse_candidates``.

This file used to pin Car C2's "downweight" ranking penalty: a multiply on
``placement_score`` for ``task_list`` wiki pages. Car C7 retired that
mechanism outright, for two independent reasons documented in
``yadgar/_shared/wiki/policy.py`` and ``yadgar/backend/retrieval/providers/
fusion.py``:

  1. It was a VERIFIED SIGN BUG. The code computed
     ``placement_score = ce + wiki_prior_weight * native_score`` and then
     ``*= factor`` (factor in (0, 1)). ``ce`` is a raw cross-encoder logit
     and is commonly NEGATIVE, so multiplying a negative score by a
     sub-1.0 factor moves it TOWARD ZERO — which is an INCREASE under a
     "higher ranks first" sort. The penalty meant to sink a page instead
     promoted it whenever its placement score was negative.
  2. ``task_list`` is now ``recall_disposition="exclude"``, filtered out of
     the candidate pool entirely by the stage-1 SQL WHERE clause (Car C7)
     before it can reach ``fuse_candidates`` at all — filtering moved
     upstream of ranking, so a downstream ranking penalty has no job left.

The three tests below are re-pointed onto the invariants that replaced the
old ones:

  - ``test_fusion_never_special_cases_page_type``: ``fuse_candidates`` applies
    the SAME formula (``ce + wiki_prior_weight * native_score``) regardless of
    ``page_type`` — there is no branch in fusion that reads ``page_type`` at
    all anymore (exclusion is a policy/SQL concern, not a fusion concern).
  - ``test_negative_ce_logit_never_raised_by_ranking_path``: THE MONEY TEST.
    Reconstructs the exact numeric shape of the sign bug (a negative-CE
    candidate that a reintroduced ``*= factor`` would incorrectly promote
    past a less-negative peer) and asserts the correct, bug-free order.
    A regression that reintroduces any multiply on ``placement_score`` flips
    this order and fails the test.
  - ``test_fusion_never_reads_downweight_factor``: pins that
    ``fuse_candidates`` does not read a ``RECALL_DOWNWEIGHT_FACTOR`` (or any
    similarly-named) attribute off ``settings`` — the settings namespace
    passed in deliberately omits it, so any resurrected ``getattr(settings,
    "RECALL_DOWNWEIGHT_FACTOR", ...)`` would need to tolerate its absence,
    and a reintroduced *required* read would raise ``AttributeError`` here.
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
        project_id=_DIR,
        raw={
            "slug": slug,
            "content": content,
            "_retrieval_score": score,
            "project_id": _DIR,
            "page_type": page_type,
        },
    )


def _settings() -> SimpleNamespace:
    """Settings carrying only the v6 T6 fusion knobs.

    Car C7: deliberately NO ``RECALL_DOWNWEIGHT_FACTOR`` attribute — the
    setting was deleted from ``yadgar._shared.config.config`` /
    ``config_registry`` / ``config_yaml``. Omitting it here means any code
    path that unconditionally reads it raises ``AttributeError`` loudly in
    these tests rather than silently reading a value that no longer exists
    in production settings.
    """
    return SimpleNamespace(
        RECALL_MEMORY_QUOTA=5,
        RECALL_WIKI_QUOTA=5,
        RECALL_MEMORY_PRIOR_WEIGHT=0.1,
        RECALL_WIKI_PRIOR_WEIGHT=0.1,
    )


def _with_native_ce(fn):
    """Run *fn* with ``_score_candidates_ce`` monkeypatched to return native_score."""
    import yadgar.backend.retrieval.providers.fusion as fusion_mod

    original_ce = fusion_mod._score_candidates_ce

    def _native_ce(candidates, query, retriever):
        return {i: c.native_score for i, c in enumerate(candidates)}

    fusion_mod._score_candidates_ce = _native_ce
    try:
        return fn()
    finally:
        fusion_mod._score_candidates_ce = original_ce


class TestNoDownweightMechanismInFusion:
    """Car C7: ``fuse_candidates`` carries no page_type-aware ranking penalty."""

    def test_fusion_never_special_cases_page_type(self):
        """A ``task_list``-typed candidate is ranked by the SAME formula as any other.

        Pre-Car-C2 and post-Car-C7 agree here: fusion has never special-cased
        ``page_type`` inside its own ranking math (the old C2 downweight code
        read ``page_type`` via ``downweight_multiplier``, but that helper — and
        every call site — is deleted). This test picks CE scores so the
        higher-CE candidate (which happens to be ``page_type=task_list``, a
        value fusion must now treat as inert) ranks first purely by the
        ``ce + wiki_prior_weight * native_score`` formula. If any special-case
        branch for ``page_type`` were reintroduced into fusion, a plausible
        reintroduction (sink task_list below same-CE peers) would flip this
        order even though this candidate's CE is strictly higher.
        """
        from yadgar.backend.retrieval.providers.fusion import fuse_candidates

        # task-typed candidate has the HIGHER ce (0.35 vs 0.30); it must win
        # on the merits, not lose because of its page_type.
        higher_ce_task_typed = _make_wiki_candidate(
            "task list page about quokkas", 0.35, PAGE_TYPE_TASK_LIST, "task-list-page"
        )
        lower_ce_plain = _make_wiki_candidate("plain page about quokkas", 0.30, None, "plain-page")

        retriever = MagicMock()
        settings = _settings()

        result = _with_native_ce(
            lambda: fuse_candidates(
                memory_candidates=[],
                wiki_candidates=[lower_ce_plain, higher_ce_task_typed],
                query="quokkas",
                retriever=retriever,
                max_results=5,
                settings=settings,
                profile=None,
            )
        )

        assert [c.id for c in result] == ["task-list-page", "plain-page"], (
            f"page_type must not affect ranking inside fuse_candidates; "
            f"the higher-CE candidate must win regardless of page_type; "
            f"got {[c.id for c in result]}"
        )

    def test_negative_ce_logit_never_raised_by_ranking_path(self):
        """THE SIGN-BUG PIN. A negative CE score must never be RAISED by ranking.

        Reconstructs the exact numeric shape of the deleted bug:
        ``placement_score = ce + wiki_prior_weight * native_score`` with
        ``native_score=0`` for both candidates isolates ``placement_score`` to
        exactly ``ce``.

          - ``pos-ce-page``: ce=-0.6  → placement_score = -0.6 (correct winner:
            less negative)
          - ``neg-ce-page``: ce=-0.8, ``page_type=task_list`` (the old
            downweight target) → placement_score = -0.8

        Correct (bug-free) order: pos-ce-page (-0.6) ranks ABOVE neg-ce-page
        (-0.8) — this is the only mathematically correct order given these
        scores, independent of page_type.

        Under the deleted bug, ``neg-ce-page`` being ``task_list`` would have
        its placement_score multiplied by 0.5: -0.8 * 0.5 = -0.4, which is
        HIGHER than pos-ce-page's -0.6 — the multiply RAISES the negative
        score past its correct peer and FLIPS the order. This test fails
        under that bug and passes under the current (multiply-free) fusion.
        """
        from yadgar.backend.retrieval.providers.fusion import fuse_candidates

        pos = _make_wiki_candidate("pos ce page about quokkas", 0.0, None, "pos-ce-page")
        neg = _make_wiki_candidate(
            "neg ce page about quokkas", 0.0, PAGE_TYPE_TASK_LIST, "neg-ce-page"
        )

        retriever = MagicMock()
        settings = _settings()

        import yadgar.backend.retrieval.providers.fusion as fusion_mod

        original_ce = fusion_mod._score_candidates_ce

        ce_by_id = {"pos-ce-page": -0.6, "neg-ce-page": -0.8}

        def _fixed_ce(candidates, query, retriever):
            return {i: ce_by_id[c.id] for i, c in enumerate(candidates)}

        fusion_mod._score_candidates_ce = _fixed_ce
        try:
            result = fuse_candidates(
                memory_candidates=[],
                wiki_candidates=[neg, pos],
                query="quokkas",
                retriever=retriever,
                max_results=5,
                settings=settings,
                profile=None,
            )
        finally:
            fusion_mod._score_candidates_ce = original_ce

        assert [c.id for c in result] == ["pos-ce-page", "neg-ce-page"], (
            f"A negative CE logit must never be RAISED by the ranking path "
            f"(the deleted sign bug); expected pos-ce-page (-0.6) above "
            f"neg-ce-page (-0.8), got {[c.id for c in result]}"
        )

    def test_fusion_never_reads_downweight_factor(self):
        """``fuse_candidates`` must not depend on a ``RECALL_DOWNWEIGHT_FACTOR`` setting.

        ``_settings()`` deliberately omits the attribute (it no longer exists
        in production ``config.py`` / ``config_registry.py`` / ``config_yaml.py``
        — Car C7 deleted it outright). A resurrected unconditional read of
        ``settings.RECALL_DOWNWEIGHT_FACTOR`` inside fusion would raise
        ``AttributeError`` here instead of silently reading a real value from
        production settings, catching the regression at test time.
        """
        from yadgar.backend.retrieval.providers.fusion import fuse_candidates

        plain = _make_wiki_candidate("plain page", 0.5, None, "plain-page")
        task = _make_wiki_candidate("task list page", 0.5, PAGE_TYPE_TASK_LIST, "task-list-page")

        retriever = MagicMock()
        settings = _settings()

        result = _with_native_ce(
            lambda: fuse_candidates(
                memory_candidates=[],
                wiki_candidates=[task, plain],
                query="x",
                retriever=retriever,
                max_results=5,
                settings=settings,
                profile=None,
            )
        )

        assert isinstance(result, list)
        assert {c.id for c in result} == {"plain-page", "task-list-page"}
