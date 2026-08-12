"""Engine-#2 car G: restore verification against a REAL MariaDB — exit criterion 4.

Marked ``integration`` — the default addopts exclude it
(``-m 'not integration and not e2e'``). Run explicitly::

    pytest yadgar/tests/integration/test_mariadb_restore_arm.py -m integration -v

WHY THE FIXTURE TABLE HAS ROWS
------------------------------
``config`` ships EMPTY (ADR-0203), and an enumeration over an empty table
compares ``{}`` to ``{}`` — which passes whether or not the comparison works at
all. Every partial-restore shape below except *drop table* would be unprovable
against it. So the fixture creates ``fixture_rows`` WITH rows and the shapes run
against that; ``config`` rides along to prove the zero-row table is handled too.
This is exactly the vacuous-pass class the arm exists to prevent, one layer
down: a suite that only ever sees empty tables cannot tell a working comparison
from a deleted one.

WHY THE SHAPES MUTATE THE SOURCE RATHER THAN THE ARTIFACT
--------------------------------------------------------
A dump is taken, then the LIVE schema is moved, then the (now stale) artifact is
restored and enumerated. Every divergence direction is reachable that way and
none of it depends on hand-editing SQL text, which would test the editor rather
than the arm:

* source GAINS rows      -> the restore is MISSING them. The literal 2026-06-16
                            direction (1,484 of 3,622 present).
* source LOSES rows      -> the restore has EXTRA. A ``>=`` check PASSES this;
                            the enumeration does not.
* source row UPDATED     -> identical counts, different content. Invisible to
                            every count-based check that has ever been written.
* a table appears/vanishes, a column changes -> the coarser shapes.

The one artifact-level shape that is NOT reachable that way is a leak: a
statement that names the LIVE schema explicitly. That one is written into the
artifact on purpose, because the belt it exercises exists for precisely the case
where the ``USE``/``CREATE DATABASE`` filter did not catch something.

The scratch container never touches the live data root at ``~/.local/share/yadgar``.
"""

from __future__ import annotations

import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from yadgar.backend.admin_exec import backup_sql, restore_sql
from yadgar.backend.admin_exec.restore_sql import RestoreVerificationError
from yadgar.tests.integration._podman import podman_env

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("engine2_mariadb")]

_IMAGE = "docker.io/library/mariadb:11.4"
_DB = "yadgar"
_APP_USER = "yadgar_app"
_APP_PASS = "carg-integration-password"
_ROOT_PASS = "carg-root"
_BOOT_TIMEOUT_SEC = 180.0

# entrypoint-backend.sh's car-G grant, with ONE deliberate difference: the host
# part is `@'%'` because that is how the mariadb image creates its MARIADB_USER,
# where the entrypoint uses `@'localhost'`. The DATABASE pattern — the half worth
# testing — is identical, including the escaped `_`, which is a LIKE wildcard in
# a grant's database position and would otherwise match far more than intended.
# Running the real statement is what proves the production grant is valid syntax
# and actually confers CREATE/DROP on the scratch schema; every scratch-schema
# assertion below fails without it.
_SCRATCH_GRANT = (
    f"GRANT ALL PRIVILEGES ON `{_DB}\\_restorecheck\\_%`.* TO '{_APP_USER}'@'%'; FLUSH PRIVILEGES;"
)

_FIXTURE_TABLE = "fixture_rows"


def _cnf_body(socket: str) -> str:
    return (
        "\n".join(
            [
                "[client]",
                f"socket = {socket}",
                f"user = {_APP_USER}",
                f"password = {_APP_PASS}",
                f"database = {_DB}",
            ]
        )
        + "\n"
    )


def _as_root(runtime: str, name: str, sql: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            runtime, "exec", name, "mariadb", "--socket=/sockets/mysqld.sock",
            "-uroot", f"-p{_ROOT_PASS}", "-e", sql,
        ],
        capture_output=True, text=True, check=False, timeout=60, env=podman_env(),
    )  # fmt: skip


