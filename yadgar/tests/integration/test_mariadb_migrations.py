"""Engine-#2 Alembic chain against a REAL MariaDB, under PRODUCTION's privileges.

Marked ``integration`` — the default addopts exclude it
(``-m 'not integration and not e2e'``). Run explicitly::

    pytest yadgar/tests/integration/test_mariadb_migrations.py -m integration -v

WHY THIS FILE WAS GREEN WHILE PRODUCTION COULD NOT WORK
-------------------------------------------------------
It used to provision its user by handing the stock image
``MARIADB_USER=yadgar_app`` / ``MARIADB_PASSWORD=…``, which grants that user
**ALL PRIVILEGES** on the database. Production's ``yadgar_app`` is the opposite:
``entrypoint-backend.sh`` REVOKEs everything and re-grants a per-table
SELECT/INSERT/UPDATE/DELETE/REFERENCES list with no CREATE, ALTER, INDEX or
DROP (D19). Same username, opposite privileges — so every assertion here passed
against a privilege set the deployed system has never had, and two failures
rode through:

  * ``alembic upgrade head`` as the app account dies on 002's first
    ``op.create_table`` with ``(1142, "CREATE command denied to user
    'yadgar_app'@'localhost' for table `yadgar`.`task`")``;
  * MariaDB REJECTS a table-level ``GRANT`` on a table that does not exist
    (``ERROR 1146 (42S02)``) and the entrypoint's heredoc runs without
    ``--force``, so the FIRST such statement aborted every one after it. On a
    fresh install that was ``alembic_version`` — the app account was left with
    USAGE and nothing else; on a long-lived host ``alembic_version`` and
    ``config`` already existed, so the abort landed at ``task`` and the ledger
    tables were never granted OR created.

So the fixture no longer invents a user. It replays ``entrypoint-backend.sh``'s
OWN bootstrap — the two heredocs read straight out of the shipped file by
``yadgar/tests/_entrypoint_sql.py``, with the Alembic chain between them, in
that order. A second hardcoded copy of the grant list would drift, and a
drifted copy is what this file is here to stop being.

WHAT ONLY A LIVE SERVER CAN SETTLE
----------------------------------
The offline half (``yadgar/tests/_shared/test_mariadb_migrations.py``) pins the
rendered DDL, and ``_shared/test_entrypoint_grants.py`` pins the grant text.
Neither can reach:

  * that the privilege set can CREATE what it grants — a text assertion sees a
    complete-looking list either way;
  * that MariaDB accepts the DDL at all;
  * that the run is IDEMPOTENT — really the question of whether alembic
    COMMITTED ``alembic_version``. MariaDB has no transactional DDL, so alembic
    commits one transaction per migration; if that did not reach disk, a second
    ``upgrade head`` re-runs the revision and dies on "table already exists";
  * that ``config`` really has ZERO rows (ADR-0203) — asserted ENGINE-DIRECT
    with ``SELECT COUNT(*)``, because exit criterion 1 says ``config_list() == []``
    is not evidence: it returned ``[]`` before this train and cannot tell the
    engines apart.

The container fixture is car C's, duplicated rather than shared — the
``xdist_group`` marker below is what keeps the two files off each other.

That duplication used to extend to the podman helpers, on the grounds that
extracting a landed car's helper bought no behaviour. Car G6 inverted the
reasoning: the socket directory can no longer be ``/tmp``, because a
dind-backed runner resolves a bind-mount source on the DAEMON's filesystem, not
ours (see ``_podman.py``). Deriving that directory is behaviour, it is needed
identically by all four files, and a third private copy would be the drift this
file exists to oppose. The env/runtime helpers are therefore imported now, and
only the fixture body remains local.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
import uuid

import pytest

pytest.importorskip("sqlalchemy", reason="sqlalchemy not installed (sql extra)")
pytest.importorskip("alembic", reason="alembic not installed (sql extra)")

from yadgar._shared.storage.sql import MariaStorageEngine  # noqa: E402
from yadgar._shared.storage.sql.migrate import (  # noqa: E402
    current_revision,
    heads,
    upgrade_to_head,
    upgrade_to_head_as_migrator,
)
from yadgar.tests import _entrypoint_sql as eps  # noqa: E402
from yadgar.tests.integration._podman import (  # noqa: E402
    container_is_running,
    container_logs,
    make_socket_dir,
    podman_env,
    remove_container_dir,
    select_container_runtime,
)

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("engine2_mariadb")]

# ADR-0205 measured idle RSS on 11.4; ADR-0212 pins the engine-#2 server version.
# Debian trixie — what Dockerfile.backend installs mariadb-server from — ships
# 11.8, and the 1146 rejection and the 1142 denial were both reproduced on
# 11.4 AND 11.8, so the behaviour under test is not version-specific.
_IMAGE = "docker.io/library/mariadb:11.4"
_DB = "yadgar"
_APP_USER = "yadgar_app"
_MIGRATE_USER = "yadgar_migrate"
_APP_PASS = "card-integration-password"
_MIGRATE_PASS = "card-integration-migrate-password"
_ROOT_PASS = "card-root"
_SOCKET_IN_CONTAINER = "/sockets/mysqld.sock"
_BOOT_TIMEOUT_SEC = 180.0

EXPECTED_HEAD = "005_project_last_validated"

# Every table the entrypoint grants the app account. Kept as a literal so a
# table silently vanishing from the entrypoint's list is a FAILURE here rather
# than an assertion that quietly checks nothing. ``_shared/test_entrypoint_grants``
# owns the same list for the text half.
GRANTED_TABLES = (
    "alembic_version",
    "config",
    "task",
    "adr",
    "agent_pattern",
    "agent_discipline",
    "agent_pattern_model",
    "client",
    "task_blocked_by",
    "adr_supersedes",
    "agent_pattern_composes",
    "project",
)

# The variables the entrypoint's heredocs interpolate. ``_pass_esc`` /
# ``_mig_pass_esc`` are the shell's single-quote-doubled locals; the test
# passwords contain no quote, so they pass through unchanged.
_SUBSTITUTIONS = {
    "MARIADB_DB": _DB,
    "MARIADB_APP_USER": _APP_USER,
    "MARIADB_MIGRATE_USER": _MIGRATE_USER,
    "_pass_esc": _APP_PASS,
    "_mig_pass_esc": _MIGRATE_PASS,
}


def _cnf_body(socket: str, user: str, password: str) -> str:
    """Byte-shape of the option files entrypoint-backend.sh writes (car A)."""
    return (
        "\n".join(
            [
                "[client]",
                f"socket = {socket}",
                f"user = {user}",
                f"password = {password}",
                f"database = {_DB}",
            ]
        )
        + "\n"
    )


@pytest.fixture(scope="module")
def live_mariadb():
    """Spin a scratch MariaDB reachable over a unix socket; tear it down.

    ROOT ONLY. No ``MARIADB_USER`` — that env var is precisely what made this
    file test the wrong privileges. Every account here is created by replaying
    the entrypoint, as the privileged socket account the entrypoint itself
    uses (``--user=${MARIADB_ADMIN_USER}``; root over the socket is its
    equivalent inside the stock image — the PRIVILEGES are what this file is
    about, not which auth plugin the admin uses).

    NOT ``tmp_path``: a unix socket path caps at ~107 bytes and pytest's tmp
    dirs are long enough to blow it. Not ``/tmp`` either — the mount source is
    resolved by the container DAEMON, which is a separate filesystem under a
    dind-backed runner (``_podman.shared_mount_root``). Resource-capped and
    removed with its anonymous volume — the image declares ``/var/lib/mysql`` a
    VOLUME, so without ``-v`` every run leaks a datadir-sized one. Never
    touches the live data root.
    """
    runtime = select_container_runtime()
    if runtime is None:
        pytest.skip(
            "no working container runtime on this host "
            "(podman/docker absent, or present but non-functional)"
        )

    name = f"yadgar-card-mdb-{uuid.uuid4().hex[:8]}"
    sock_dir = make_socket_dir(runtime, image=_IMAGE, prefix="ymdb")
    socket_path = sock_dir / "mysqld.sock"

    started = subprocess.run(
        [
            runtime, "run", "-d", "--name", name,
            "--memory", "512m", "--cpus", "1",
            "-e", f"MARIADB_ROOT_PASSWORD={_ROOT_PASS}",
            "-v", f"{sock_dir}:/sockets:Z",
            _IMAGE,
            f"--socket={_SOCKET_IN_CONTAINER}",
        ],
        capture_output=True, text=True, check=False, timeout=300, env=podman_env(),
    )  # fmt: skip
    if started.returncode != 0:
        shutil.rmtree(sock_dir, ignore_errors=True)
        pytest.skip(f"could not start MariaDB container: {started.stderr.strip()}")

    server = {
        "runtime": runtime,
        "name": name,
        "dir": sock_dir,
        "socket": socket_path,
        "app_cnf": sock_dir / "client.cnf",
        "migrate_cnf": sock_dir / "migrate.cnf",
    }
    try:
        _await_ready(server)
        yield server
    finally:
        subprocess.run(
            [runtime, "rm", "-f", "-v", name],
            capture_output=True, check=False, timeout=120, env=podman_env(),
        )  # fmt: skip
        remove_container_dir(runtime, sock_dir, image=_IMAGE)


def admin_sql(server: dict, sql: str) -> subprocess.CompletedProcess:
    """Run SQL as the privileged socket account — the entrypoint's ADMIN step.

    Deliberately WITHOUT ``--force``, exactly as ``entrypoint-backend.sh`` runs
    it: the abort-on-first-error behaviour is half of the bug under test.
    """
    return subprocess.run(
        [
            server["runtime"], "exec", "-i", server["name"],
            "mariadb", f"--socket={_SOCKET_IN_CONTAINER}", "-uroot", f"-p{_ROOT_PASS}",
        ],
        input=sql, capture_output=True, text=True, check=False, timeout=120,
        env=podman_env(),
    )  # fmt: skip


def _await_ready(server: dict) -> None:
    """Block until the server answers — the socket appears before it is usable,
    because the official image runs a bootstrap server first.

    A container that has DIED cannot start answering, so the wait ends there
    rather than spending the remaining timeout on it, and reports the logs that
    say why.
    """
    runtime, name = server["runtime"], server["name"]
    deadline = time.monotonic() + _BOOT_TIMEOUT_SEC
    last = ""
    while time.monotonic() < deadline:
        probe = admin_sql(server, "SELECT 1")
        if probe.returncode == 0:
            return
        if not container_is_running(runtime, name):
            raise AssertionError(
                f"the MariaDB container {name} exited during boot; "
                f"last logs:\n{container_logs(runtime, name)}"
            )
        last = probe.stderr.strip()
        time.sleep(1.0)
    raise AssertionError(f"MariaDB not ready within {_BOOT_TIMEOUT_SEC}s: {last}")


# ── replaying entrypoint-backend.sh ──────────────────────────────────────────


def run_accounts_phase(server: dict) -> None:
    """Phase A — the entrypoint's accounts heredoc, plus the option files it writes."""
    result = admin_sql(server, eps.accounts_sql(_SUBSTITUTIONS))
    assert result.returncode == 0, f"entrypoint phase A failed:\n{result.stderr}"
    for path, user, password in (
        (server["app_cnf"], _APP_USER, _APP_PASS),
        (server["migrate_cnf"], _MIGRATE_USER, _MIGRATE_PASS),
    ):
        path.write_text(_cnf_body(str(server["socket"]), user, password), encoding="utf-8")
        path.chmod(0o600)


