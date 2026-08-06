"""Engine-#2 car H — the cross-engine ``check_invariants`` arm.

The fourth of ADR-0195's four operational arms (backup, restore verification,
migrations, check_invariants). Pure stdlib plus stubs: NO database, NO
``sqlalchemy``/``alembic`` import at module scope, so this file NEVER skips —
mirroring car D's ``test_engine2_migration_wiring.py``. The live half is
``yadgar/tests/integration/test_cross_engine_invariants.py``.

THE PROPERTY THIS FILE EXISTS TO PIN
------------------------------------
A check that CANNOT run must report ``unavailable``, never ``ok``. This train
exists because two vacuous passes did real damage: a partial restore
(1,484/3,622) passed a ``>=`` check on 2026-06-16 and 3,622 memories were
destroyed, and this repo's own type ratchet reported clean for its whole life by
inferring success from an absence of errors. So the arm must produce POSITIVE
evidence per check, and absence of a check is itself a violation.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from yadgar.backend.admin_exec import invariants_cross_engine as ce

# ── stubs ────────────────────────────────────────────────────────────────────


class _FakeResult:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def scalar_one(self) -> Any:
        return self._rows[0][0]

    def __iter__(self):
        return iter(self._rows)


class _FakeSqlEngine:
    """Minimal ``MariaStorageEngine`` stand-in — tables + one scalar count."""

    def __init__(
        self,
        *,
        tables: list[str] | None = None,
        config_rows: int = 0,
        revision: str | None = "0001_config",
        raises: Exception | None = None,
    ) -> None:
        self._tables = tables if tables is not None else ["alembic_version", "config"]
        self._config_rows = config_rows
        self.revision = revision
        self._raises = raises
        self.engine = object()  # the AsyncEngine handle migrate.* would receive

    async def list_tables(self) -> list[str]:
        if self._raises is not None:
            raise self._raises
        return sorted(self._tables)

    async def count_rows(self, table: str) -> int:
        if self._raises is not None:
            raise self._raises
        return self._config_rows if table == "config" else 0


class _FakeSurrealStorage:
    """Only ``_q`` is used, and only for ``schema_version``."""

    def __init__(self, versions: list[str] | None = None, raises: Exception | None = None) -> None:
        self._versions = versions
        self._raises = raises

    def _q(self, _surql: str, _params: dict | None = None) -> list:
        if self._raises is not None:
            raise self._raises
        return [{"version": v} for v in (self._versions or [])]


def _all_code_versions() -> list[str]:
    from yadgar._shared.storage.migrations import _MIGRATIONS

    return [m["version"] for m in _MIGRATIONS]


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    sql_engine: Any = None,
    heads: tuple[str, ...] = ("0001_config",),
    current: str | None = "0001_config",
    migrate_importable: bool = True,
    current_raises: Exception | None = None,
) -> None:
    """Wire the module's three seams: engine handle, alembic chain, stamped rev."""
    monkeypatch.setattr(ce, "_get_sql_engine", lambda: sql_engine)

    class _FakeMigrate:
        @staticmethod
        def heads() -> tuple[str, ...]:
            return heads

        @staticmethod
        async def current_revision(_engine: Any) -> str | None:
            if current_raises is not None:
                raise current_raises
            return current

    def _module() -> Any:
        if not migrate_importable:
            raise ImportError("No module named 'alembic'")
        return _FakeMigrate

    monkeypatch.setattr(ce, "_migrate_module", _module)


def _run(storage: Any = None) -> dict:
    return asyncio.run(ce.run_cross_engine_checks(storage or _FakeSurrealStorage()))


# ── 1. THE headline property: a check that cannot run is UNAVAILABLE ─────────


