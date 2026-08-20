"""Car B2 — ledger tasks 197 (write side) + 195 (the read that never happens).

TASK 197, WRITE SIDE — ``_flip_adr_status`` never re-derives ``tier``
---------------------------------------------------------------------
``MariaStorageEngine._flip_adr_status`` flips ``status`` and (on the supersede
flip only) stamps ``superseded_at``.  It leaves ``tier`` exactly as the row was
created with.  So a row created ``accepted`` / ``tier='binding'`` and later
superseded keeps ``tier='binding'`` while its status says ``superseded`` — a
D27 contradiction written by the system itself.

Measured on the live corpus 2026-08-19: ``adr_list(tier="historical")`` returns
ZERO rows for ``m-agahi/yadgar`` despite 20 historical-status rows (14
superseded + 6 rejected), all carrying stored ``tier='binding'``.  Car B1's
``adr_tier_where`` rescues the NULL-tier cohort by classifying on ``status``;
it deliberately does NOT override a row that carries an explicit — and wrong —
``tier``, because a stored value that disagrees with its status is a WRITE bug,
not a read bug.  This is that write bug.  Repairing the 20 rows without this
fix would re-rot on the very next supersede.

THE ``archived`` FLIP IS WHY THIS IS NOT ``_tier_for_status(status)``
--------------------------------------------------------------------
``_flip_adr_status`` has exactly two callers: ``ledger.add_adr_supersedes``
(→ ``'superseded'``) and ``nightly_sweep._sweep_project_adrs``
(→ ``'archived'``).  ``'archived'`` is not a D27 status, and both
``core.server.tools.adr._tier_for_status`` and this module's read-side
classification map anything unrecognised to ``'binding'``.  That default is
correct for a CREATE (an unclassified new ADR is binding) and CATASTROPHIC for
this FLIP: the nightly sweep only ever archives rows that are already
superseded / rejected / deprecated, so re-deriving their tier from
``'archived'`` would silently re-tier the entire historical cohort back to
``binding`` on a cron — a bigger version of the bug being fixed.

So the flip is THREE-way: historical, binding, or LEAVE THE COLUMN ALONE.  The
third arm is pinned below and is the nightly-sweep regression guard.

TASK 195 — the ``adr_supersedes`` join is written and never read
----------------------------------------------------------------
``adr_add(supersedes="ADR-NNNN")`` reports ``supersedes: "none"`` on the
superseder and ``superseded_by: "-"`` on the target, in 22/22 supersede-bearing
ADRs across two corpora.  The brief for this car described that as "the value
never reaches the superseder's own column".  There is no such column: migration
002 gives ``adr`` exactly ``(id, project_id, title, status, decided_on,
subsystem, tier, body_slug, superseded_at, created_at, updated_at)`` and the
supersede relation lives ONLY in the ``adr_supersedes`` join table.

The join row demonstrably EXISTS.  ``ledger.add_adr_supersedes`` runs the join
INSERT and the target status flip in one ``try`` block, INSERT first — so the
flip observed in 22/22 cases is only reachable through an INSERT that did not
raise.  The defect is therefore strictly READ-side: nothing in the codebase
ever selects from ``adr_supersedes``, so ``_row_to_adr_list_entry`` and
``_row_to_response_metadata`` both fall through ``row.get("supersedes")`` →
``None`` → the ``"none"`` / ``"-"`` placeholders.  ``MariaStorageEngine`` grew
no reader for that table (``adr.py:115`` records this as out-of-scope for Car
F and nothing picked it up since).

WHY THESE TESTS EXECUTE REAL SQL
--------------------------------
Same reason as the Car B1 file whose harness they reuse: there is no MariaDB in
the yadgar-ci image, and the house convention for this layer is a static SQL
string grep.  A grep cannot express "the stored ``tier`` column is now
``historical``" or "the ``tier`` column was NOT touched", which is the exact
property task 197 needs pinned, and it agrees with any plausible-looking
string.  These tests capture the real statement + bind params the engine
methods emit, execute them against stdlib ``sqlite3`` over fixture tables built
from ``lc.ADR_COLUMNS``, and then SELECT THE ROW BACK and assert the STORED
value.  Plain ``UPDATE`` / ``SELECT`` / ``JOIN`` / named params behave
identically in SQLite and MariaDB.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime
from typing import Any

import pytest

# ``sqlalchemy`` is gated by the ``sql`` extra — the yadgar-ci image skips it.
pytest.importorskip("sqlalchemy", reason="sqlalchemy not installed (sql extra)")

from yadgar._shared.storage.sql import MariaStorageEngine  # noqa: E402, I001
from yadgar._shared.storage.sql import ledger_columns as lc  # noqa: E402
from yadgar.tests._shared.test_adr_car_b1_ledger_sql import _capture  # noqa: E402

_YADGAR = "m-agahi/yadgar"
_FLUX = "quinyx/flux"


# ---------------------------------------------------------------------------
# SQLite fixture — the two tables the statements under test touch
# ---------------------------------------------------------------------------


def _corpus(
    adr_rows: tuple[dict[str, Any], ...],
    edges: tuple[tuple[int, int], ...] = (),
) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    columns = [c.strip() for c in lc.ADR_COLUMNS.split(",")]
    conn.execute(f"CREATE TABLE adr ({', '.join(f'{c} TEXT' for c in columns)})")  # noqa: S608
    conn.execute("CREATE TABLE adr_supersedes (adr_id TEXT, supersedes_id TEXT)")
    for row in adr_rows:
        full = {c: row.get(c) for c in columns}
        full["title"] = row.get("title") or f"ADR {row['id']}"
        conn.execute(
            f"INSERT INTO adr ({', '.join(columns)}) "  # noqa: S608
            f"VALUES ({', '.join(':' + c for c in columns)})",
            full,
        )
    for adr_id, supersedes_id in edges:
        conn.execute(
            "INSERT INTO adr_supersedes (adr_id, supersedes_id) VALUES (?, ?)",
            (adr_id, supersedes_id),
        )
    return conn


def _stored_tier(
    sql: str,
    params: dict,
    seeded: tuple[dict[str, Any], ...],
    row_id: int,
) -> str | None:
    """Run the captured UPDATE against the fixture, return the row's STORED tier.

    The point of the round-trip: a captured-string assertion would pass for a
    statement that names ``tier`` and writes the wrong value, and would also
    pass for one that names it in a comment.  Reading the column back cannot.
    """
    # ``superseded_at`` is a real ``datetime`` (MariaDB DATETIME). SQLite's
    # implicit datetime adapter is deprecated in 3.12+ and ADR-0087's
    # zero-warning gate turns that DeprecationWarning into a failure, so the
    # value is stringified HERE — in the harness, not by weakening the method.
    bound = {k: (v.isoformat(sep=" ") if isinstance(v, datetime) else v) for k, v in params.items()}
    conn = _corpus(seeded)
    try:
        conn.execute(sql, bound)
        row = conn.execute("SELECT tier FROM adr WHERE id = ?", (row_id,)).fetchone()
        assert row is not None, f"fixture row {row_id} vanished"
        return None if row["tier"] is None else str(row["tier"])
    finally:
        conn.close()


def _flip(adr_id: int, status: str) -> tuple[str, dict]:
    return _capture(lambda engine: engine._flip_adr_status(adr_id, status))


# ---------------------------------------------------------------------------
# Task 197 — the flip re-derives tier, in three directions
# ---------------------------------------------------------------------------


class TestFlipReDerivesTier:
    """D27: ``superseded|rejected|deprecated`` → historical, ``open|accepted``
    → binding, anything else → the column is not written at all."""

    def test_supersede_flip_re_tiers_to_historical(self) -> None:
        """The live defect: 20 yadgar rows sit at ``binding`` after this flip."""
        seeded = ({"id": 24, "project_id": _YADGAR, "status": "accepted", "tier": "binding"},)
        sql, params = _flip(24, "superseded")
        assert _stored_tier(sql, params, seeded, 24) == lc.TIER_HISTORICAL

    def test_deprecated_flip_re_tiers_to_historical(self) -> None:
        """``superseded`` is not the only historical status (D27 names three)."""
        seeded = ({"id": 25, "project_id": _YADGAR, "status": "accepted", "tier": "binding"},)
        sql, params = _flip(25, "deprecated")
        assert _stored_tier(sql, params, seeded, 25) == lc.TIER_HISTORICAL

    def test_binding_flip_is_not_wrongly_re_tiered(self) -> None:
        """The other direction: a flip BACK to a binding status must not leave
        the row ``historical``, and must not invent ``historical`` either."""
        seeded = ({"id": 26, "project_id": _YADGAR, "status": "superseded", "tier": "historical"},)
        sql, params = _flip(26, "accepted")
        assert _stored_tier(sql, params, seeded, 26) == lc.TIER_BINDING

    def test_archived_flip_leaves_tier_untouched(self) -> None:
        """NIGHTLY-SWEEP REGRESSION GUARD.

        ``nightly_sweep._sweep_project_adrs`` flips retired rows to
        ``'archived'``, a status D27 does not classify.  A two-way
        ``historical if status in HISTORICAL else binding`` rule would re-tier
        the entire archived cohort to ``binding`` on a cron — silently, and
        every night.  The column must not be written for an unclassified
        status.
        """
        seeded = ({"id": 27, "project_id": _YADGAR, "status": "superseded", "tier": "historical"},)
        sql, params = _flip(27, "archived")
        assert _stored_tier(sql, params, seeded, 27) == lc.TIER_HISTORICAL

    def test_archived_flip_does_not_invent_a_tier_for_a_null_row(self) -> None:
        """The same guard from the other end: NULL stays NULL, so Car B1's
        status-classifying read arm keeps its jurisdiction over those rows."""
        seeded = ({"id": 28, "project_id": _YADGAR, "status": "rejected", "tier": None},)
        sql, params = _flip(28, "archived")
        assert _stored_tier(sql, params, seeded, 28) is None

    def test_supersede_flip_still_stamps_superseded_at(self) -> None:
        """C15a's stamp is not collateral damage of the tier edit."""
        _, params = _flip(24, "superseded")
        assert params.get("superseded_at") is not None

    def test_archived_flip_still_does_not_stamp_superseded_at(self) -> None:
        _, params = _flip(24, "archived")
        assert "superseded_at" not in params


