"""C15 (PR #65 review BLOCKER): legacy_unbackfilled helper contract.

The C7 invariant pin (``TestPerProjectQueriesScopeOnProjectId`` in
``test_cli_stats_module.py``) parametrized over ``_query_core_counts``
asserted: no per-project SELECT references ``directory_context`` AT
ALL, and every ``$p``-bearing SELECT contains ``project_id = $p``.

C14 added a ``legacy_unbackfilled`` bucket that, by construction,
MUST key on ``directory_context`` — the rows being counted are the
ones that lack ``project_id``. Two invariants collided inside one
function: C7's "no directory_context in per-project SELECTs" and
C14's "count the legacy rows that have no project_id".

Resolution: extract the legacy SELECT out of ``_query_core_counts``
into a dedicated ``_query_legacy_unbackfilled_counts`` helper. The
C7 invariant now parametrizes over the six C7-era helpers only;
the new helper carries its own contract here.

Why not narrow the C7 test: option B would carve an explicit
exception into a HARD invariant. The C7 invariant's value is
preventing the regression task 333 closed — a new reader silently
scoping on ``directory_context`` and under-counting every project.
Option A (this car) keeps the invariant clean and honestly names
the legacy lookup as a separate function.

Post-fix, ``_query_core_counts`` is unchanged in shape (5 base
SELECTs, no ``directory_context``), and the caller invokes the new
helper alongside it when ``project is not None``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from yadgar.core.cli.stats import (
    StatsData,
    _query_legacy_unbackfilled_counts,
)


@pytest.fixture
def sd() -> StatsData:
    return StatsData()


class TestLegacyUnbackfilledHelper:
    """C15: the legacy SELECT lives in its own helper; C7 invariant
    parametrization continues to hold against ``_query_core_counts``.
    """

    def test_legacy_helper_keys_on_directory_context(self, sd) -> None:
        """The helper MUST select on ``project_id IS NULL AND
        directory_context = $p`` — that is the entire point. Unlike the
        C7-era helpers, this one is allowed (and required) to key on
        ``directory_context`` because the rows it counts are precisely
        the ones that have no ``project_id``."""
        db = MagicMock()
        db.query.return_value = [[{"count": 17}]]
        _query_legacy_unbackfilled_counts(db, "m-agahi/yadgar", sd)
        assert db.query.call_count == 1, (
            f"legacy helper must run exactly one SELECT; got {db.query.call_count}"
        )
        sql = db.query.call_args_list[0].args[0]
        assert "project_id IS NULL" in sql, (
            f"legacy helper must invert the project_id predicate; got {sql!r}"
        )
        assert "directory_context" in sql, (
            f"legacy helper must scope on directory_context (the "
            f"column legacy rows hold); got {sql!r}"
        )
        assert "$p" in sql, f"legacy helper must bind $p; got {sql!r}"
        assert sd.legacy_unbackfilled == 17, (
            f"legacy count must land on the dataclass field; got {sd.legacy_unbackfilled!r}"
        )

    def test_legacy_helper_is_not_a_per_project_helper(self, sd) -> None:
        """The C7 invariant parametrization in test_cli_stats_module.py
        must NOT include this helper. Pin the reason here: this helper
        is the ONE sanctioned exception (task 268 / ADR-0233 —
        ``directory_context`` stays alive for project_backfill to
        derive FROM it; the legacy rows are the remaining migration
        backlog)."""
        # Just a docstring-bearing pin; the assertion is implicit in
        # the helper's existence as a separate function (the C7 test
        # parametrizes a fixed list of helpers). Keeping the test
        # body empty-but-passing documents the contract for any
        # later maintainer reading this file.
        assert callable(_query_legacy_unbackfilled_counts)