async def run_migration_phase(server: dict) -> str:
    """Phase B — the Alembic chain as the MIGRATION account."""
    return await upgrade_to_head_as_migrator(server["migrate_cnf"])


def run_grants_phase(server: dict) -> subprocess.CompletedProcess:
    """Phase C — the app account's narrowing grants. Returned, not asserted:
    one test needs to see this FAIL (running it before the tables exist)."""
    return admin_sql(server, eps.grants_sql(_SUBSTITUTIONS))


async def run_production_bootstrap(server: dict) -> str:
    """All three phases, in the entrypoint's order. Returns the head revision."""
    run_accounts_phase(server)
    head = await run_migration_phase(server)
    grants = run_grants_phase(server)
    assert grants.returncode == 0, f"entrypoint phase C failed:\n{grants.stderr}"
    return head


@pytest.fixture
def fresh_server(live_mariadb):
    """A server with NO engine-#2 database and NO engine-#2 accounts.

    The state a fresh install starts from, which is where the fresh-install
    failure mode lives: a test that pre-creates the tables cannot see it.
    """
    reset = "\n".join(
        [
            f"DROP DATABASE IF EXISTS `{_DB}`;",
            f"DROP USER IF EXISTS '{_APP_USER}'@'localhost';",
            f"DROP USER IF EXISTS '{_MIGRATE_USER}'@'localhost';",
        ]
    )
    result = admin_sql(live_mariadb, reset)
    assert result.returncode == 0, result.stderr
    for cnf in (live_mariadb["app_cnf"], live_mariadb["migrate_cnf"]):
        cnf.unlink(missing_ok=True)
    return live_mariadb