class TestFlipTierMappingAgreesWithTheOtherCopies:
    """``lc.adr_tier_for_flip`` is the write-side flip classifier; it must agree
    with the read-side (``lc.HISTORICAL_STATUSES``, Car B1) and the create-side
    (``adr._tier_for_status``, Car A6) on the statuses all three classify.  The
    ONLY licensed disagreement is the unclassified arm, which is what the
    nightly-sweep guard above depends on."""

    def test_historical_statuses_map_to_historical(self) -> None:
        for status in lc.HISTORICAL_STATUSES:
            assert lc.adr_tier_for_flip(status) == lc.TIER_HISTORICAL, status

    def test_binding_statuses_map_to_binding(self) -> None:
        for status in ("open", "accepted"):
            assert lc.adr_tier_for_flip(status) == lc.TIER_BINDING, status

    def test_unclassified_status_declines_to_classify(self) -> None:
        for status in ("archived", "", None):
            assert lc.adr_tier_for_flip(status) is None, status

    def test_create_side_agrees_on_every_classified_status(self) -> None:
        from yadgar.core.server.tools.adr import _tier_for_status

        for status in (*lc.HISTORICAL_STATUSES, "open", "accepted"):
            assert lc.adr_tier_for_flip(status) == _tier_for_status(status), status