def test_engine_two_absent_reports_unavailable_never_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Engine #2 absent: every engine-dependent check says so BY NAME, none say ok.

    The most important test in the car. A status assertion alone would pass even
    if nothing ran and something else wrote the string, so this also demands the
    specific reason AND full-report coverage (next test) as positive evidence.
    """
    _install(monkeypatch, sql_engine=None)
    result = _run(_FakeSurrealStorage(_all_code_versions()))

    for name in (
        ce.CHECK_ENGINE_TWO_SCHEMA_HEAD,
        ce.CHECK_CONFIG_ROW_BASELINE,
        ce.CHECK_PAGE_ROW_DESYNC,
    ):
        check = result["checks"][name]
        assert check["status"] == ce.STATUS_UNAVAILABLE, (
            f"{name} reported {check['status']!r} with engine #2 absent — "
            "a check that cannot run must never report ok"
        )
        assert check["reason"] == ce.REASON_ENGINE_TWO_ABSENT, (
            f"{name} gave reason {check.get('reason')!r}; an unavailable check must "
            "name WHICH cause, or 'not installed' cannot be told from 'broken'"
        )


def test_engine_two_absent_does_not_shrink_the_report(monkeypatch: pytest.MonkeyPatch) -> None:
    """Absence must not drop checks from the report — that is a silent pass."""
    _install(monkeypatch, sql_engine=None)
    result = _run(_FakeSurrealStorage(_all_code_versions()))

    assert set(result["checks"]) == set(ce.REQUIRED_CHECKS)
    assert result["status"] == ce.STATUS_UNAVAILABLE, (
        "aggregate status collapsed to ok while three checks could not run"
    )


def test_aggregate_never_reports_ok_while_any_check_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _FakeSqlEngine()
    _install(monkeypatch, sql_engine=engine)
    result = _run(_FakeSurrealStorage(_all_code_versions()))
    # page_row_desync is spine-gated and therefore still unavailable today.
    assert result["checks"][ce.CHECK_PAGE_ROW_DESYNC]["status"] == ce.STATUS_UNAVAILABLE
    assert result["status"] == ce.STATUS_UNAVAILABLE
    assert result["violations"] == []


# ── 2. structural guard: a check that fails to report is a VIOLATION ─────────


def test_missing_check_is_a_violation_not_a_silent_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop a check from the registry: the arm must notice, not shrink quietly."""
    _install(monkeypatch, sql_engine=_FakeSqlEngine())
    monkeypatch.setattr(
        ce,
        "_CHECK_REGISTRY",
        tuple(c for c in ce._CHECK_REGISTRY if c[0] != ce.CHECK_CONFIG_ROW_BASELINE),
    )
    result = _run(_FakeSurrealStorage(_all_code_versions()))

    assert result["status"] == ce.STATUS_VIOLATION
    assert any(ce.CHECK_CONFIG_ROW_BASELINE in v for v in result["violations"])
    assert result["checks"][ce.CHECK_CONFIG_ROW_BASELINE]["status"] == ce.STATUS_VIOLATION


# ── 3. assertion 1a — the alembic chain's own shape (no database) ────────────


def test_forked_alembic_chain_is_a_violation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two heads means the chain forked — comparing against heads()[0] would lie."""
    _install(monkeypatch, sql_engine=_FakeSqlEngine(), heads=("0001_config", "0002_other"))
    result = _run(_FakeSurrealStorage(_all_code_versions()))

    chain = result["checks"][ce.CHECK_ALEMBIC_CHAIN_SHAPE]
    assert chain["status"] == ce.STATUS_VIOLATION
    assert chain["detail"]["heads"] == ["0001_config", "0002_other"]
    assert result["status"] == ce.STATUS_VIOLATION


def test_chain_shape_is_checked_even_without_a_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shape half needs no engine, so engine absence must not blind it."""
    _install(monkeypatch, sql_engine=None, heads=("0001_config", "0002_other"))
    result = _run(_FakeSurrealStorage(_all_code_versions()))
    assert result["checks"][ce.CHECK_ALEMBIC_CHAIN_SHAPE]["status"] == ce.STATUS_VIOLATION


def test_sql_extra_absent_is_its_own_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """'not installed' must be distinguishable from 'installed and broken'."""
    _install(monkeypatch, sql_engine=None, migrate_importable=False)
    result = _run(_FakeSurrealStorage(_all_code_versions()))

    chain = result["checks"][ce.CHECK_ALEMBIC_CHAIN_SHAPE]
    assert chain["status"] == ce.STATUS_UNAVAILABLE
    assert chain["reason"] == ce.REASON_SQL_EXTRA_ABSENT


# ── 4. assertion 1b — engine #2 stamped at head ──────────────────────────────


def test_engine_two_never_migrated_is_a_violation(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, sql_engine=_FakeSqlEngine(), current=None)
    check = _run(_FakeSurrealStorage(_all_code_versions()))["checks"][
        ce.CHECK_ENGINE_TWO_SCHEMA_HEAD
    ]
    assert check["status"] == ce.STATUS_VIOLATION
    assert check["detail"]["current"] is None


def test_engine_two_behind_head_is_a_violation(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, sql_engine=_FakeSqlEngine(), heads=("0002_spine",), current="0001_config")
    check = _run(_FakeSurrealStorage(_all_code_versions()))["checks"][
        ce.CHECK_ENGINE_TWO_SCHEMA_HEAD
    ]
    assert check["status"] == ce.STATUS_VIOLATION
    assert check["detail"] == {"current": "0001_config", "head": "0002_spine"}


def test_engine_two_at_head_is_ok_with_positive_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """OK is not enough — the check must SHOW what it compared."""
    _install(monkeypatch, sql_engine=_FakeSqlEngine())
    check = _run(_FakeSurrealStorage(_all_code_versions()))["checks"][
        ce.CHECK_ENGINE_TWO_SCHEMA_HEAD
    ]
    assert check["status"] == ce.STATUS_OK
    assert check["detail"] == {"current": "0001_config", "head": "0001_config"}