@pytest.fixture
async def bootstrapped(fresh_server):
    """A server brought up exactly the way the container brings it up."""
    await run_production_bootstrap(fresh_server)
    return fresh_server


def app_engine(server: dict, **kwargs) -> MariaStorageEngine:
    """An engine on the RUNTIME credentials — the DDL-less app account."""
    return MariaStorageEngine.from_option_file(server["app_cnf"], **kwargs)


async def _scalar(engine: MariaStorageEngine, sql: str):
    from sqlalchemy import text  # noqa: PLC0415

    async with engine.engine.connect() as conn:
        return (await conn.execute(text(sql))).scalar()


def granted_tables(server: dict) -> set[str]:
    """Tables the app account actually holds a grant on, read back from the server."""
    shown = admin_sql(server, f"SHOW GRANTS FOR '{_APP_USER}'@'localhost'")
    assert shown.returncode == 0, shown.stderr
    found: set[str] = set()
    for line in shown.stdout.splitlines():
        if " ON " not in line or " TO " not in line:
            continue
        target = line.split(" ON ", 1)[1].split(" TO ", 1)[0].strip()
        if "." not in target:
            continue
        db, _, table = target.rpartition(".")
        if db.strip("`") == _DB:
            found.add(table.strip("`"))
    return found


# ── the bug, pinned ──────────────────────────────────────────────────────────