# ---------------------------------------------------------------------------
# Task 195 — somebody finally reads adr_supersedes
# ---------------------------------------------------------------------------

#: ``adr.id`` is ONE GLOBAL AUTO_INCREMENT; ids 16/17 are flux, 23–26 yadgar.
_EDGE_ROWS: tuple[dict[str, Any], ...] = (
    {"id": 16, "project_id": _FLUX, "status": "superseded", "tier": "historical"},
    {"id": 17, "project_id": _FLUX, "status": "accepted", "tier": "binding"},
    {"id": 23, "project_id": _YADGAR, "status": "superseded", "tier": "historical"},
    {"id": 24, "project_id": _YADGAR, "status": "superseded", "tier": "historical"},
    {"id": 25, "project_id": _YADGAR, "status": "accepted", "tier": "binding"},
    {"id": 26, "project_id": _YADGAR, "status": "accepted", "tier": "binding"},
    {"id": 29, "project_id": _YADGAR, "status": "superseded", "tier": "historical"},
)

#: 25 supersedes BOTH 23 and 24 (inside yadgar); 17 supersedes 16 entirely
#: inside flux; 17 ALSO supersedes 29, i.e. a CROSS-PROJECT edge whose only
#: yadgar end is the TARGET.  Car B1 stops new ones being written, and
#: pre-B1 data can still carry them — which is why the scope predicate tests
#: BOTH ends of the edge and not just the superseder's.
_EDGES: tuple[tuple[int, int], ...] = ((25, 23), (25, 24), (17, 16), (17, 29))