def test_engine_two_read_failure_is_unavailable_not_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(
        monkeypatch,
        sql_engine=_FakeSqlEngine(),
        current_raises=RuntimeError("connection refused"),
    )
    check = _run(_FakeSurrealStorage(_all_code_versions()))["checks"][
        ce.CHECK_ENGINE_TWO_SCHEMA_HEAD
    ]
    assert check["status"] == ce.STATUS_UNAVAILABLE
    assert check["reason"] == ce.REASON_QUERY_FAILED
    assert "connection refused" in check["detail"]["error"]


# ── 5. assertion 1c — SurrealDB's own hand-rolled chain at head ──────────────


def test_surreal_pending_migration_is_a_violation() -> None:
    """A migration in code but not in schema_version = pending, not fine."""
    applied = _all_code_versions()[:-1]
    result = _run(_FakeSurrealStorage(applied))
    check = result["checks"][ce.CHECK_SURREAL_SCHEMA_HEAD]
    assert check["status"] == ce.STATUS_VIOLATION
    assert check["detail"]["missing"] == [_all_code_versions()[-1]]


def test_surreal_unknown_version_is_a_violation() -> None:
    """A row the code does not know = the DB is ahead (rolled-back daemon)."""
    result = _run(_FakeSurrealStorage([*_all_code_versions(), "999_from_the_future"]))
    check = result["checks"][ce.CHECK_SURREAL_SCHEMA_HEAD]
    assert check["status"] == ce.STATUS_VIOLATION
    assert check["detail"]["unknown"] == ["999_from_the_future"]


def test_surreal_at_head_is_ok_with_positive_evidence() -> None:
    check = _run(_FakeSurrealStorage(_all_code_versions()))["checks"][ce.CHECK_SURREAL_SCHEMA_HEAD]
    assert check["status"] == ce.STATUS_OK
    assert check["detail"]["applied"] == len(_all_code_versions())
    assert check["detail"]["head"] == _all_code_versions()[-1]


def test_surreal_query_failure_is_unavailable_not_ok() -> None:
    check = _run(_FakeSurrealStorage(raises=RuntimeError("surreal down")))["checks"][
        ce.CHECK_SURREAL_SCHEMA_HEAD
    ]
    assert check["status"] == ce.STATUS_UNAVAILABLE
    assert check["reason"] == ce.REASON_QUERY_FAILED


def test_surreal_storage_absent_is_unavailable_not_ok() -> None:
    check = asyncio.run(ce.run_cross_engine_checks(None))["checks"][ce.CHECK_SURREAL_SCHEMA_HEAD]
    assert check["status"] == ce.STATUS_UNAVAILABLE
    assert check["reason"] == ce.REASON_STORAGE_ABSENT


# ── 6. assertion 2 — the config-row baseline, exact in BOTH directions ───────


def test_config_row_above_baseline_is_a_violation(monkeypatch: pytest.MonkeyPatch) -> None:
    """This train ships config EMPTY (ADR-0203): an unexpected row is a signal."""
    _install(monkeypatch, sql_engine=_FakeSqlEngine(config_rows=1))
    check = _run(_FakeSurrealStorage(_all_code_versions()))["checks"][ce.CHECK_CONFIG_ROW_BASELINE]
    assert check["status"] == ce.STATUS_VIOLATION
    assert check["detail"] == {"rows": 1, "expected": 0}


def test_config_row_below_baseline_is_a_violation(monkeypatch: pytest.MonkeyPatch) -> None:
    """The 2026-06-16 class: a seed that did not land must NOT pass a >= check."""
    monkeypatch.setattr(ce, "EXPECTED_CONFIG_ROWS", 3)
    _install(monkeypatch, sql_engine=_FakeSqlEngine(config_rows=1))
    check = _run(_FakeSurrealStorage(_all_code_versions()))["checks"][ce.CHECK_CONFIG_ROW_BASELINE]
    assert check["status"] == ce.STATUS_VIOLATION
    assert check["detail"] == {"rows": 1, "expected": 3}


def test_config_at_baseline_is_ok_and_shows_the_count(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, sql_engine=_FakeSqlEngine(config_rows=0))
    check = _run(_FakeSurrealStorage(_all_code_versions()))["checks"][ce.CHECK_CONFIG_ROW_BASELINE]
    assert check["status"] == ce.STATUS_OK
    assert check["detail"] == {"rows": 0, "expected": 0}