def _as_app(runtime: str, name: str, sql: str) -> subprocess.CompletedProcess[str]:
    """One statement as the APP user — the same account the arm itself uses.

    Deliberately not root as the readiness probe: the image answers ping on a
    temporary server BEFORE the accounts exist, so only a successful app
    connection implies the account and the database are really in place (the
    flake car F hit).

    Only ever called from the MODULE fixture. Anything a TEST runs must go
    through ``_host_sql`` instead — see its docstring.
    """
    return subprocess.run(
        [
            runtime, "exec", name, "mariadb", "--socket=/sockets/mysqld.sock",
            f"-u{_APP_USER}", f"-p{_APP_PASS}", _DB, "-e", sql,
        ],
        capture_output=True, text=True, check=False, timeout=60, env=podman_env(),
    )  # fmt: skip


def _host_sql(cnf: Path, sql: str) -> subprocess.CompletedProcess[str]:
    """Run SQL from the HOST over the bind-mounted socket, not via ``podman exec``.

    NOT a style preference. ``yadgar/tests/conftest.py`` redirects ``HOME`` and
    ``XDG_DATA_HOME`` per test (the #64 hook-isolation guard), and rootless
    podman keeps its container store under ``XDG_DATA_HOME``. A ``podman exec``
    issued from a FUNCTION-scoped fixture therefore looks in a different store
    than the module fixture used and answers "no such container" for a container
    that is running perfectly well. Car F never hit this because every one of its
    podman calls lives in the module fixture.

    Going through the host client also means the tests reach the server exactly
    the way ``restore_sql`` does — same binary, same option file, same socket.
    """
    return subprocess.run(
        [
            "mariadb", f"--defaults-file={cnf}", "-e", sql,
        ],
        capture_output=True, text=True, check=False, timeout=60,
    )  # fmt: skip


def _await_ready(runtime: str, name: str) -> None:
    deadline = time.monotonic() + _BOOT_TIMEOUT_SEC
    last = ""
    while time.monotonic() < deadline:
        probe = _as_app(runtime, name, "SELECT 1")
        if probe.returncode == 0:
            return
        last = (probe.stderr or probe.stdout).strip()
        time.sleep(2.0)
    pytest.fail(f"MariaDB never became ready within {_BOOT_TIMEOUT_SEC}s: {last}")


def _remove_socket_dir(runtime: str, sock_dir: Path) -> None:
    """Delete the mount dir, including what the container's uid took ownership of."""
    shutil.rmtree(sock_dir, ignore_errors=True)
    if sock_dir.exists() and Path(runtime).name == "podman":
        subprocess.run(
            [runtime, "unshare", "rm", "-rf", str(sock_dir)],
            capture_output=True, check=False, timeout=60, env=podman_env(),
        )  # fmt: skip


@pytest.fixture(scope="module")
def live_mariadb():
    """Scratch MariaDB with a ZERO-row table and a table that HAS rows; torn down after.

    NOT ``tmp_path``: a unix socket path caps at ~107 bytes and pytest's tmp
    dirs are long enough to blow it.
    """
    runtime = shutil.which("podman") or shutil.which("docker")
    if runtime is None:
        pytest.skip("docker/podman not available on this host")
    if shutil.which("mariadb-dump") is None:
        pytest.skip("mariadb-dump not available on this host")
    if shutil.which("mariadb") is None:
        pytest.skip("mariadb client not available on this host")

    name = f"yadgar-carg-mdb-{uuid.uuid4().hex[:8]}"
    sock_dir = Path(f"/tmp/ymdbg-{uuid.uuid4().hex[:8]}")
    sock_dir.mkdir(mode=0o777, parents=True)
    sock_dir.chmod(0o777)  # mkdir mode is umask-masked
    socket_path = sock_dir / "mysqld.sock"

    started = subprocess.run(
        [
            runtime, "run", "-d", "--name", name,
            "--memory", "512m", "--cpus", "1",
            "-e", f"MARIADB_ROOT_PASSWORD={_ROOT_PASS}",
            "-e", f"MARIADB_DATABASE={_DB}",
            "-e", f"MARIADB_USER={_APP_USER}",
            "-e", f"MARIADB_PASSWORD={_APP_PASS}",
            "-v", f"{sock_dir}:/sockets:Z",
            _IMAGE,
            "--socket=/sockets/mysqld.sock",
        ],
        capture_output=True, text=True, check=False, timeout=300, env=podman_env(),
    )  # fmt: skip
    if started.returncode != 0:
        shutil.rmtree(sock_dir, ignore_errors=True)
        pytest.skip(f"could not start MariaDB container: {started.stderr.strip()}")

    try:
        _await_ready(runtime, name)
        granted = _as_root(runtime, name, _SCRATCH_GRANT)
        assert granted.returncode == 0, granted.stderr
        created = _as_app(
            runtime,
            name,
            "CREATE TABLE config (id BIGINT AUTO_INCREMENT PRIMARY KEY, k VARCHAR(255));"
            f"CREATE TABLE {_FIXTURE_TABLE} ("
            "  id BIGINT AUTO_INCREMENT PRIMARY KEY,"
            "  label VARCHAR(64) NOT NULL,"
            "  payload TEXT NULL);",
        )
        assert created.returncode == 0, created.stderr
        cnf = sock_dir / "client.cnf"
        cnf.write_text(_cnf_body(str(socket_path)), encoding="utf-8")
        cnf.chmod(0o600)
        yield {"cnf": cnf, "dir": sock_dir, "runtime": runtime, "name": name}
    finally:
        # -v: the image declares /var/lib/mysql a VOLUME, so every run creates an
        # anonymous one. Without this each run leaks a datadir-sized volume.
        subprocess.run(
            [runtime, "rm", "-f", "-v", name],
            capture_output=True, check=False, timeout=120, env=podman_env(),
        )  # fmt: skip
        _remove_socket_dir(runtime, sock_dir)