def _edges_for(project_id: str) -> dict[int, dict[str, list[int]]]:
    """Capture ``list_adr_supersedes``'s statement, run it, fold the result.

    The fold is the SAME shape the method itself produces; the method's own
    fold is exercised through the admin op tests.  Here the point is that the
    STATEMENT selects the right edges out of a corpus with a foreign project in
    it, which is the half a mock cannot answer.
    """
    sql, params = _capture(lambda engine: engine.list_adr_supersedes(project_id=project_id))
    conn = _corpus(_EDGE_ROWS, _EDGES)
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    out: dict[int, dict[str, list[int]]] = {}
    for row in rows:
        adr_id, target = int(row["adr_id"]), int(row["supersedes_id"])
        out.setdefault(adr_id, {"supersedes": [], "superseded_by": []})
        out.setdefault(target, {"supersedes": [], "superseded_by": []})
        out[adr_id]["supersedes"].append(target)
        out[target]["superseded_by"].append(adr_id)
    return out


class TestListAdrSupersedesReadsTheJoin:
    def test_superseder_carries_both_of_its_targets(self) -> None:
        assert sorted(_edges_for(_YADGAR)[25]["supersedes"]) == [23, 24]

    def test_target_carries_the_reverse_pointer(self) -> None:
        edges = _edges_for(_YADGAR)
        assert edges[23]["superseded_by"] == [25]
        assert edges[24]["superseded_by"] == [25]

    def test_a_row_with_no_edges_is_absent_rather_than_wrong(self) -> None:
        """26 has no edge at all.  The op fills the empty lists per row; the
        READ must not invent an entry, or 'has none' and 'not asked' merge."""
        assert 26 not in _edges_for(_YADGAR)

    def test_a_foreign_projects_edges_do_not_leak_in(self) -> None:
        """``17 → 16`` is entirely inside flux.  ``adr.id`` is global, so an
        unscoped join would hand yadgar's ``adr_list`` a flux supersede."""
        edges = _edges_for(_YADGAR)
        assert 16 not in edges
        assert edges[17]["supersedes"] == [29], "the flux-only edge rode in on 17"

    def test_an_inbound_cross_project_edge_still_surfaces(self) -> None:
        """``17 → 29`` has its ONLY yadgar end on the TARGET.

        Scoping on the superseder alone drops it, and yadgar's ADR 29 then
        reports ``superseded_by: "-"`` — the exact symptom task 195 exists to
        remove, reintroduced for the rows most likely to carry it (pre-Car-B1
        links, which is when unvalidated cross-project FKs were written).
        """
        assert _edges_for(_YADGAR)[29]["superseded_by"] == [17]

    def test_the_foreign_project_still_sees_its_own(self) -> None:
        """The scope is a filter, not a hard-coded project."""
        assert _edges_for(_FLUX)[17]["supersedes"] == [16, 29]


# ---------------------------------------------------------------------------
# Task 195 — and the fold the method performs over those rows
# ---------------------------------------------------------------------------


class _RowsConnection:
    """A connection whose ``execute`` yields fixed edge rows."""

    def __init__(self, rows: tuple[tuple[int, int], ...]) -> None:
        self._rows = rows

    async def execute(self, sql: Any, params: dict | None = None) -> tuple[tuple[int, int], ...]:
        return self._rows

    async def __aenter__(self) -> _RowsConnection:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _RowsEngine:
    def __init__(self, rows: tuple[tuple[int, int], ...]) -> None:
        self._rows = rows

    def connect(self) -> _RowsConnection:
        return _RowsConnection(self._rows)


def _fold(rows: tuple[tuple[int, int], ...]) -> dict[int, dict[str, list[int]]]:
    engine = MariaStorageEngine.__new__(MariaStorageEngine)
    object.__setattr__(engine, "_engine", _RowsEngine(rows))
    return asyncio.run(engine.list_adr_supersedes(project_id=_YADGAR))


class TestListAdrSupersedesFold:
    """The statement above returns FLAT ``(adr_id, supersedes_id)`` pairs; the
    two-direction map is built in Python.  The SQL tests execute the shipped
    statement but fold it themselves, so they cannot see a fold defect — these
    drive the method's own."""

    def test_both_directions_are_built_from_one_row(self) -> None:
        assert _fold(((25, 23),)) == {
            25: {"supersedes": [23], "superseded_by": []},
            23: {"supersedes": [], "superseded_by": [25]},
        }

    def test_a_superseder_accumulates_all_of_its_targets(self) -> None:
        assert _fold(((25, 23), (25, 24)))[25]["supersedes"] == [23, 24]

    def test_a_target_superseded_twice_accumulates_both(self) -> None:
        assert _fold(((25, 23), (26, 23)))[23]["superseded_by"] == [25, 26]

    def test_no_rows_folds_to_an_empty_map(self) -> None:
        assert _fold(()) == {}