def test_config_table_absent_while_stamped_at_head_is_a_violation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Head CREATES config — head-plus-no-table is a contradiction, not absence."""
    _install(monkeypatch, sql_engine=_FakeSqlEngine(tables=["alembic_version"]))
    check = _run(_FakeSurrealStorage(_all_code_versions()))["checks"][ce.CHECK_CONFIG_ROW_BASELINE]
    assert check["status"] == ce.STATUS_VIOLATION
    assert "head" in check["detail"]["message"]


def test_config_table_absent_and_unmigrated_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No table and not stamped at head: honestly cannot run — not a violation."""
    _install(monkeypatch, sql_engine=_FakeSqlEngine(tables=[]), current=None)
    check = _run(_FakeSurrealStorage(_all_code_versions()))["checks"][ce.CHECK_CONFIG_ROW_BASELINE]
    assert check["status"] == ce.STATUS_UNAVAILABLE
    assert check["reason"] == ce.REASON_CONFIG_TABLE_ABSENT


# ── 7. assertion 3 — the spine-gated desync stub, and its tripwire ───────────


def test_page_row_desync_is_unavailable_while_the_spine_is_unshipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0209's content_hash is not implemented — say so, do not report ok."""
    _install(monkeypatch, sql_engine=_FakeSqlEngine())
    check = _run(_FakeSurrealStorage(_all_code_versions()))["checks"][ce.CHECK_PAGE_ROW_DESYNC]
    assert check["status"] == ce.STATUS_UNAVAILABLE
    assert check["reason"] == ce.REASON_SPINE_NOT_SHIPPED
    assert check["detail"]["absent_tables"] == sorted(ce.SPINE_LEDGER_TABLES)


def test_page_row_desync_trips_itself_once_the_spine_lands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stub must convert to RED the moment its precondition is satisfied.

    Otherwise the spine train ships the ledger tables and this arm keeps
    reporting a comfortable 'unavailable' over data it should be comparing —
    the vacuous pass, one layer up.
    """
    _install(
        monkeypatch,
        sql_engine=_FakeSqlEngine(tables=["alembic_version", "config", "adr", "agent_pattern"]),
    )
    result = _run(_FakeSurrealStorage(_all_code_versions()))
    check = result["checks"][ce.CHECK_PAGE_ROW_DESYNC]
    assert check["status"] == ce.STATUS_VIOLATION
    assert sorted(check["detail"]["present_tables"]) == ["adr", "agent_pattern"]
    assert result["status"] == ce.STATUS_VIOLATION


# ── 8. the op body: async, always carries the arm, violations reach the top ──


def test_check_invariants_op_is_async() -> None:
    """Engine #2's driver is async-only — a sync op body could not reach it."""
    import inspect

    from yadgar.backend.admin_exec import _ADMIN_OPS

    assert inspect.iscoroutinefunction(_ADMIN_OPS["check_invariants"])


def test_op_result_always_carries_the_arm(monkeypatch: pytest.MonkeyPatch) -> None:
    from yadgar.backend.admin_exec import invariants

    monkeypatch.setattr(invariants, "_get_storage", lambda: _FakeSurrealStorage())
    monkeypatch.setattr(
        invariants,
        "_run_check_invariants",
        lambda _s: {"ok": True, "violations": [], "fixed": [], "counts": {}},
    )
    _install(monkeypatch, sql_engine=None)

    result = asyncio.run(invariants.check_invariants({}))
    assert set(result["cross_engine"]["checks"]) == set(ce.REQUIRED_CHECKS)


def test_cross_engine_violations_reach_the_top_level(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cross-engine violation must flip ok — the arm is not advisory."""
    from yadgar.backend.admin_exec import invariants

    monkeypatch.setattr(invariants, "_get_storage", lambda: _FakeSurrealStorage())
    monkeypatch.setattr(
        invariants,
        "_run_check_invariants",
        lambda _s: {"ok": True, "violations": [], "fixed": [], "counts": {}},
    )
    _install(monkeypatch, sql_engine=_FakeSqlEngine(config_rows=4))

    result = asyncio.run(invariants.check_invariants({}))
    assert result["ok"] is False
    assert any(ce.CHECK_CONFIG_ROW_BASELINE in v for v in result["violations"])


def test_unavailable_alone_does_not_flip_top_level_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Engine #2 is optional today: absence must be LOUD, not permanently red.

    A core-only install has no MariaDB. If absence flipped ok, check_invariants
    would be red everywhere and someone would special-case the arm away — the
    loudness has to come from the key always being present, not from ok.
    """
    from yadgar.backend.admin_exec import invariants

    monkeypatch.setattr(
        invariants, "_get_storage", lambda: _FakeSurrealStorage(_all_code_versions())
    )
    monkeypatch.setattr(
        invariants,
        "_run_check_invariants",
        lambda _s: {"ok": True, "violations": [], "fixed": [], "counts": {}},
    )
    _install(monkeypatch, sql_engine=None)

    result = asyncio.run(invariants.check_invariants({}))
    assert result["ok"] is True
    assert result["cross_engine"]["status"] == ce.STATUS_UNAVAILABLE
