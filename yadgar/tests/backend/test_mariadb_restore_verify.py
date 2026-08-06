"""Engine-#2 car G: the ``mariadb_restore_verify`` backend admin op — unit half.

WHAT THESE TESTS ARE FOR
------------------------
The integration half (``yadgar/tests/integration/test_mariadb_restore_arm.py``)
proves the arm REJECTS partial restores against a real server. These pin the
parts a real server cannot show cheaply:

* the artifact's ``USE`` / ``CREATE DATABASE`` statements are stripped so a
  restore can never be redirected at the LIVE schema, and anything unexpected
  in that family is a hard error rather than a silent pass-through;
* the aggregate is tri-state and ``unavailable`` NEVER reads as ``ok`` — the
  op refuses on anything that is not ``ok``, which is the opposite of car H's
  reporting arm and has to be pinned deliberately;
* a check that never reported is a VIOLATION, not an absence.

Deliberately does NOT import the ``sql`` extra: the arm shells the ``mariadb``
client exactly as car F's dump shells ``mariadb-dump``, so ``asyncmy`` /
``sqlalchemy`` are irrelevant and these tests never skip.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from yadgar.backend.admin_exec import admin_ops, restore_sql
from yadgar.backend.admin_exec.restore_sql import (
    CHECK_COLUMN_SETS,
    CHECK_ROW_IDENTITY,
    CHECK_SOURCE_UNTOUCHED,
    CHECK_TABLE_SET,
    REQUIRED_CHECKS,
    STATUS_OK,
    STATUS_UNAVAILABLE,
    STATUS_VIOLATION,
    RestoreVerificationError,
)


def _write_cnf(tmp_path: Path) -> Path:
    datadir = tmp_path / "mariadb"
    datadir.mkdir(parents=True, exist_ok=True)
    cnf = datadir / "client.cnf"
    cnf.write_text(
        "\n".join(
            [
                "[client]",
                "socket = /data/mariadb/mysqld.sock",
                "user = yadgar_app",
                "password = hunter2",
                "database = yadgar",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    cnf.chmod(0o600)
    return cnf


# ── registration ─────────────────────────────────────────────────────────────


def test_restore_verify_op_registered():
    """The op is on the /admin dispatch table — the route validates against it."""
    assert "mariadb_restore_verify" in admin_ops()


# ── the artifact filter: the belt that keeps a restore off the LIVE schema ───


def _filtered(body: str, source_db: str = "yadgar") -> list[str]:
    return list(restore_sql.filter_dump_statements(body.splitlines(keepends=True), source_db))


def test_filter_drops_the_create_database_and_use_lines():
    """``--databases`` emits both; replaying them verbatim would hit the LIVE schema."""
    body = (
        "-- MariaDB dump\n"
        "CREATE DATABASE /*!32312 IF NOT EXISTS*/ `yadgar` /*!40100 DEFAULT CHARACTER SET utf8mb4 */;\n"
        "USE `yadgar`;\n"
        "CREATE TABLE `config` (`id` bigint NOT NULL);\n"
    )
    out = "".join(_filtered(body))
    assert "CREATE DATABASE" not in out
    assert "USE " not in out
    assert "CREATE TABLE `config`" in out


def test_filter_raises_on_a_use_naming_some_other_database():
    """A redirect we did not author must be LOUD, never silently dropped."""
    body = "USE `mysql`;\nDELETE FROM user;\n"
    with pytest.raises(RuntimeError, match="USE"):
        _filtered(body)


def test_filter_raises_on_a_create_database_naming_some_other_database():
    body = "CREATE DATABASE `somewhere_else`;\n"
    with pytest.raises(RuntimeError, match="CREATE DATABASE"):
        _filtered(body)


def test_filter_is_case_insensitive_and_tolerates_leading_space():
    """Matching only on an exact upper-case prefix would leave a bypass open."""
    with pytest.raises(RuntimeError, match="USE"):
        _filtered("  use `mysql`;\n")


def test_filter_streams_rather_than_materialising():
    """The filter must be lazy — a multi-GB artifact cannot be read into memory.

    Pulling ONE item from an infinite source proves laziness; a list-returning
    implementation would hang here instead of finishing.
    """

    def _endless():
        yield "CREATE TABLE `t` (`a` int);\n"
        while True:
            yield "INSERT INTO `t` VALUES (1);\n"

    stream = restore_sql.filter_dump_statements(_endless(), "yadgar")
    assert next(iter(stream)).startswith("CREATE TABLE")


# ── tri-state aggregation ────────────────────────────────────────────────────


def test_unavailable_never_aggregates_to_ok():
    """Car H's rule, and here it is load-bearing: the op refuses on non-ok."""
    report = restore_sql._aggregate(
        {name: {"status": STATUS_OK, "detail": {}} for name in REQUIRED_CHECKS}
        | {CHECK_ROW_IDENTITY: {"status": STATUS_UNAVAILABLE, "reason": "query_failed"}}
    )
    assert report["status"] == STATUS_UNAVAILABLE
    assert report["status"] != STATUS_OK