async def test_the_production_bootstrap_brings_a_fresh_schema_to_head(fresh_server):
    """THE regression test: the container's own sequence, from an empty database.

    Fails on the pre-fix entrypoint in whichever way it is run — as one
    combined heredoc the first GRANT is rejected 1146 before any account is
    usable; with the migration driven by the app account it is 1142 on
    ``CREATE TABLE task``.
    """
    head = await run_production_bootstrap(fresh_server)
    assert head == EXPECTED_HEAD
    assert heads() == (EXPECTED_HEAD,)

    engine = app_engine(fresh_server)
    try:
        assert await current_revision(engine.engine) == EXPECTED_HEAD
        tables = set(await engine.list_tables())
    finally:
        await engine.dispose()
    assert set(GRANTED_TABLES) <= tables, f"missing after migration: {set(GRANTED_TABLES) - tables}"


def test_the_module_entry_point_the_entrypoint_actually_calls(fresh_server):
    """``python3 -m yadgar._shared.storage.sql.migrate`` — phase B as a SUBPROCESS.

    Every other test drives the chain through the Python API, but the container
    invokes a module entry point from shell. A broken ``__main__`` guard, a
    missing exit code or an unreadable option file would be invisible to the
    API tests and fatal in production, so this runs the real command line.
    """
    run_accounts_phase(fresh_server)

    # The ARGUMENT form the entrypoint uses, asserted here rather than assumed:
    # a test that passed the option file through a different channel would go
    # green against a phase B that cannot find its credentials.
    command = eps.migration_command_line()
    assert '"${MARIADB_MIGRATE_CNF}"' in command, command

    def _run(option_file) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "yadgar._shared.storage.sql.migrate", str(option_file)],
            capture_output=True, text=True, check=False, timeout=300,
        )  # fmt: skip

    ok = _run(fresh_server["migrate_cnf"])
    assert ok.returncode == 0, ok.stderr
    assert EXPECTED_HEAD in ok.stderr

    # And a failure is a NON-ZERO exit whose message names the driver errno —
    # the entrypoint gates phase C on this status, so a migration that failed
    # while exiting 0 would apply the REVOKE and then abort on 1146.
    bad = _run(fresh_server["dir"] / "does-not-exist.cnf")
    assert bad.returncode == 1
    assert "engine #2 migration FAILED" in bad.stderr

    # The RUNTIME credentials must not be a working substitute — that is the
    # whole point of the split.
    denied = _run(fresh_server["app_cnf"])
    assert denied.returncode == 1
    assert '"error_code": 114' in denied.stderr or '"error_code": 1044' in denied.stderr, (
        denied.stderr
    )