@pytest.fixture
def arm(live_mariadb, tmp_path, monkeypatch):
    """Point the dump + restore ops at the scratch server, with rows in place.

    Function-scoped so every shape starts from the SAME corpus: the table is
    rebuilt from scratch each time, which is what lets one test mutate the source
    without leaking that mutation into the next.
    """
    cnf = live_mariadb["cnf"]
    monkeypatch.setenv("YADGAR_MARIADB_CLIENT_CNF", str(cnf))
    monkeypatch.setenv("YADGAR_SQL_BACKUP_DIR", str(tmp_path / "dumps"))

    reset = _host_sql(
        cnf,
        f"DROP TABLE IF EXISTS {_FIXTURE_TABLE};"
        f"CREATE TABLE {_FIXTURE_TABLE} ("
        "  id BIGINT AUTO_INCREMENT PRIMARY KEY,"
        "  label VARCHAR(64) NOT NULL,"
        # TWO nullable columns, on purpose. CONCAT_WS SKIPS a NULL rather than
        # emitting a placeholder, so with one nullable column every NULL/value
        # difference still shifts the separator count and any digest catches it.
        # With two, ('x', NULL) and (NULL, 'x') collapse to the SAME pre-image
        # unless NULL is encoded explicitly — which is the only shape that proves
        # the sentinel is load-bearing.
        "  payload TEXT NULL,"
        "  note VARCHAR(32) NULL);"
        f"INSERT INTO {_FIXTURE_TABLE} (label, payload, note) VALUES "
        "('alpha','one',NULL),('beta','two','n2'),('gamma',NULL,NULL),"
        "('delta','four','n4'),('epsilon','five',NULL);",
    )
    assert reset.returncode == 0, reset.stderr
    return {
        "dumps": tmp_path / "dumps",
        "sql": lambda stmt: _host_sql(cnf, stmt),
    }


def _dump(arm) -> str:
    """Take an artifact through car F's real op and return its basename."""
    result = backup_sql.mariadb_dump({"label": "carg"})
    assert result["ok"] is True
    return str(result["filename"])


# ── the happy path, with POSITIVE evidence ──────────────────────────────────


def test_full_restore_round_trip_verifies_clean(arm):
    """Dump -> replay into a scratch schema -> enumerate -> ok, over REAL rows.

    Asserts the compared row counts, not just the status. An ``ok`` with no
    numbers behind it cannot be told apart from a comparison that never ran —
    which is the failure this arm's whole design is aimed at.
    """
    report = restore_sql.mariadb_restore_verify({"filename": _dump(arm)})

    assert report["ok"] is True
    assert report["status"] == "ok"
    assert report["violations"] == []
    assert report["unavailable"] == []
    counts = report["checks"]["row_identity"]["detail"]["counts"]
    assert counts[_FIXTURE_TABLE] == {"source": 5, "restored": 5}
    assert counts["config"] == {"source": 0, "restored": 0}
    # The scratch schema is transient by contract — nothing is left behind.
    assert restore_sql.SCRATCH_INFIX in report["scratch_database"]


