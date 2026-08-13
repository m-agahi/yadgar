"""The cross-engine ``check_invariants`` arm against a REAL MariaDB (car H).

Marked ``integration`` — the default addopts exclude it
(``-m 'not integration and not e2e'``). Run explicitly::

    pytest yadgar/tests/integration/test_cross_engine_invariants.py -m integration -v

WHAT ONLY A LIVE SERVER CAN SETTLE
----------------------------------
The offline half (``yadgar/tests/backend/test_cross_engine_invariants.py``) pins
every branch through stubs, which is enough for logic. Three things it cannot
reach, and all three are the ones that would let the arm pass vacuously in
production:

  * that the stamped-revision read works against a real ``alembic_version`` — the
    stub returns whatever it is told, so "at head" has never been read from a
    server;
  * that ``count_rows`` sees the SAME database the migration wrote to. This is
    the whole point of an EXACT count: ``config_list()`` returned ``[]`` before
    this train existed and cannot tell the engines apart, so the assertion is
    only worth anything engine-direct (ADR-0203, exit criterion 1);
  * that an INJECTED violation is actually caught end-to-end. A test that only
    ever observes the healthy state is the vacuous pass one level up, so each
    assertion here is proven RED by damaging real server state — a bogus
    ``alembic_version`` stamp, a real ``config`` row, a dropped table.

The container fixture is car C/D's, duplicated rather than shared: extracting it
to a conftest would put a refactor of two landed cars in this diff for no gain.
``xdist_group`` keeps the three files off each other's servers.
"""

from __future__ import annotations

import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy", reason="sqlalchemy not installed (sql extra)")
pytest.importorskip("alembic", reason="alembic not installed (sql extra)")

from yadgar._shared.storage.sql import (
    MariaStorageEngine,  # noqa: E402
    migrate,  # noqa: E402
)
from yadgar._shared.storage.sql.migrate import upgrade_to_head  # noqa: E402
from yadgar.backend.admin_exec import invariants_cross_engine as ce  # noqa: E402
from yadgar.tests.integration._podman import (  # noqa: E402
    container_is_running,
    container_logs,
    make_socket_dir,
    podman_env,
    remove_container_dir,
    select_container_runtime,
)

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("engine2_mariadb")]

_IMAGE = "docker.io/library/mariadb:11.4"
_DB = "yadgar"
_APP_USER = "yadgar_app"
_APP_PASS = "carh-integration-password"
_BOOT_TIMEOUT_SEC = 180.0
# The socket file is the FIRST thing mysqld creates; if it has not reached our
# side of the mount by here, waiting out the full boot timeout cannot help.
_MOUNT_VISIBLE_TIMEOUT_SEC = 30.0


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


