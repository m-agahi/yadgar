"""Car 8 (task 283) — fusion priors must never outrank the query signal.

``_FusionMixin._fuse_scores`` applies two precomputed priors (``graph_prior``,
``cofire_prior``) AFTER fusion. Until this car the application was purely
ADDITIVE and therefore query-independent::

    fused_scores[mid] = fused_scores[mid] + weight * prior_val

A row that barely matched the query — or did not match it at all beyond the
min-max floor — received the same absolute boost as a rank-1 hit, so a
popular-but-irrelevant row could be lifted over a genuinely relevant one.
``config.py`` claimed the opposite ("must not dominate vector(1.0)/fts(0.5)")
and nothing tested the claim.

These tests assert the claim for the first time. They are written against the
PUBLIC ``_fuse_scores`` surface with the SHIPPED default weights, so they pin
the behaviour a real recall gets — not a hand-picked weight.

RED-VERIFY, measured against the pre-fix tree (additive, 0.2 / 0.15) —
3 failed, 5 passed:
  * ``test_max_priors_never_outrank_a_materially_better_match`` — RED on BOTH
    fusion methods. This is the magnitude gate.
  * ``test_floor_row_with_max_priors_never_outranks_a_positive_match`` — RED on
    ``convex`` (the production default). This is the weight-INDEPENDENT
    discriminator: an additive term lifts a zero-score row above a
    positive-score row at ANY non-zero weight, and multiplicative cannot at any
    weight. It passes pre-fix on ``wrrf`` only because that branch drops
    zero-total rows from ``fused_scores`` before the boost is applied, so the
    additive term never reaches them there.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from yadgar._shared.config import Settings
from yadgar.backend.retrieval.fusion import _FusionMixin

# The SHIPPED defaults — the invariant must hold for what production actually runs.
_GRAPH_W = Settings.model_fields["WRRF_GRAPH_PRIOR_WEIGHT"].default
_COFIRE_W = Settings.model_fields["WRRF_COFIRE_PRIOR_WEIGHT"].default

# Both fusion methods are exercised: "convex" is the production default
# (``FUSION_METHOD``), "wrrf" is the alternate branch of the same `if`.
_METHODS = ["convex", "wrrf"]

_MAX_PRIOR = 1.0  # priors are normalized to [0, 1] by consolidation


def _make_settings(fusion_method: str):
    s = MagicMock()
    s.WRRF_VECTOR_WEIGHT = 1.0
    s.WRRF_FTS_WEIGHT = 0.5
    s.WRRF_PPR_WEIGHT = 0.5
    s.WRRF_SPREADING_WEIGHT = 0.3
    s.WRRF_GRAPH_PRIOR_WEIGHT = _GRAPH_W
    s.WRRF_COFIRE_PRIOR_WEIGHT = _COFIRE_W
    s.FUSION_METHOD = fusion_method
    s.FUSION_NORM = "zscore"
    s.COMBMNZ_ENABLED = False
    return s


def _make_fuser(fusion_method: str, prior_ids: dict[int, float]):
    """Build a Retriever-shaped fuser whose storage returns `prior_ids` for BOTH priors."""
    storage = MagicMock()
    storage.get_memory_graph_priors.return_value = dict(prior_ids)
    storage.get_memory_cofire_priors.return_value = dict(prior_ids)

    class _TestFuser(_FusionMixin):
        def __init__(self) -> None:
            self._settings = _make_settings(fusion_method)
            self._storage = storage
            self._reranker = MagicMock()
            self._reranker.compute_signal_confidence.return_value = 1.0

    return _TestFuser()


def _row(vector: float, fts: float) -> dict[str, float]:
    return {"vector": vector, "fts": fts, "ppr": 0.0, "spread": 0.0}


@pytest.mark.parametrize("fusion_method", _METHODS)
def test_floor_row_with_max_priors_never_outranks_a_positive_match(fusion_method):
    """A row at the normalization floor + maximum priors must stay below a real match.

    Weight-independent: any ADDITIVE term gives the floor row `w * 1.0 > 0`,
    which beats id 20's tiny-but-positive query score at every non-zero weight.
    A multiplicative term leaves a zero score at zero.
    """
    # id 30 (highest id — the tie-break would favour it) is the floor row.
    scores = {
        10: _row(0.90, 0.90),  # clear match, no priors
        20: _row(0.11, 0.11),  # barely matched, no priors
        30: _row(0.10, 0.10),  # normalization floor, MAXIMUM priors
    }
    fuser = _make_fuser(fusion_method, {30: _MAX_PRIOR})

    fused, fused_scores = fuser._fuse_scores(scores=scores, w_temporal=0.0, open_domain_mode=False)
    ranked = [mid for mid, _ in fused]

    assert ranked[0] == 10, f"the best query match must rank first; got {ranked}"
    # The wrrf branch drops zero-total rows from `fused_scores` outright; the convex
    # branch keeps them at 0.0. Either is correct — what must never happen is the
    # floor row climbing above id 20, which did match the query.
    assert 30 not in ranked or ranked.index(30) > ranked.index(20), (
        f"a floor-scoring row with graph_prior={_MAX_PRIOR} and cofire_prior={_MAX_PRIOR} "
        f"must not outrank id 20, which actually matched the query; got {ranked} "
        f"({fused_scores})"
    )


@pytest.mark.parametrize("fusion_method", _METHODS)
def test_max_priors_never_outrank_a_materially_better_match(fusion_method):
    """Maximum priors must not flip a row that is materially worse on the query.

    id 30's query relevance is 15% below id 10's after normalization. The priors
    are at their ceiling. `config.py`'s "secondary nudge" claim means id 10 wins.
    """
    scores = {
        10: _row(0.90, 0.90),  # best match, NO priors
        20: _row(0.10, 0.10),  # floor anchor so id 30 does not normalize to 0
        30: _row(0.78, 0.78),  # 15% weaker after min-max, MAXIMUM priors
    }
    fuser = _make_fuser(fusion_method, {30: _MAX_PRIOR})

    fused, fused_scores = fuser._fuse_scores(scores=scores, w_temporal=0.0, open_domain_mode=False)
    ranked = [mid for mid, _ in fused]

    assert ranked[0] == 10, (
        f"id 10 matches the query better than id 30; maximum priors "
        f"(graph={_GRAPH_W}, cofire={_COFIRE_W}) must not overturn that. "
        f"got {ranked} ({fused_scores})"
    )


@pytest.mark.parametrize("fusion_method", _METHODS)
def test_prior_still_reorders_equally_relevant_rows(fusion_method):
    """The FEATURE survives: among comparable matches, the higher prior wins.

    id 40 carries the prior and has the LOWER id, so the `(score, id)`-descending
    tie-break favours id 50. If id 40 ranks first, the prior is what moved it.
    """
    scores = {
        40: _row(0.50, 0.50),  # prior
        50: _row(0.50, 0.50),  # no prior
    }
    fuser = _make_fuser(fusion_method, {40: 0.8})

    fused, fused_scores = fuser._fuse_scores(scores=scores, w_temporal=0.0, open_domain_mode=False)
    ranked = [mid for mid, _ in fused]

    assert ranked[0] == 40, (
        f"among equally-relevant rows the higher prior must rank first; got {ranked}"
    )
    assert fused_scores[40] > fused_scores[50]


@pytest.mark.parametrize("fusion_method", _METHODS)
def test_priors_apply_on_both_fusion_paths(fusion_method):
    """Both branches of the `FUSION_METHOD` `if` reach the prior application.

    `fused_scores` is populated in BOTH branches (`dict(fused)` for convex,
    the mixin's return for wrrf), so the boost is never applied to a stale or
    empty dict. Asserted so a future refactor cannot silently drop the convex
    branch's assignment.
    """
    scores = {
        40: _row(0.50, 0.50),
        50: _row(0.50, 0.50),
    }
    fuser = _make_fuser(fusion_method, {40: 0.8})

    fuser._fuse_scores(scores=scores, w_temporal=0.0, open_domain_mode=False)

    fuser._storage.get_memory_graph_priors.assert_called_once()
    fuser._storage.get_memory_cofire_priors.assert_called_once()
