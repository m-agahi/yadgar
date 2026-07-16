"""finish-viz F1 cap-affordance — /api/graph surfaces node/edge cap truncation.

When a per-type node cap (VIZ_MAX_MEMORIES/WIKI/ENTITIES) or the transition edge
cap (VIZ_MAX_TRANSITIONS) actually truncates, the payload carries `nodes_hidden` /
`edges_hidden` so the frontend can show a "N … hidden (cap)" status line — mirroring
the existing `weak_edges_hidden` pattern (never silently drop DB truth).

Scope note (per the plan + the weak_edges_hidden lesson): only the TRANSITION edge
cap is surfaced via edges_hidden because it is the one edge type with a cheap
predicate-matched total (default render gates on count>=2). The other four edge
caps carry distinct builder-side predicates whose totals are not cheaply derivable,
so counting them via a plain table count() would LIE — they are intentionally not
counted rather than reported wrong.

Critical invariant: NO-OP at the default (caps 0/-1 = unlimited → zero count
queries). These are MagicMock unit tests over the pure counting helpers.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from yadgar.backend.graph.graph_api import GraphAPI


def _api(count_result=None):
    """A GraphAPI over a mock storage; ._s._q returns count_result when set."""
    storage = MagicMock()
    if count_result is not None:
        storage._q = MagicMock(return_value=[{"c": count_result}])
    return GraphAPI(storage)


_NODES = [{"type": "memory"}] * 3 + [{"type": "wiki"}] * 2 + [{"type": "entity"}] * 5


# ── nodes_hidden ──────────────────────────────────────────────────────────────


def test_nodes_hidden_noop_at_default():
    """All caps 0 (unlimited) → 0 hidden AND zero count queries (no overhead)."""
    api = _api()
    assert api._count_nodes_hidden(_NODES, 0, 0, 0) == 0
    api._s._q.assert_not_called()


def test_nodes_hidden_negative_cap_is_unlimited():
    """-1 also means unlimited → no query, 0 hidden."""
    api = _api()
    assert api._count_nodes_hidden(_NODES, -1, -1, -1) == 0
    api._s._q.assert_not_called()


def test_nodes_hidden_memory_cap_truncates():
    """A memory cap below the DB total → hidden = total - rendered."""
    api = _api(count_result=10)  # 10 memories in DB
    # cap=3 → 3 memory nodes rendered (see _NODES) → 7 hidden
    assert api._count_nodes_hidden(_NODES, 3, 0, 0) == 7


def test_nodes_hidden_sums_across_types():
    """Multiple capped types accumulate their hidden counts."""
    api = _api(count_result=20)  # each count() returns 20
    # memory: 20-3=17, wiki: 20-2=18, entity: 20-5=15 → 50
    assert api._count_nodes_hidden(_NODES, 5, 5, 5) == 50


def test_nodes_hidden_never_negative():
    """Rendered >= total (cap not actually biting) → clamps to 0, not negative."""
    api = _api(count_result=3)  # total 3 == rendered memory 3
    assert api._count_nodes_hidden(_NODES, 5, 0, 0) == 0


def test_nodes_hidden_count_failure_is_best_effort():
    """A count() failure for one type contributes 0, does not raise."""
    api = GraphAPI(MagicMock())
    api._s._q = MagicMock(side_effect=RuntimeError("db down"))
    assert api._count_nodes_hidden(_NODES, 5, 0, 0) == 0


# ── edges_hidden ──────────────────────────────────────────────────────────────


def test_edges_hidden_noop_at_default():
    """Transition cap 0 → 0 hidden, no count query."""
    api = _api()
    assert api._count_edges_hidden([{"type": "transition"}], 0) == 0
    api._s._q.assert_not_called()


def test_edges_hidden_transition_cap_truncates():
    """Transition cap below the count>=2 total → hidden = total - rendered."""
    api = _api(count_result=100)  # 100 transitions with count>=2
    edges = [{"type": "transition"}] * 10 + [{"type": "causal"}] * 5
    # 10 transition edges rendered → 90 hidden (causal edges are not transitions)
    assert api._count_edges_hidden(edges, 10) == 90


def test_edges_hidden_never_negative():
    """Rendered >= total → clamps to 0."""
    api = _api(count_result=5)
    edges = [{"type": "transition"}] * 8
    assert api._count_edges_hidden(edges, 3) == 0


def test_edges_hidden_count_failure_is_best_effort():
    """A count() failure returns 0, does not raise."""
    api = GraphAPI(MagicMock())
    api._s._q = MagicMock(side_effect=RuntimeError("db down"))
    assert api._count_edges_hidden([{"type": "transition"}], 5) == 0