@pytest.fixture(scope="module")
def live_mariadb():
    """Scratch MariaDB over a unix socket, torn down with its anonymous volume.

    Runtime selection PROBES rather than checks presence (car G5) and the
    socket directory comes from ``shared_mount_root`` rather than ``/tmp``
    (car G6) — this file is not in the CI job's file list, so it carried both
    defects latently after its three siblings were fixed.
    """
    runtime = select_container_runtime()
    if runtime is None:
        pytest.skip(
            "no working container runtime on this host "
            "(podman/docker absent, or present but non-functional)"
        )

    name = f"yadgar-carh-mdb-{uuid.uuid4().hex[:8]}"
    sock_dir = make_socket_dir(runtime, image=_IMAGE, prefix="ymdbh")
    socket_path = sock_dir / "mysqld.sock"

    started = subprocess.run(
        [
            runtime, "run", "-d", "--name", name,
            "--memory", "512m", "--cpus", "1",
            "-e", "MARIADB_ROOT_PASSWORD=carh-root",
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

    cnf = sock_dir / "client.cnf"
    cnf.write_text(_cnf_body(str(socket_path)), encoding="utf-8")
    cnf.chmod(0o600)

    try:
        _await_ready(cnf, runtime, name, socket_path)
        yield {"cnf": cnf}
    finally:
        subprocess.run(
            [runtime, "rm", "-f", "-v", name],
            capture_output=True, check=False, timeout=120, env=podman_env(),
        )  # fmt: skip
        remove_container_dir(runtime, sock_dir, image=_IMAGE)


def _await_ready(cnf: Path, runtime: str, name: str, socket_path: Path) -> None:
    """The socket appears before the server is usable (bootstrap server first).

    Exits early on a dead container or a socket that never crosses the mount,
    rather than retrying for the full timeout against something that cannot
    start answering (car G6).
    """
    import asyncio

    async def _probe() -> None:
        deadline = time.monotonic() + _BOOT_TIMEOUT_SEC
        mount_deadline = time.monotonic() + _MOUNT_VISIBLE_TIMEOUT_SEC
        last: Exception | None = None
        while time.monotonic() < deadline:
            if not container_is_running(runtime, name):
                raise AssertionError(
                    f"the MariaDB container {name} exited during boot; "
                    f"last logs:\n{container_logs(runtime, name)}"
                )
            if not socket_path.exists() and time.monotonic() > mount_deadline:
                raise AssertionError(
                    f"{socket_path} never appeared on this side of the mount "
                    f"within {_MOUNT_VISIBLE_TIMEOUT_SEC}s while the container was "
                    "still running — the bind mount is not shared with the "
                    f"{Path(runtime).name} daemon. Set "
                    "YADGAR_TEST_SHARED_MOUNT_ROOT to a directory both sides see."
                )
            engine = MariaStorageEngine.from_option_file(cnf)
            try:
                await engine.verify()
                return
            except Exception as exc:  # noqa: BLE001 — boot race, retry
                last = exc
                await asyncio.sleep(1.0)
            finally:
                await engine.dispose()
        raise AssertionError(f"MariaDB not ready within {_BOOT_TIMEOUT_SEC}s: {last}")

    asyncio.run(_probe())


@pytest.fixture
async def engine(live_mariadb):
    eng = MariaStorageEngine.from_option_file(live_mariadb["cnf"])
    try:
        yield eng
    finally:
        await eng.dispose()


async def _reset_to_base(engine: MariaStorageEngine) -> None:
    """Undo every migration — leave a database with NO yadgar tables at all.

    Deliberately NOT ``alembic downgrade base``: that walks the STAMPED
    revision backwards, and ``test_stamp_that_disagrees_with_the_chain_is_a_violation``
    deliberately corrupts ``alembic_version`` to prove the head-mismatch check
    red — so alembic can no longer find its own way back
    (``CommandError: Can't locate revision identified by '0000_stale'``) and
    a downgrade-by-stamp teardown dies right there, taking every test after
    it down with a fixture-setup error on top of the one teardown error.
    Measured empirically running this file end to end.

    Instead: enumerate whatever tables the schema ACTUALLY has (real
    catalog state, not the possibly-corrupted stamp) and drop every one of
    them with FK checks off. This is what the fixture's PREVIOUS version got
    wrong — it hardcoded ``DROP TABLE config`` / ``DROP TABLE
    alembic_version``, which was complete when the chain had one migration
    and silently fell behind the moment ``002_ledger_tables`` added five more
    tables (``task``, ``adr``, ``agent_pattern``, ``agent_discipline``,
    ``task_blocked_by``, ``adr_supersedes``, ``agent_pattern_composes``) plus
    ``003``/``004`` added three more still. Those survived teardown, and the
    NEXT test's ``upgrade_to_head`` died on ``1050 Table 'task' already
    exists`` (docs/issues/0001-pr40-carryover.md §2 item 3; measured by car
    G6 as ``1 failed, 1 passed, 6 errors`` — the 6 errors are every test
    after the first that needs a second ``migrated`` fixture instantiation).
    Catalog-driven enumeration self-updates with the migration chain instead
    of drifting from it again the next time a migration adds a table.
    """
    from sqlalchemy import text  # noqa: PLC0415

    tables = await engine.list_tables()
    if not tables:
        return
    async with engine.engine.begin() as conn:
        await conn.execute(text("SET FOREIGN_KEY_CHECKS=0"))
        for table in tables:
            # table names come straight from information_schema.tables — DB
            # catalog state, not user input.
            await conn.execute(text(f"DROP TABLE IF EXISTS `{table}`"))
        await conn.execute(text("SET FOREIGN_KEY_CHECKS=1"))


@pytest.fixture
async def migrated(engine):
    """An engine at head — the state the backend boots into."""
    await upgrade_to_head(engine.engine)
    try:
        yield engine
    finally:
        await _reset_to_base(engine)


async def _exec(engine: MariaStorageEngine, sql: str) -> None:
    from sqlalchemy import text  # noqa: PLC0415

    async with engine.engine.begin() as conn:
        await conn.execute(text(sql))


class _SurrealAtHead:
    """SurrealDB stand-in reporting its own chain at head — not under test here."""

    def _q(self, _surql: str, _params: dict | None = None) -> list:
        from yadgar._shared.storage.migrations import _MIGRATIONS  # noqa: PLC0415

        return [{"version": m["version"]} for m in _MIGRATIONS]


async def _arm(monkeypatch: pytest.MonkeyPatch, engine) -> dict:
    monkeypatch.setattr(ce, "_get_sql_engine", lambda: engine)
    return await ce.run_cross_engine_checks(_SurrealAtHead())


def _expected_head() -> str:
    """The alembic chain's real head, read live rather than hardcoded.

    A literal revision id in a test goes stale the moment a new migration
    lands — it did: this repo's head moved from ``0001_config`` to
    ``004_agent_pattern_model_client`` without these assertions being
    touched, and the chain will move again. Asking the same script directory
    the production code asks keeps the assertion tied to reality instead.
    """
    return migrate.heads()[0]


# ── the healthy state, read from a real server ───────────────────────────────


async def test_at_head_and_empty_is_ok_with_engine_direct_evidence(
    migrated, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both engine-#2 checks pass, and the report SHOWS the values compared."""
    result = await _arm(monkeypatch, migrated)

    head = result["checks"][ce.CHECK_ENGINE_TWO_SCHEMA_HEAD]
    assert head["status"] == ce.STATUS_OK, head
    assert head["detail"]["current"] == head["detail"]["head"] == _expected_head()

    baseline = result["checks"][ce.CHECK_CONFIG_ROW_BASELINE]
    assert baseline["status"] == ce.STATUS_OK, baseline
    assert baseline["detail"] == {"rows": 0, "expected": 0}

    # The spine ledger tables (agent_pattern, agent_discipline) now exist —
    # migrations 002-004 shipped since this test was first written, so
    # REASON_SPINE_NOT_SHIPPED no longer applies — the desync check actually
    # RUNS: both tables are present and empty, nothing to compare, ok.
    desync = result["checks"][ce.CHECK_PAGE_ROW_DESYNC]
    assert desync["status"] == ce.STATUS_OK, desync
    assert desync["detail"]["compared"] == 0

    # Still not globally ok: the SurrealDB stand-in used here (_SurrealAtHead)
    # carries no `_db_url`, so check_surreal_schema_head reports UNAVAILABLE
    # (embedded mode) — "cannot run" must never be reported as ok.
    assert result["status"] == ce.STATUS_UNAVAILABLE
    assert result["violations"] == []


# ── injected violations, proven RED against real server state ────────────────


async def test_unmigrated_database_is_a_violation(engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """A server that alembic never touched: no stamp, no config table."""
    result = await _arm(monkeypatch, engine)

    head = result["checks"][ce.CHECK_ENGINE_TWO_SCHEMA_HEAD]
    assert head["status"] == ce.STATUS_VIOLATION
    assert head["detail"]["current"] is None
    # No stamp AND no table is honest absence, not a contradiction.
    assert result["checks"][ce.CHECK_CONFIG_ROW_BASELINE]["status"] == ce.STATUS_UNAVAILABLE
    assert result["status"] == ce.STATUS_VIOLATION


async def test_stamp_that_disagrees_with_the_chain_is_a_violation(
    migrated, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Damage the real ``alembic_version`` row — the arm must catch it."""
    await _exec(migrated, "UPDATE alembic_version SET version_num = '0000_stale'")
    result = await _arm(monkeypatch, migrated)

    head = result["checks"][ce.CHECK_ENGINE_TWO_SCHEMA_HEAD]
    assert head["status"] == ce.STATUS_VIOLATION
    assert head["detail"] == {"current": "0000_stale", "head": _expected_head()}
    assert any(ce.CHECK_ENGINE_TWO_SCHEMA_HEAD in v for v in result["violations"])


async def test_an_unexpected_config_row_is_a_violation(
    migrated, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0203: this train ships config EMPTY, so one real row is a real signal."""
    await _exec(
        migrated,
        "INSERT INTO config (`key`, value, default_value) VALUES ('carh.probe', '\"x\"', '\"x\"')",
    )
    result = await _arm(monkeypatch, migrated)

    baseline = result["checks"][ce.CHECK_CONFIG_ROW_BASELINE]
    assert baseline["status"] == ce.STATUS_VIOLATION
    assert baseline["detail"] == {"rows": 1, "expected": 0}
    assert result["status"] == ce.STATUS_VIOLATION


async def test_a_missing_seed_is_a_violation_too_not_a_ge_pass(
    migrated, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 2026-06-16 shape: fewer rows than declared must NOT pass.

    A partial restore (1,484 of 3,622) satisfied a ``>=`` check and 3,622
    memories were destroyed. Raising the declared baseline simulates the knob
    train having seeded, with the seed only half-landed.
    """
    monkeypatch.setattr(ce, "EXPECTED_CONFIG_ROWS", 2)
    await _exec(
        migrated,
        "INSERT INTO config (`key`, value, default_value) "
        "VALUES ('carh.only_one', '\"x\"', '\"x\"')",
    )
    baseline = (await _arm(monkeypatch, migrated))["checks"][ce.CHECK_CONFIG_ROW_BASELINE]

    assert baseline["status"] == ce.STATUS_VIOLATION
    assert baseline["detail"] == {"rows": 1, "expected": 2}


async def test_stamped_at_head_with_the_table_gone_is_a_violation(
    migrated, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Head CREATES config. Head-plus-no-table is two checks disagreeing."""
    await _exec(migrated, "DROP TABLE config")
    baseline = (await _arm(monkeypatch, migrated))["checks"][ce.CHECK_CONFIG_ROW_BASELINE]

    assert baseline["status"] == ce.STATUS_VIOLATION
    assert "head" in baseline["detail"]["message"]


# ── the engine-direct count is real, and its identifier guard holds ──────────


async def test_count_rows_reads_the_same_database_the_migration_wrote(migrated) -> None:
    await _exec(
        migrated,
        "INSERT INTO config (`key`, value, default_value) VALUES ('a', '1', '1'), ('b', '2', '2')",
    )
    assert await migrated.count_rows("config") == 2


async def test_count_rows_rejects_a_non_identifier(migrated) -> None:
    with pytest.raises(ValueError, match="bare SQL identifier"):
        await migrated.count_rows("config; DROP TABLE config")