def test_a_check_that_never_reported_is_a_violation():
    """Absence is the 2026-06-16 shape — it must not read as coverage."""
    report = restore_sql._aggregate({CHECK_TABLE_SET: {"status": STATUS_OK, "detail": {}}})
    assert report["status"] == STATUS_VIOLATION
    assert set(report["checks"]) == set(REQUIRED_CHECKS)
    assert any("did not report" in v for v in report["violations"])


def test_all_ok_aggregates_to_ok():
    """The control: without this, a report that can only fail proves nothing."""
    report = restore_sql._aggregate(
        {name: {"status": STATUS_OK, "detail": {}} for name in REQUIRED_CHECKS}
    )
    assert report["status"] == STATUS_OK
    assert report["violations"] == []


def test_required_checks_covers_the_four_names():
    assert REQUIRED_CHECKS == frozenset(
        {CHECK_TABLE_SET, CHECK_COLUMN_SETS, CHECK_ROW_IDENTITY, CHECK_SOURCE_UNTOUCHED}
    )


# ── the checks themselves, over a faked SQL seam ─────────────────────────────


class _FakeSQL:
    """Stand-in for the ``mariadb`` client: maps a SQL substring to rows."""

    def __init__(self, tables: dict[str, list[str]], divergence: list[list[str]] | None = None):
        self.tables = tables
        self.divergence = divergence or []

    def __call__(self, cfg, sql: str, database: str | None = None) -> list[list[str]]:
        if "information_schema.tables" in sql.lower():
            db = sql.split("'")[1]
            return [[t] for t in sorted(self.tables.get(db, []))]
        if "information_schema.columns" in sql.lower():
            return [["a"], ["b"]]
        if "select (select count(*)" in sql.lower():
            return [["3", "3"]]
        return self.divergence


def test_table_set_violation_names_both_directions(monkeypatch):
    """Missing AND extra tables are both failures — a one-way check is not a check."""
    monkeypatch.setattr(
        restore_sql,
        "_run_sql",
        _FakeSQL({"src": ["config", "gone"], "dst": ["config", "surprise"]}),
    )
    outcome = restore_sql.check_table_set(object(), "src", "dst")
    assert outcome["status"] == STATUS_VIOLATION
    assert outcome["detail"]["missing"] == ["gone"]
    assert outcome["detail"]["extra"] == ["surprise"]


def test_table_set_ok_when_identical(monkeypatch):
    monkeypatch.setattr(restore_sql, "_run_sql", _FakeSQL({"src": ["config"], "dst": ["config"]}))
    assert restore_sql.check_table_set(object(), "src", "dst")["status"] == STATUS_OK


def test_row_identity_reports_divergent_digests(monkeypatch):
    """One row present source-side and absent target-side is the 06-16 shape."""
    monkeypatch.setattr(
        restore_sql,
        "_run_sql",
        _FakeSQL({"src": ["t"], "dst": ["t"]}, divergence=[["abc123", "1", "0"]]),
    )
    outcome = restore_sql.check_row_identity(object(), "src", "dst", ["t"])
    assert outcome["status"] == STATUS_VIOLATION
    assert "t" in outcome["detail"]["tables"]