async def test_every_granted_table_reaches_the_app_account(bootstrapped):
    """Phase C completed — no statement aborted the ones behind it.

    Read back from the SERVER, not from the heredoc text: the shipped bug was
    a grant list that looked complete and stopped applying at line 4.
    """
    assert granted_tables(bootstrapped) == set(GRANTED_TABLES)


async def test_the_app_account_can_read_every_ledger_table(bootstrapped):
    """The grants are not merely present, they WORK from the runtime engine.

    ``SHOW GRANTS`` listing a table and the runtime account being able to read
    it are different claims — a grant on a table the migration never created
    would satisfy the first and not the second.
    """
    engine = app_engine(bootstrapped)
    try:
        for table in GRANTED_TABLES:
            # ``alembic_version`` legitimately holds the stamp row; every other
            # table is empty on a fresh schema (ADR-0203 for ``config``).
            expected = 1 if table == "alembic_version" else 0
            assert await _scalar(engine, f"SELECT COUNT(*) FROM `{table}`") == expected
    finally:
        await engine.dispose()


async def test_the_app_account_can_write_through_the_chokepoint(bootstrapped):
    """A real INSERT + SELECT through the D20 chokepoint under production grants."""
    engine = app_engine(bootstrapped)
    try:
        await engine.create_project_row(key="m-agahi/yadgar", kind="git", display_name="yadgar")
        assert [row["key"] for row in await engine.list_project_rows()] == ["m-agahi/yadgar"]
        created = await engine.create_task_row(project_id="m-agahi/yadgar", title="a task")
        assert (await engine.get_task_row(created["id"]))["title"] == "a task"
    finally:
        await engine.dispose()


async def test_the_app_account_still_cannot_create_a_table(bootstrapped):
    """D19, asserted against a live server — the mutation guard on this fix.

    Making the migration work by granting the runtime account CREATE would
    turn every other test in this file green and delete the property the
    narrowing exists for. This is the one that would go red.
    """
    from sqlalchemy import text  # noqa: PLC0415

    engine = app_engine(bootstrapped)
    try:
        with pytest.raises(Exception) as caught:  # noqa: PT011 — errno asserted below
            async with engine.engine.begin() as conn:
                await conn.execute(text("CREATE TABLE d19_probe (id INT PRIMARY KEY)"))
    finally:
        await engine.dispose()
    assert getattr(caught.value, "orig", caught.value).args[0] == 1142, (
        f"expected 1142 CREATE-denied, got {caught.value!r}"
    )