def test_the_scratch_schema_is_dropped_afterwards(arm):
    """A verification run must not accumulate schemas on the live server."""
    report = restore_sql.mariadb_restore_verify({"filename": _dump(arm)})
    survivors = arm["sql"](
        "SELECT SCHEMA_NAME FROM information_schema.schemata WHERE SCHEMA_NAME LIKE "
        f"'{_DB}\\_restorecheck\\_%'"
    )
    assert report["scratch_database"] not in survivors.stdout


# ── exit criterion 4: PARTIAL restores are REJECTED ─────────────────────────


def _expect_rejection(filename: str) -> RestoreVerificationError:
    with pytest.raises(RestoreVerificationError) as excinfo:
        restore_sql.mariadb_restore_verify({"filename": filename})
    assert excinfo.value.report["status"] != "ok"
    return excinfo.value


def test_partial_restore_missing_rows_is_rejected(arm):
    """THE 2026-06-16 shape: the restored corpus holds FEWER rows than the source.

    A ``count(restored) >= count(expected)`` check passes 1,484 of 3,622. This
    one does not, because it compares which rows, not how many.
    """
    dump = _dump(arm)
    arm["sql"](f"INSERT INTO {_FIXTURE_TABLE} (label,payload) VALUES ('zeta','six'),('eta','7')")

    error = _expect_rejection(dump)
    detail = error.report["checks"]["row_identity"]["detail"]
    assert _FIXTURE_TABLE in detail["tables"]
    assert detail["counts"][_FIXTURE_TABLE] == {"source": 7, "restored": 5}
    # Both missing rows are named individually — enumeration, not a delta.
    assert len(detail["tables"][_FIXTURE_TABLE]) == 2
    assert all(d["restored_rows"] == 0 for d in detail["tables"][_FIXTURE_TABLE])


def test_restore_with_extra_rows_is_rejected(arm):
    """The direction a one-sided check cannot see: the restore has MORE than the source.

    ``>=`` passes this outright. It is the shape of restoring a stale artifact
    over a corpus rows have since been deleted from.
    """
    dump = _dump(arm)
    arm["sql"](f"DELETE FROM {_FIXTURE_TABLE} WHERE label IN ('alpha','beta')")

    error = _expect_rejection(dump)
    detail = error.report["checks"]["row_identity"]["detail"]
    assert detail["counts"][_FIXTURE_TABLE] == {"source": 3, "restored": 5}
    assert all(d["source_rows"] == 0 for d in detail["tables"][_FIXTURE_TABLE])


def test_restore_of_mutated_rows_is_rejected_despite_identical_counts(arm):
    """Same row count, different content — invisible to EVERY count-based check.

    This is the test that proves the arm compares identity rather than volume.
    """
    dump = _dump(arm)
    arm["sql"](f"UPDATE {_FIXTURE_TABLE} SET payload='TAMPERED' WHERE label='gamma'")

    error = _expect_rejection(dump)
    detail = error.report["checks"]["row_identity"]["detail"]
    assert detail["counts"][_FIXTURE_TABLE] == {"source": 5, "restored": 5}
    assert detail["tables"][_FIXTURE_TABLE], "a mutated row must be named"


def test_restore_of_a_null_transposition_is_rejected(arm):
    """A value moved BETWEEN two nullable columns must not digest the same.

    ``('one', NULL)`` and ``(NULL, 'one')`` are different rows that CONCAT_WS
    reduces to the same pre-image, because it drops a NULL together with its
    separator. Only the explicit NULL sentinel keeps them apart. Deleting the
    sentinel makes THIS test — and only this test — go green-when-it-should-be-red,
    which is how it was found: the earlier NULL-vs-''-in-the-last-column version
    passed with the sentinel removed and therefore proved nothing.
    """
    dump = _dump(arm)
    arm["sql"](f"UPDATE {_FIXTURE_TABLE} SET payload=NULL, note='one' WHERE label='alpha'")

    error = _expect_rejection(dump)
    assert error.report["checks"]["row_identity"]["status"] == "violation"
    detail = error.report["checks"]["row_identity"]["detail"]
    # Counts are identical on both sides — only identity separates them.
    assert detail["counts"][_FIXTURE_TABLE] == {"source": 5, "restored": 5}


