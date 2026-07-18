"""C4.0 — deterministic fusion tie-break (ADR-0108 A, task #34).

Every score-only sort in the fusion path (`retrieval/fusion.py` +
`retrieval/providers/fusion.py`) must break equal scores by memory/candidate
id DESCENDING (newer-wins). Without this, equal-score rows land in
set-iteration / insertion order — nondeterministic across runs and across the
`set[int]` union in `_convex_fuse` — so a target tied at the top-N boundary
crosses or falls below the cutoff nondeterministically.

RED-VERIFY note: score-only `sorted` is STABLE, so tie order == insertion
order within ONE run — a naive "is it deterministic?" assertion PASSES pre-fix.
These tests instead construct inputs where insertion order != id-desc and
assert the SEMANTIC property (higher id first on ties), which is RED pre-fix
and GREEN post-fix.
"""

from __future__ import annotations

from hypothesis import example, given, settings
from hypothesis import strategies as st

from yadgar.backend.retrieval.fusion import (
    _convex_fuse,
    _tiebreak_key,
    _wrrf_fuse,
)


def _is_score_desc_id_desc(pairs: list[tuple[int, float]]) -> bool:
    """True iff `pairs` is sorted by score desc, ties broken by id desc."""
    for (id_a, s_a), (id_b, s_b) in zip(pairs, pairs[1:], strict=False):
        if s_a < s_b:
            return False
        if s_a == s_b and id_a < id_b:
            return False
    return True


class TestTiebreakKey:
    """The shared `(score, id)`-descending sort key."""

    def test_higher_score_wins(self):
        rows = [(1, 0.5), (2, 0.9)]
        out = sorted(rows, key=_tiebreak_key, reverse=True)
        assert out == [(2, 0.9), (1, 0.5)]

    def test_equal_score_higher_id_wins(self):
        # Insertion order (lower id first) != desired order (higher id first),
        # so a stable score-only sort would give the WRONG order here.
        rows = [(10, 0.5), (20, 0.5), (30, 0.5)]
        out = sorted(rows, key=_tiebreak_key, reverse=True)
        assert out == [(30, 0.5), (20, 0.5), (10, 0.5)]

    def test_total_order_stable_across_shuffles(self):
        import random

        rows = [(i, float(i % 3)) for i in range(50)]
        canonical = sorted(rows, key=_tiebreak_key, reverse=True)
        for seed in range(20):
            shuffled = rows[:]
            random.Random(seed).shuffle(shuffled)
            assert sorted(shuffled, key=_tiebreak_key, reverse=True) == canonical


class TestWrrfFuseModuleTiebreak:
    """Module-level `_wrrf_fuse(ranked_lists, weights)` tie-break."""

    def test_equal_wrrf_score_breaks_by_id_desc(self):
        # Two signals, both rank the two ids identically → equal WRRF score.
        # Insert lower id first in every list so insertion order != id-desc.
        ranked = {"vector": [10, 20], "fts": [10, 20]}
        # rank-0 for both 10 and 20 across two lists gives them equal scores
        # only if they occupy the same rank in each list; force a true tie by
        # giving each id rank 0 in exactly one list.
        ranked = {"vector": [10], "fts": [20]}
        weights = {"vector": 1.0, "fts": 1.0}
        out = _wrrf_fuse(ranked, weights)
        # 10 and 20 both get w/(k+0+1) = identical score → tie → id desc.
        assert [mid for mid, _ in out] == [20, 10]

    def test_ordering_is_deterministic(self):
        ranked = {"vector": [1, 2, 3], "fts": [3, 2, 1]}
        weights = {"vector": 1.0, "fts": 1.0}
        first = _wrrf_fuse(ranked, weights)
        for _ in range(10):
            assert _wrrf_fuse(ranked, weights) == first


class TestConvexFuseModuleTiebreak:
    """Module-level `_convex_fuse` — also exercises the set[int] union order."""

    def test_equal_convex_score_breaks_by_id_desc(self):
        # Single signal, all equal raw scores → range_s == 0 → all get 0.5 →
        # every id ties. The set[int] union at :106-108 must not leak its
        # iteration order into the result.
        signal_scores = {"vector": {10: 1.0, 20: 1.0, 30: 1.0}}
        weights = {"vector": 1.0}
        out = _convex_fuse(signal_scores, weights)
        assert [mid for mid, _ in out] == [30, 20, 10]

    def test_set_union_order_does_not_leak(self):
        signal_scores = {
            "vector": {5: 1.0, 15: 1.0},
            "fts": {5: 1.0, 15: 1.0},
        }
        weights = {"vector": 1.0, "fts": 1.0}
        first = _convex_fuse(signal_scores, weights)
        for _ in range(20):
            assert _convex_fuse(signal_scores, weights) == first
        assert [mid for mid, _ in first] == [15, 5]


class TestTiebreakProperty:
    """Hypothesis: the tie-break is a deterministic total order, ties -> id desc."""

    @settings(max_examples=300)
    @given(
        st.lists(
            st.tuples(
                st.integers(min_value=0, max_value=10_000),
                # small score domain -> forces frequent ties
                st.integers(min_value=0, max_value=3).map(float),
            ),
            min_size=0,
            max_size=40,
            unique_by=lambda t: t[0],  # unique ids (fusion keys are unique)
        )
    )
    @example([(1, 0.0), (2, 0.0), (3, 0.0)])
    @example([(100, 1.0), (1, 1.0)])
    def test_sort_is_score_desc_then_id_desc(self, rows):
        out = sorted(rows, key=_tiebreak_key, reverse=True)
        assert _is_score_desc_id_desc(out)
        # deterministic: same input -> same output every time
        assert sorted(rows, key=_tiebreak_key, reverse=True) == out

    @settings(max_examples=300)
    @given(
        st.lists(
            st.tuples(
                st.integers(min_value=0, max_value=10_000),
                st.integers(min_value=0, max_value=3).map(float),
            ),
            min_size=0,
            max_size=40,
            unique_by=lambda t: t[0],
        )
    )
    def test_order_independent_of_input_permutation(self, rows):
        import random

        canonical = sorted(rows, key=_tiebreak_key, reverse=True)
        shuffled = rows[:]
        random.Random(1234).shuffle(shuffled)
        assert sorted(shuffled, key=_tiebreak_key, reverse=True) == canonical