async def test_migrating_as_the_runtime_account_is_denied(fresh_server):
    """The shipped failure, verbatim: the chain cannot run on the app engine.

    Accounts exist and the app account holds what it can hold on an empty
    database, so the chain gets as far as its first ``CREATE TABLE`` and is
    refused — which is exactly what the backend did on every boot.
    """
    run_accounts_phase(fresh_server)
    engine = app_engine(fresh_server)
    try:
        with pytest.raises(Exception) as caught:  # noqa: PT011 — errno asserted below
            await upgrade_to_head(engine.engine)
    finally:
        await engine.dispose()
    orig = getattr(caught.value, "orig", caught.value)
    assert orig.args[0] in (1142, 1044), f"expected a privilege denial, got {caught.value!r}"


async def test_grants_applied_before_the_migration_are_rejected(fresh_server):
    """The ordering constraint, proven against a live server rather than assumed.

    A future "simplification" that moves the grants back next to the account
    creation puts the fresh-install failure straight back, and this is what
    stops it. ``1146`` is the server refusing a grant on a table that does not
    exist; the client has no ``--force``, so it takes the rest of the heredoc
    with it.
    """
    run_accounts_phase(fresh_server)
    result = run_grants_phase(fresh_server)
    assert result.returncode != 0
    assert "1146" in result.stderr, result.stderr
    assert granted_tables(fresh_server) == set(), (
        "the REVOKE lands and then the grants abort — the account is left with "
        "strictly less than it started with, which is why phase C is gated on "
        "phase B succeeding"
    )


async def test_the_live_host_state_rolls_forward(fresh_server):
    """The 2026-08-08 host: stamped at 0001, ledger tables absent, two grants.

    Reconstructed rather than described: 0001 applied while the account was
    still broad, then the D19 narrowing aborted at ``task``. The fix has to
    carry THAT state to head with no manual repair.
    """
    from alembic import command  # noqa: PLC0415

    from yadgar._shared.storage.sql.migrate import build_alembic_config  # noqa: PLC0415

    run_accounts_phase(fresh_server)

    migrator = MariaStorageEngine.from_option_file(fresh_server["migrate_cnf"])
    try:

        def _up_to_0001(connection) -> None:
            cfg = build_alembic_config()
            cfg.attributes["connection"] = connection
            command.upgrade(cfg, "0001_config")

        async with migrator.engine.connect() as conn:
            await conn.run_sync(_up_to_0001)
    finally:
        await migrator.dispose()

    partial = admin_sql(
        fresh_server,
        "\n".join(
            f"GRANT SELECT, INSERT, UPDATE, DELETE, REFERENCES ON `{_DB}`.{table} "
            f"TO '{_APP_USER}'@'localhost';"
            for table in ("alembic_version", "config")
        ),
    )
    assert partial.returncode == 0, partial.stderr
    assert granted_tables(fresh_server) == {"alembic_version", "config"}

    # …and now the container restarts with the fix in place.
    assert await run_production_bootstrap(fresh_server) == EXPECTED_HEAD
    assert granted_tables(fresh_server) == set(GRANTED_TABLES)

    engine = app_engine(fresh_server)
    try:
        assert await current_revision(engine.engine) == EXPECTED_HEAD
    finally:
        await engine.dispose()


# ── schema shape, unchanged assertions on a correctly-provisioned schema ─────


async def test_upgrade_creates_config_and_stamps_head(bootstrapped):
    engine = app_engine(bootstrapped)
    try:
        tables = await engine.list_tables()
        assert "config" in tables
        assert "alembic_version" in tables
        assert await current_revision(engine.engine) == EXPECTED_HEAD
    finally:
        await engine.dispose()


async def test_config_has_zero_rows(bootstrapped):
    """ADR-0203, asserted ENGINE-DIRECT per exit criterion 1.

    ``config_list() == []`` is NOT evidence — it returned ``[]`` before this
    train and cannot distinguish the engines. Zero rows is what keeps task
    0095's free-re-key window open; seeding belongs to the knob train.
    """
    engine = app_engine(bootstrapped)
    try:
        assert await _scalar(engine, "SELECT COUNT(*) FROM config") == 0
    finally:
        await engine.dispose()