def test_restore_missing_a_whole_table_is_rejected(arm):
    """A table the source has and the artifact predates — the coarsest partial."""
    dump = _dump(arm)
    arm["sql"]("CREATE TABLE appeared_later (id BIGINT PRIMARY KEY)")
    try:
        error = _expect_rejection(dump)
        assert error.report["checks"]["table_set"]["detail"]["missing"] == ["appeared_later"]
    finally:
        arm["sql"]("DROP TABLE IF EXISTS appeared_later")


def test_restore_carrying_a_table_the_source_dropped_is_rejected(arm):
    """The other direction of the table set — an artifact holding a since-dropped table."""
    arm["sql"]("CREATE TABLE doomed (id BIGINT PRIMARY KEY)")
    dump = _dump(arm)
    arm["sql"]("DROP TABLE doomed")

    error = _expect_rejection(dump)
    assert error.report["checks"]["table_set"]["detail"]["extra"] == ["doomed"]


def test_restore_with_a_changed_column_list_is_rejected(arm):
    """A shape mismatch reports as a VIOLATION, not as an unavailable query error.

    Without the column precondition the row query would ERROR on the missing
    column and the generic handler would call that ``unavailable`` — an honest
    status for the wrong reason, and one an operator would read as flaky.
    """
    dump = _dump(arm)
    arm["sql"](f"ALTER TABLE {_FIXTURE_TABLE} ADD COLUMN extra_col INT NULL")

    error = _expect_rejection(dump)
    assert error.report["checks"]["column_sets"]["status"] == "violation"
    assert _FIXTURE_TABLE in error.report["checks"]["column_sets"]["detail"]["mismatches"]


def test_a_truncated_artifact_is_rejected(arm):
    """A dump cut off mid-file must not restore quietly as 'most of the data'."""
    dump = _dump(arm)
    path = arm["dumps"] / dump
    body = path.read_text(encoding="utf-8")
    path.write_text(body[: len(body) // 2], encoding="utf-8")

    error = _expect_rejection(dump)
    assert error.report["status"] != "ok"


# ── the second belt: a leak that reaches the LIVE schema ────────────────────


def test_an_artifact_naming_another_database_in_a_use_is_refused(arm):
    """The filter's own half: a redirect is LOUD, never silently dropped."""
    dump = _dump(arm)
    path = arm["dumps"] / dump
    path.write_text(path.read_text(encoding="utf-8") + "\nUSE `mysql`;\n", encoding="utf-8")

    error = _expect_rejection(dump)
    assert error.report["status"] != "ok"


def test_a_statement_that_writes_the_live_schema_is_caught_by_the_source_belt(arm):
    """The belt for the case the FILTER misses.

    A fully-qualified INSERT names the live schema without any ``USE`` at all, so
    the statement filter passes it through by construction. The pre/post
    fingerprint is what notices, which is the whole point of having a second belt
    that does not care WHY the first one leaked.
    """
    dump = _dump(arm)
    path = arm["dumps"] / dump
    path.write_text(
        path.read_text(encoding="utf-8")
        + f"\nINSERT INTO `{_DB}`.`{_FIXTURE_TABLE}` (label,payload) VALUES ('LEAK','x');\n",
        encoding="utf-8",
    )

    error = _expect_rejection(dump)
    assert error.report["checks"]["source_untouched"]["status"] == "violation"
    assert _FIXTURE_TABLE in error.report["checks"]["source_untouched"]["detail"]["changed"]


# ── tri-state: what cannot be checked is never ok ───────────────────────────


def test_an_absent_artifact_is_unavailable_and_still_refused(arm):
    """UNAVAILABLE is a refusal here, unlike car H's reporting arm."""
    error = _expect_rejection("mariadb.yadgar.does-not-exist.sql")
    assert error.report["status"] == "unavailable"