def test_row_identity_ok_when_no_divergence(monkeypatch):
    monkeypatch.setattr(restore_sql, "_run_sql", _FakeSQL({"src": ["t"], "dst": ["t"]}))
    assert restore_sql.check_row_identity(object(), "src", "dst", ["t"])["status"] == STATUS_OK


def test_source_untouched_is_unavailable_without_a_before_reading(monkeypatch):
    """No baseline means the tripwire cannot assert — and unavailable blocks the op."""
    monkeypatch.setattr(restore_sql, "_run_sql", _FakeSQL({"src": ["t"]}))
    outcome = restore_sql.check_source_untouched(object(), "src", None)
    assert outcome["status"] == STATUS_UNAVAILABLE


def test_source_untouched_violation_when_the_source_moved(monkeypatch):
    """If the restore reached the LIVE schema this is the only thing that sees it."""
    monkeypatch.setattr(restore_sql, "_run_sql", _FakeSQL({"src": ["t"]}, divergence=[["9", "9"]]))
    before = {"t": ["1", "1"]}
    outcome = restore_sql.check_source_untouched(object(), "src", before)
    assert outcome["status"] == STATUS_VIOLATION


# ── the op refuses on anything that is not ok ────────────────────────────────


def test_op_raises_when_the_report_is_not_ok(tmp_path, monkeypatch):
    """Fails CLOSED. Car H tolerates unavailable; a restore gate must not."""
    monkeypatch.setenv("YADGAR_MARIADB_CLIENT_CNF", str(_write_cnf(tmp_path)))
    monkeypatch.setenv("YADGAR_SQL_BACKUP_DIR", str(tmp_path / "dumps"))
    (tmp_path / "dumps").mkdir()
    (tmp_path / "dumps" / "a.sql").write_text("CREATE TABLE `t` (`a` int);\n", encoding="utf-8")

    monkeypatch.setattr(restore_sql, "_client_binary", lambda: "/usr/bin/mariadb")
    monkeypatch.setattr(restore_sql, "_fingerprint", lambda cfg, db: {})
    monkeypatch.setattr(restore_sql, "_create_scratch", lambda cfg, db: None)
    monkeypatch.setattr(restore_sql, "_drop_scratch", lambda cfg, db: None)
    monkeypatch.setattr(restore_sql, "_replay_artifact", lambda cfg, path, db, src: 1)
    monkeypatch.setattr(
        restore_sql,
        "verify_restore",
        lambda *a, **k: {
            "status": STATUS_VIOLATION,
            "checks": {},
            "violations": ["row_identity: 1 row missing"],
            "unavailable": [],
        },
    )

    with pytest.raises(RestoreVerificationError) as excinfo:
        restore_sql.mariadb_restore_verify({"filename": "a.sql"})
    assert excinfo.value.report["status"] == STATUS_VIOLATION


def test_op_rejects_a_filename_that_walks_out_of_the_dump_directory(tmp_path, monkeypatch):
    """The payload carries a BASENAME (car F's rule); a path is an escape attempt."""
    monkeypatch.setenv("YADGAR_MARIADB_CLIENT_CNF", str(_write_cnf(tmp_path)))
    monkeypatch.setattr(restore_sql, "_client_binary", lambda: "/usr/bin/mariadb")
    with pytest.raises(RestoreVerificationError, match="basename"):
        restore_sql.mariadb_restore_verify({"filename": "../../etc/passwd"})


def test_op_is_unavailable_not_ok_when_the_client_binary_is_absent(tmp_path, monkeypatch):
    """A verification that CANNOT run reports unavailable and still refuses."""
    monkeypatch.setenv("YADGAR_MARIADB_CLIENT_CNF", str(_write_cnf(tmp_path)))
    monkeypatch.setattr(restore_sql, "_client_binary", lambda: None)
    with pytest.raises(RestoreVerificationError) as excinfo:
        restore_sql.mariadb_restore_verify({"filename": "a.sql"})
    assert excinfo.value.report["status"] == STATUS_UNAVAILABLE
