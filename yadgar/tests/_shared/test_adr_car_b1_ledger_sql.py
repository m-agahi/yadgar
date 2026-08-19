"""Car B1 — ledger tasks 188 + 191: two ``adr`` reader defects in the SQL layer.

Ledger task 188: ``MariaStorageEngine.get_adr_row`` looks a row up by ``id``
ALONE.  ``adr.id`` is ONE GLOBAL ``AUTO_INCREMENT`` shared across every project
(``quinyx/flux`` owns ids 7–22 and 257–332; ``m-agahi/yadgar`` owns 23–252), so
an unscoped by-id lookup returns FOREIGN rows as a matter of routine, not as a
corner case.  ``adr_get`` then merges that foreign row's metadata (status, tier,
subsystem, decided_on, hashes) onto this project's body page — a chimera.
Car A6 (``2de31c0b``) added a defensive guard on the BODY half
(``_fetch_adr_body_page`` ignores a ``body_slug`` inconsistent with the resolved
project_id), so the prose cannot cross projects; the ROW lookup was left open
and is what this file closes.

Ledger task 191: ``adr_list`` defaults its ``tier`` filter to ``"binding"`` and
forwards it verbatim, so a NULL-tier row matches neither ``"binding"`` nor
``"historical"``.  The premise "invisible through every argument value" is
NOT quite right — ``tier=None`` omits the WHERE clause entirely and DOES
surface NULL rows — but the DEFAULT hides them, and the default is what every
consumer gets.  Measured on the live corpus 2026-08-19: 237 rows unfiltered,
233 under ``tier="binding"``, 0 under ``tier="historical"`` → 4 rows reachable
only by a caller who knows to pass ``tier=None``.

DECISION (see the car report): a NULL-tier row is classified by its STATUS,
using the same D27 mapping the WRITE side already applies
(``adr._tier_for_status``, landed in Car A6).  ``superseded|rejected|deprecated``
→ historical, everything else → binding.  A blanket "NULL is binding" would put
historical-status rows into the default list, which is exactly what D27 excludes
them from; status-derivation instead makes every NULL row reachable through
exactly ONE filter value and agrees with what ``seed_adr_tier_subsystem`` would
stamp, so running that backfill later changes nothing observable.

WHY THESE TESTS EXECUTE REAL SQL
--------------------------------
``test_mariadb_ledger_crud`` states the house convention for this layer: no
MariaDB in the yadgar-ci image, so assertions are static SQL-string greps.  A
string grep cannot express "a row belonging to ANOTHER project is NOT returned",
which is the exact property task 188 needs pinned — and a NULL-vs-``=`` matching
question (191) is precisely where SQL's three-valued logic bites and a grep
agrees with any plausible-looking string.

So these tests CAPTURE the real SQL text + real bind params the engine methods
emit (a fake engine handle; no DB, no driver, no refactor of the methods) and
then EXECUTE that text against stdlib ``sqlite3`` over a fixture ``adr`` table.
The SQL under test is the shipped SQL, and NULL semantics are real NULL
semantics.  The dialect gap is acceptable for these two queries: plain
``SELECT`` + ``WHERE`` + named params + ``IS NULL`` + ``IN`` are identical in
SQLite and MariaDB.
"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any

import pytest

# ``sqlalchemy`` is gated by the ``sql`` extra — the yadgar-ci image skips it.
pytest.importorskip("sqlalchemy", reason="sqlalchemy not installed (sql extra)")

from yadgar._shared.storage.sql import MariaStorageEngine  # noqa: E402, I001
from yadgar._shared.storage.sql import ledger_columns as lc  # noqa: E402


# ---------------------------------------------------------------------------
# SQL capture harness — no DB, no driver, no change to the methods under test
# ---------------------------------------------------------------------------


class _EmptyResult:
    """Stands in for a SQLAlchemy ``Result`` that matched nothing.

    The methods under test consume their result two ways — ``.first()``
    (``get_adr_row``) and iteration (``list_adr_rows``) — so this supports both
    and yields nothing either way.  The rows are irrelevant here: the harness
    exists to capture the STATEMENT, which is then run for real against SQLite.
    """

    def first(self) -> None:
        return None

    def __iter__(self):
        return iter(())


class _CapturingConnection:
    def __init__(self, sink: list[tuple[str, dict]]) -> None:
        self._sink = sink

    async def execute(self, sql: Any, params: dict | None = None) -> _EmptyResult:
        self._sink.append((str(sql), dict(params or {})))
        return _EmptyResult()

    async def __aenter__(self) -> _CapturingConnection:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _CapturingEngine:
    """Minimal stand-in for ``AsyncEngine`` — ``connect()`` / ``begin()`` only."""

    def __init__(self) -> None:
        self.statements: list[tuple[str, dict]] = []

    def connect(self) -> _CapturingConnection:
        return _CapturingConnection(self.statements)

    def begin(self) -> _CapturingConnection:
        return _CapturingConnection(self.statements)


def _capture(coro_factory) -> tuple[str, dict]:
    """Run one engine method against a capturing handle; return (sql, params)."""
    engine = MariaStorageEngine.__new__(MariaStorageEngine)
    engine._engine = _CapturingEngine()  # type: ignore[attr-defined]
    asyncio.run(coro_factory(engine))
    statements = engine._engine.statements  # type: ignore[attr-defined]
    assert len(statements) == 1, f"expected exactly one statement, got {statements!r}"
    return statements[0]


# ---------------------------------------------------------------------------
# SQLite fixture corpus — two projects sharing one global id sequence
# ---------------------------------------------------------------------------

#: ``adr`` rows as they really are: ONE id space, MANY projects.  ``tier`` is
#: NULL on ids 24 and 25 — the two shapes task 191 has to classify.
_FIXTURE_ROWS: tuple[dict[str, Any], ...] = (
    # quinyx/flux — the foreign rows task 188 must never return to yadgar.
    {"id": 16, "project_id": "quinyx/flux", "status": "accepted", "tier": "binding"},
    {"id": 22, "project_id": "quinyx/flux", "status": "accepted", "tier": None},
    # m-agahi/yadgar
    {"id": 23, "project_id": "m-agahi/yadgar", "status": "accepted", "tier": "binding"},
    {"id": 24, "project_id": "m-agahi/yadgar", "status": "accepted", "tier": None},
    {"id": 25, "project_id": "m-agahi/yadgar", "status": "superseded", "tier": None},
    {"id": 26, "project_id": "m-agahi/yadgar", "status": "superseded", "tier": "historical"},
    {"id": 27, "project_id": "m-agahi/yadgar", "status": "rejected", "tier": None},
)

_YADGAR = "m-agahi/yadgar"
_FLUX = "quinyx/flux"


def _sqlite_corpus() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    columns = [c.strip() for c in lc.ADR_COLUMNS.split(",")]
    conn.execute(f"CREATE TABLE adr ({', '.join(f'{c} TEXT' for c in columns)})")  # noqa: S608
    for row in _FIXTURE_ROWS:
        full = {c: row.get(c) for c in columns}
        full.setdefault("title", f"ADR {row['id']}")
        conn.execute(
            f"INSERT INTO adr ({', '.join(columns)}) "  # noqa: S608
            f"VALUES ({', '.join(':' + c for c in columns)})",
            full,
        )
    return conn


def _run(sql: str, params: dict) -> list[int]:
    """Execute the CAPTURED statement against the fixture; return matched ids.

    ``sqlite3.Connection`` as a context manager commits a TRANSACTION — it does
    NOT close the handle — so the close is explicit here (ADR-0087's zero-warning
    gate turns the resulting ResourceWarning into a failure).
    """
    conn = _sqlite_corpus()
    try:
        return [int(r["id"]) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Task 188 — get_adr_row must be project-scoped
# ---------------------------------------------------------------------------


class TestGetAdrRowIsProjectScoped:
    """A by-id lookup must never reach across the shared id sequence.

    NON-VACUOUS BY CONSTRUCTION: the same id (16) is asked for from BOTH
    projects.  A query that ignores project_id returns the row in both
    directions, so it cannot pass the pair; a query that scopes returns it only
    to its owner.  A test using only same-project ids passes against the
    unfixed code.
    """

    def test_foreign_project_row_is_not_returned(self) -> None:
        sql, params = _capture(
            lambda e: e.get_adr_row(16, project_id=_YADGAR),
        )
        assert _run(sql, params) == [], (
            "ADR id 16 belongs to quinyx/flux; a m-agahi/yadgar lookup must not "
            "return it — adr_get would merge its metadata onto a yadgar body page"
        )

    def test_own_project_row_is_still_returned(self) -> None:
        """The same-project direction must keep working (one-direction tests
        stay green through the whole defect lifetime)."""
        sql, params = _capture(
            lambda e: e.get_adr_row(16, project_id=_FLUX),
        )
        assert _run(sql, params) == [16]

    def test_project_id_is_bound_not_interpolated(self) -> None:
        sql, params = _capture(
            lambda e: e.get_adr_row(23, project_id=_YADGAR),
        )
        assert params.get("project_id") == _YADGAR, (
            f"project_id must reach the driver as a bind param; got {params!r}"
        )
        assert _YADGAR not in sql, "project_id must not be string-interpolated into the SQL"


# ---------------------------------------------------------------------------
# Task 191 — list_adr_rows must classify NULL-tier rows by status
# ---------------------------------------------------------------------------


def _list_sql(**kwargs: Any) -> tuple[str, dict]:
    return _capture(lambda e: e.list_adr_rows(project_id=_YADGAR, **kwargs))


class TestListAdrRowsNullTier:
    """NULL-tier rows are classified by STATUS under a tier filter (D27).

    Both directions are pinned: the NULL rows must land in the right bucket AND
    the rows that already carry a stored tier must be unaffected — a fix that
    simply widened ``binding`` to "or NULL" would red on
    ``test_historical_filter_includes_null_tier_historical_status``.
    """

    def test_binding_filter_includes_null_tier_binding_status(self) -> None:
        sql, params = _list_sql(tier="binding")
        assert _run(sql, params) == [23, 24], (
            "id 24 is tier=NULL status=accepted — D27 binding.  It is one of the "
            "4 live rows hidden from the DEFAULT adr_list call"
        )

    def test_binding_filter_excludes_null_tier_historical_status(self) -> None:
        sql, params = _list_sql(tier="binding")
        matched = _run(sql, params)
        assert 25 not in matched and 27 not in matched, (
            "ids 25/27 are tier=NULL with superseded/rejected status — D27 "
            "historical.  A blanket 'NULL is binding' default would surface them "
            f"in the binding list; got {matched}"
        )

    def test_historical_filter_includes_null_tier_historical_status(self) -> None:
        sql, params = _list_sql(tier="historical")
        assert _run(sql, params) == [25, 26, 27], (
            "every NULL-tier row must be reachable through exactly one filter value"
        )

    def test_no_tier_filter_returns_every_row(self) -> None:
        """``tier=None`` means no filter — unchanged behaviour, pinned."""
        sql, params = _list_sql(tier=None)
        assert _run(sql, params) == [23, 24, 25, 26, 27]

    def test_stored_tier_still_wins_for_non_null_rows(self) -> None:
        """A row with a stored tier is matched on that tier, not on its status.

        Row 26 is ``status=superseded`` AND ``tier=historical``; row 23 is
        ``status=accepted`` AND ``tier=binding``.  Neither may move.
        """
        assert 26 not in _run(*_list_sql(tier="binding"))
        assert 23 not in _run(*_list_sql(tier="historical"))

    def test_status_filter_still_composes_with_tier(self) -> None:
        sql, params = _list_sql(tier="historical", status="rejected")
        assert _run(sql, params) == [27]

    def test_unknown_tier_value_gets_plain_equality(self) -> None:
        """An out-of-enum ``tier`` must not acquire a NULL arm.

        Only the two D27 values carry a status-derived fallback; anything else
        is a plain equality match, so a typo returns nothing rather than
        silently returning every NULL-tier row.
        """
        sql, params = _list_sql(tier="bnding")
        assert _run(sql, params) == []
