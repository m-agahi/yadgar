"""Fix #4 (PR #65 review): stats.py counts ``legacy_unbackfilled`` rows.

Pre-fix, ``_query_core_counts`` reported the four post-re-key buckets
(total / active / stale / archived / protected) but had NO surface for
the rows that pre-date Car C0's ``project_id`` stamp. Those rows still
hold the filesystem PATH in ``directory_context`` and have
``project_id IS NULL``. A user running ``yadgar stats --project
m-agahi/yadgar`` therefore under-counts the corpus -- the legacy rows
sit in the store, are correctly migrated from the wiki side, but never
appear in any of the four buckets above (because they compare on the
new column).

Post-fix, ``_query_core_counts`` runs a fifth count: rows with
``project_id IS NULL AND directory_context = $p``. That count lands at
``sd.legacy_unbackfilled`` and the JSON output surfaces it as
``legacy_unbackfilled`` so the CLI user sees the gap.

The SQL has to key on ``directory_context`` (NOT ``project_id``) for
this specific helper -- that is the entire point. The other helpers
keep their ``project_id = $p`` predicate unchanged (C8 task 333 pin
still holds for them); this helper inverts it because the rows it
counts are precisely the ones the inversion is required to find.

A non-project run (``$p is None``) returns 0: there is no scope to
fall through to a legacy path in the global no-filter case -- the
caller is asking for everything, the legacy rows ARE everything.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from yadgar.core.cli.stats import (
    StatsData,
    _build_json_output,
    _query_core_counts,
    _query_legacy_unbackfilled_counts,
)


def _make_db(query_results=None) -> MagicMock:
    """Mock Surreal client with `query` returning the prepared result list.

    Each call to ``db.query(sql)`` pops the next item from ``query_results``;
    when exhausted, returns ``[]`` (matches an empty result set, which
    ``_count`` parses to ``0``).
    """
    db = MagicMock()
    if query_results is None:
        query_results = []
    queue = list(query_results)

    def _query(sql, params=None):  # signature matches _q's call
        if queue:
            return queue.pop(0)
        return []

    db.query.side_effect = _query
    return db


@pytest.fixture
def populated_sd() -> StatsData:
    """A StatsData with the five real core counts set, so _build_json_output
    can be exercised end-to-end without needing a live DB."""
    sd = StatsData()
    sd.total = 100
    sd.active = 80
    sd.stale = 15
    sd.archived = 5
    sd.protected = 3
    sd.legacy_unbackfilled = 7  # the new field
    return sd


class TestLegacyUnbackfilledCoreCounts:
    """Fix #4: a 5th counter for legacy rows outside the project_id scope."""

    def test_legacy_unbackfilled_field_exists_on_dataclass(self) -> None:
        """PR #65 review finding #4: StatsData must expose
        ``legacy_unbackfilled``. Pre-fix the dataclass omitted it -- the
        site had nothing to write the count onto, so even a successful SQL
        would have died on ``AttributeError``."""
        sd = StatsData()
        assert hasattr(sd, "legacy_unbackfilled")
        assert sd.legacy_unbackfilled == 0

    def test_legacy_unbackfilled_query_runs_when_project_is_set(self) -> None:
        """When ``project`` is truthy, the legacy helper runs the
        legacy-unbackfilled SELECT. The returned count lands on
        ``sd.legacy_unbackfilled``.

        C15 (PR #65 review BLOCKER): the legacy SELECT was extracted out
        of ``_query_core_counts`` into ``_query_legacy_unbackfilled_counts``
        so the C7 invariant pin in test_cli_stats_module.py (no
        ``directory_context`` in per-project SELECTs) continues to hold.
        The legacy count is no longer a query the core helper issues.
        """
        db = MagicMock()
        db.query.return_value = [[{"count": 42}]]  # always 42
        sd = StatsData()
        _query_legacy_unbackfilled_counts(db, "m-agahi/yadgar", sd)
        # One query — the legacy SELECT itself.
        assert db.query.call_count == 1, (
            f"legacy helper must run exactly one query; got {db.query.call_count}"
        )
        assert sd.legacy_unbackfilled == 42, (
            f"new count must land on the new field; got {sd.legacy_unbackfilled!r}"
        )

    def test_legacy_unbackfilled_sql_inverts_to_null_project_id(self) -> None:
        """PR #65: the legacy SELECT keys on ``project_id IS NULL AND
        directory_context = $p`` -- this is the only correct shape, because
        the rows being counted ARE the rows that lack a project_id.

        C15: the SQL lives in ``_query_legacy_unbackfilled_counts`` now,
        not in ``_query_core_counts``.
        """
        db = MagicMock()
        db.query.return_value = [[{"count": 0}]]
        _query_legacy_unbackfilled_counts(db, "m-agahi/yadgar", StatsData())
        legacy_calls = [
            call
            for call in db.query.call_args_list
            if "IS NULL" in call.args[0] and "directory_context" in call.args[0]
        ]
        assert len(legacy_calls) == 1, (
            f"legacy_unbackfilled SELECT must key on project_id IS NULL AND "
            f"directory_context = $p; queries seen: "
            f"{[c.args[0] for c in db.query.call_args_list]}"
        )
        sql = legacy_calls[0].args[0]
        assert "$p" in sql, (
            f"legacy_unbackfilled SELECT must bind $p so the CLI can scope "
            f"on the resolved identity; got {sql!r}"
        )

    def test_legacy_unbackfilled_core_counts_does_not_invert(self) -> None:
        """C15 (PR #65 review BLOCKER): ``_query_core_counts`` must NOT
        issue a SELECT that keys on ``directory_context``. The C7
        invariant pin in test_cli_stats_module.py parametrizes over
        ``_query_core_counts`` and asserts no per-project SELECT names
        ``directory_context`` AT ALL. The legacy bucket was extracted
        to ``_query_legacy_unbackfilled_counts`` precisely so this
        invariant continues to hold."""
        db = MagicMock()
        db.query.return_value = [[{"count": 0}]]
        _query_core_counts(db, "m-agahi/yadgar", StatsData())
        for call in db.query.call_args_list:
            sql = call.args[0]
            assert "directory_context" not in sql, (
                f"_query_core_counts must not key on directory_context (C7 invariant); got {sql!r}"
            )

    def test_legacy_unbackfilled_core_counts_skip_when_project_is_none(self) -> None:
        """A no-filter run (``project=None``) keeps ``_query_core_counts``
        at its 5 base SELECTs (total / active / stale / archived /
        protected). The legacy bucket is not invoked here — that's the
        CALLER's job, not ``_query_core_counts``'s."""
        db = MagicMock()
        db.query.return_value = []
        _query_core_counts(db, None, StatsData())
        for call in db.query.call_args_list:
            sql = call.args[0]
            assert "directory_context" not in sql, (
                f"global run must not key on directory_context; got {sql!r}"
            )
        assert db.query.call_count == 5, (
            f"global run keeps the 5 pre-existing selects; got {db.query.call_count}"
        )

    def test_legacy_unbackfilled_surfaces_in_json_output(self, populated_sd) -> None:
        """The CLI JSON mode must include ``legacy_unbackfilled`` so a
        caller can compute ``total + legacy_unbackfilled == pre-re-key
        corpus size``."""
        out = _build_json_output(populated_sd)
        assert "legacy_unbackfilled" in out, (
            f"JSON output missing legacy_unbackfilled; got {sorted(out)!r}"
        )
        assert out["legacy_unbackfilled"] == 7, (
            f"JSON output must echo the dataclass field; got {out.get('legacy_unbackfilled')!r}"
        )