async def test_the_server_accepted_the_shape(bootstrapped):
    """Read the shape back from information_schema, not from our own DDL string."""
    from sqlalchemy import text  # noqa: PLC0415

    engine = app_engine(bootstrapped)
    try:
        async with engine.engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT column_name, data_type, is_nullable, column_key, extra, "
                        "character_maximum_length "
                        "FROM information_schema.columns "
                        "WHERE table_schema = DATABASE() AND table_name = 'config' "
                        "ORDER BY ordinal_position"
                    )
                )
            ).all()
    finally:
        await engine.dispose()

    shape = {str(r[0]): r for r in rows}
    assert set(shape) == {"key", "value", "default_value", "updated_at"}
    assert "directory" not in shape, "ADR-0198/ADR-0207 D2: knobs are global"

    assert str(shape["key"][1]).lower() == "varchar"
    assert shape["key"][5] == 64
    assert str(shape["key"][3]).upper() == "PRI", "key must BE the primary key"
    assert str(shape["key"][2]).upper() == "NO"

    for col in ("value", "default_value"):
        assert str(shape[col][1]).lower() == "text"
        assert str(shape[col][2]).upper() == "NO"

    assert str(shape["updated_at"][1]).lower() == "datetime"
    assert "on update current_timestamp" in str(shape["updated_at"][4]).lower()


async def test_there_is_exactly_one_primary_key_column(bootstrapped):
    """No surrogate id alongside the key — ADR-0198 dropped it deliberately."""
    engine = app_engine(bootstrapped)
    try:
        count = await _scalar(
            engine,
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = 'config' AND column_key = 'PRI'",
        )
    finally:
        await engine.dispose()
    assert count == 1


# ── idempotence: the discriminating test ─────────────────────────────────


async def test_running_upgrade_head_twice_changes_nothing(bootstrapped):
    """The real assertion is that alembic COMMITTED ``alembic_version``.

    MariaDB has no transactional DDL, so alembic opens and commits one
    transaction per migration rather than one for the whole run. If that commit
    did not land, this second call re-runs 0001 and dies on "table config
    already exists" — which is precisely the failure this test exists to catch.
    """
    engine = app_engine(bootstrapped)
    try:
        before = sorted(await engine.list_tables())

        assert await run_migration_phase(bootstrapped) == EXPECTED_HEAD

        assert sorted(await engine.list_tables()) == before
        assert await current_revision(engine.engine) == EXPECTED_HEAD
        assert await _scalar(engine, "SELECT COUNT(*) FROM config") == 0
        assert await _scalar(engine, "SELECT COUNT(*) FROM alembic_version") == 1
    finally:
        await engine.dispose()


async def test_the_whole_bootstrap_is_idempotent(bootstrapped):
    """A restart re-runs all three phases. Nothing may break on the second pass.

    Phase C in particular: ``REVOKE`` then re-``GRANT`` against an account that
    already holds exactly those grants.
    """
    assert await run_production_bootstrap(bootstrapped) == EXPECTED_HEAD
    assert granted_tables(bootstrapped) == set(GRANTED_TABLES)


async def test_upgrade_head_on_a_current_schema_needs_no_ddl_privilege(bootstrapped):
    """The boot matrix row: migrate.cnf absent + already at head → a no-op.

    ``_migrate_engine_two`` falls back to the runtime engine when there are no
    migration credentials, and that is safe ONLY because alembic reads the
    stamp and does nothing when there is nothing to apply. If that ever
    stopped being true the fallback would need removing, so it is pinned.
    """
    engine = app_engine(bootstrapped)
    try:
        assert await upgrade_to_head(engine.engine) == EXPECTED_HEAD
    finally:
        await engine.dispose()


# ── reversibility ────────────────────────────────────────────────────────


async def test_downgrade_removes_the_tables_and_unstamps(bootstrapped):
    """Supported, so it is exercised rather than excused. Runs as the migration
    account — DROP is DDL, and the runtime account has none."""
    from alembic import command  # noqa: PLC0415

    from yadgar._shared.storage.sql.migrate import build_alembic_config  # noqa: PLC0415

    def _downgrade(connection) -> None:
        cfg = build_alembic_config()
        cfg.attributes["connection"] = connection
        command.downgrade(cfg, "base")

    migrator = MariaStorageEngine.from_option_file(bootstrapped["migrate_cnf"])
    try:
        async with migrator.engine.connect() as conn:
            await conn.run_sync(_downgrade)
        assert "config" not in await migrator.list_tables()
        assert await current_revision(migrator.engine) is None
    finally:
        await migrator.dispose()

    # And it goes back up cleanly — a downgrade that cannot be re-upgraded is
    # not reversibility, just deletion.
    assert await run_migration_phase(bootstrapped) == EXPECTED_HEAD
    migrator = MariaStorageEngine.from_option_file(bootstrapped["migrate_cnf"])
    try:
        assert "config" in await migrator.list_tables()
    finally:
        await migrator.dispose()


# ── the boot path, end to end ────────────────────────────────────────────


async def test_the_backend_boot_step_migrates_the_composed_engine(monkeypatch, fresh_server):
    """``_migrate_engine_two`` — the exact call the lifespan makes.

    Goes through ``lifecycle._init_sql_storage`` so the composition root, the
    option file and the migration are proven as ONE path, which is what the
    plan's acceptance rule ("a named caller in the running system") asks for.
    The composed engine is the RUNTIME one; the migration must nonetheless run,
    because the boot step reaches for the migration credentials itself.
    """
    import yadgar._shared.runtime.state as _st
    from yadgar._shared.runtime.lifecycle import _init_sql_storage
    from yadgar.backend.embed_service.embed_service import (
        _dispose_engine_two,
        _migrate_engine_two,
    )

    run_accounts_phase(fresh_server)
    monkeypatch.setenv("YADGAR_MARIADB_CLIENT_CNF", str(fresh_server["app_cnf"]))
    monkeypatch.setenv("YADGAR_MARIADB_MIGRATE_CNF", str(fresh_server["migrate_cnf"]))
    before = _st._sql_storage
    _st._sql_storage = _init_sql_storage()
    assert _st._sql_storage is not None
    try:
        assert await _migrate_engine_two() == EXPECTED_HEAD
        # Phase C is the entrypoint's, not the lifespan's — until it runs the
        # app account holds USAGE only and cannot even open the database
        # (1044). Running it here keeps this test about the boot step while
        # still reading the result through the RUNTIME engine.
        assert run_grants_phase(fresh_server).returncode == 0
        assert "config" in await _st._sql_storage.list_tables()
        assert await _scalar(_st._sql_storage, "SELECT COUNT(*) FROM config") == 0

        # And the teardown step releases the pool.
        await _dispose_engine_two()
    finally:
        await _st._sql_storage.dispose()
        _st._sql_storage = before


async def test_the_boot_step_is_fatal_when_it_cannot_migrate(monkeypatch, fresh_server):
    """No migration credentials AND a schema behind head → the boot step RAISES.

    ADR-0222's rule with the new credential split folded in: ABSENT engine #2
    is skipped, but an engine that is present, behind head, and unmigratable
    with the credentials at hand must stop the boot rather than serve a
    schema-less database. The old swallow is what let this ship silently.
    """
    import yadgar._shared.runtime.state as _st
    from yadgar._shared.runtime.lifecycle import _init_sql_storage
    from yadgar.backend.embed_service.embed_service import _migrate_engine_two

    run_accounts_phase(fresh_server)
    monkeypatch.setenv("YADGAR_MARIADB_CLIENT_CNF", str(fresh_server["app_cnf"]))
    monkeypatch.setenv("YADGAR_MARIADB_MIGRATE_CNF", str(fresh_server["dir"] / "absent.cnf"))
    before = _st._sql_storage
    _st._sql_storage = _init_sql_storage()
    assert _st._sql_storage is not None
    try:
        with pytest.raises(Exception) as caught:  # noqa: PT011 — errno asserted below
            await _migrate_engine_two()
        orig = getattr(caught.value, "orig", caught.value)
        assert orig.args[0] in (1142, 1044)
    finally:
        await _st._sql_storage.dispose()
        _st._sql_storage = before
