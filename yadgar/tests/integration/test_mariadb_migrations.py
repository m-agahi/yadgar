"""Engine-#2 Alembic chain against a REAL MariaDB (car D).

Marked ``integration`` — the default addopts exclude it
(``-m 'not integration and not e2e'``). Run explicitly::

    pytest yadgar/tests/integration/test_mariadb_migrations.py -m integration -v

WHAT ONLY A LIVE SERVER CAN SETTLE
----------------------------------
The offline half (``yadgar/tests/_shared/test_mariadb_migrations.py``) pins the
rendered DDL, which is enough for shape. Three things it cannot reach:

  * that MariaDB ACCEPTS the DDL — ``DEFAULT CURRENT_TIMESTAMP ON UPDATE
    CURRENT_TIMESTAMP`` travels in ``server_default`` because SQLAlchemy's
    ``server_onupdate`` emits nothing, and whether that renders to something the
    server parses is a server question;
  * that the run is IDEMPOTENT — which is really the question of whether alembic
    COMMITTED the ``alembic_version`` row. MariaDB has no transactional DDL, so
    alembic opens and commits one transaction per migration; if that did not
    reach disk, a second ``upgrade head`` re-runs the revision and dies on
    "table already exists". Nothing offline can catch it;
  * that ``config`` really has ZERO rows (ADR-0203) — asserted ENGINE-DIRECT
    with ``SELECT COUNT(*)``, because exit criterion 1 says ``config_list() == []``
    is not evidence: it returned ``[]`` before this train and cannot tell the
    engines apart.

The container fixture is car C's, duplicated rather than shared. Extracting it to
a conftest would put a refactor of a landed car in this one's diff for no gain,
and the ``xdist_group`` marker below is what keeps the two files off each other.
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

from yadgar._shared.storage.sql import MariaStorageEngine  # noqa: E402
from yadgar._shared.storage.sql.migrate import (  # noqa: E402
    current_revision,
    heads,
    upgrade_to_head,
)

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("engine2_mariadb")]

# ADR-0205 measured idle RSS on 11.4; ADR-0212 pins the engine-#2 server version.
_IMAGE = "docker.io/library/mariadb:11.4"
_DB = "yadgar"
_APP_USER = "yadgar_app"
_APP_PASS = "card-integration-password"
_BOOT_TIMEOUT_SEC = 180.0

EXPECTED_HEAD = "0001_config"


def _cnf_body(socket: str) -> str:
    """Byte-shape of the option file entrypoint-backend.sh writes (car A)."""
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
    """Spin a scratch MariaDB reachable over a unix socket; tear it down.

    NOT ``tmp_path``: a unix socket path caps at ~107 bytes and pytest's tmp
    dirs are long enough to blow it. Resource-capped and removed with its
    anonymous volume — the image declares ``/var/lib/mysql`` a VOLUME, so
    without ``-v`` every run leaks a datadir-sized one. Never touches the live
    data root.
    """
    runtime = shutil.which("podman") or shutil.which("docker")
    if runtime is None:
        pytest.skip("docker/podman not available on this host")

    name = f"yadgar-card-mdb-{uuid.uuid4().hex[:8]}"
    sock_dir = Path(f"/tmp/ymdb-{uuid.uuid4().hex[:8]}")
    sock_dir.mkdir(mode=0o777, parents=True)
    sock_dir.chmod(0o777)  # mkdir mode is umask-masked
    socket_path = sock_dir / "mysqld.sock"

    started = subprocess.run(
        [
            runtime, "run", "-d", "--name", name,
            "--memory", "512m", "--cpus", "1",
            "-e", "MARIADB_ROOT_PASSWORD=card-root",
            "-e", f"MARIADB_DATABASE={_DB}",
            "-e", f"MARIADB_USER={_APP_USER}",
            "-e", f"MARIADB_PASSWORD={_APP_PASS}",
            "-v", f"{sock_dir}:/sockets:Z",
            _IMAGE,
            "--socket=/sockets/mysqld.sock",
        ],
        capture_output=True, text=True, check=False, timeout=300,
    )  # fmt: skip
    if started.returncode != 0:
        shutil.rmtree(sock_dir, ignore_errors=True)
        pytest.skip(f"could not start MariaDB container: {started.stderr.strip()}")

    cnf = sock_dir / "client.cnf"
    cnf.write_text(_cnf_body(str(socket_path)), encoding="utf-8")
    cnf.chmod(0o600)

    try:
        _await_ready(cnf)
        yield {"cnf": cnf, "socket": socket_path, "dir": sock_dir}
    finally:
        subprocess.run(
            [runtime, "rm", "-f", "-v", name], capture_output=True, check=False, timeout=120
        )
        _remove_socket_dir(runtime, sock_dir)


def _remove_socket_dir(runtime: str, sock_dir: Path) -> None:
    """Delete the mount dir, including what the container's uid took ownership of.

    The image's entrypoint chowns the socket mount to its own ``mysql`` user,
    which under rootless podman is a SUBUID — the host user then cannot even
    rmdir it despite owning the parent. ``podman unshare`` re-enters the user
    namespace where that subuid maps to root.
    """
    shutil.rmtree(sock_dir, ignore_errors=True)
    if sock_dir.exists() and Path(runtime).name == "podman":
        subprocess.run(
            [runtime, "unshare", "rm", "-rf", str(sock_dir)],
            capture_output=True, check=False, timeout=120,
        )  # fmt: skip


def _await_ready(cnf: Path) -> None:
    """Block until a real query succeeds — the socket appears before the server
    is usable, because the official image runs a bootstrap server first."""
    import asyncio

    async def _probe() -> None:
        deadline = time.monotonic() + _BOOT_TIMEOUT_SEC
        last: Exception | None = None
        while time.monotonic() < deadline:
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


@pytest.fixture
async def migrated(engine):
    """An engine whose schema is at head — the state the backend boots into."""
    await upgrade_to_head(engine.engine)
    try:
        yield engine
    finally:
        # Leave the module-scoped server clean for the next test.
        from sqlalchemy import text  # noqa: PLC0415

        async with engine.engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS config"))
            await conn.execute(text("DROP TABLE IF EXISTS alembic_version"))


async def _scalar(engine: MariaStorageEngine, sql: str):
    from sqlalchemy import text  # noqa: PLC0415

    async with engine.engine.connect() as conn:
        return (await conn.execute(text(sql))).scalar()


# ── the migration runs green from empty ──────────────────────────────────


async def test_the_schema_starts_empty(engine):
    """The premise: car C creates nothing, so this is a from-scratch run."""
    assert await engine.list_tables() == []
    assert await current_revision(engine.engine) is None


async def test_upgrade_creates_config_and_stamps_head(migrated):
    tables = await migrated.list_tables()
    assert "config" in tables
    assert "alembic_version" in tables
    assert await current_revision(migrated.engine) == EXPECTED_HEAD
    assert heads() == (EXPECTED_HEAD,)


async def test_config_has_zero_rows(migrated):
    """ADR-0203, asserted ENGINE-DIRECT per exit criterion 1.

    ``config_list() == []`` is NOT evidence — it returned ``[]`` before this
    train and cannot distinguish the engines. Zero rows is what keeps task
    0095's free-re-key window open; seeding belongs to the knob train.
    """
    assert await _scalar(migrated, "SELECT COUNT(*) FROM config") == 0


async def test_the_server_accepted_the_shape(migrated):
    """Read the shape back from information_schema, not from our own DDL string."""
    from sqlalchemy import text  # noqa: PLC0415

    async with migrated.engine.connect() as conn:
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


async def test_there_is_exactly_one_primary_key_column(migrated):
    """No surrogate id alongside the key — ADR-0198 dropped it deliberately."""
    count = await _scalar(
        migrated,
        "SELECT COUNT(*) FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = 'config' AND column_key = 'PRI'",
    )
    assert count == 1


# ── idempotence: the discriminating test ─────────────────────────────────


async def test_running_upgrade_head_twice_changes_nothing(migrated):
    """The real assertion is that alembic COMMITTED ``alembic_version``.

    MariaDB has no transactional DDL, so alembic opens and commits one
    transaction per migration rather than one for the whole run. If that commit
    did not land, this second call re-runs 0001 and dies on "table config
    already exists" — which is precisely the failure this test exists to catch.
    """
    before = sorted(await migrated.list_tables())

    assert await upgrade_to_head(migrated.engine) == EXPECTED_HEAD

    assert sorted(await migrated.list_tables()) == before
    assert await current_revision(migrated.engine) == EXPECTED_HEAD
    assert await _scalar(migrated, "SELECT COUNT(*) FROM config") == 0
    assert await _scalar(migrated, "SELECT COUNT(*) FROM alembic_version") == 1


# ── reversibility ────────────────────────────────────────────────────────


async def test_downgrade_removes_the_table_and_unstamps(migrated):
    """Supported, so it is exercised rather than excused."""
    from alembic import command  # noqa: PLC0415

    from yadgar._shared.storage.sql.migrate import build_alembic_config  # noqa: PLC0415

    def _downgrade(connection) -> None:
        cfg = build_alembic_config()
        cfg.attributes["connection"] = connection
        command.downgrade(cfg, "base")

    async with migrated.engine.connect() as conn:
        await conn.run_sync(_downgrade)

    assert "config" not in await migrated.list_tables()
    assert await current_revision(migrated.engine) is None

    # And it goes back up cleanly — a downgrade that cannot be re-upgraded is
    # not reversibility, just deletion.
    assert await upgrade_to_head(migrated.engine) == EXPECTED_HEAD
    assert "config" in await migrated.list_tables()


# ── the boot path, end to end ────────────────────────────────────────────


async def test_the_backend_boot_step_migrates_the_composed_engine(monkeypatch, live_mariadb):
    """``_migrate_engine_two`` — the exact call the lifespan makes.

    Goes through ``lifecycle._init_sql_storage`` so the composition root, the
    option file and the migration are proven as ONE path, which is what the
    plan's acceptance rule ("a named caller in the running system") asks for.
    """
    import yadgar._shared.runtime.state as _st
    from yadgar._shared.runtime.lifecycle import _init_sql_storage
    from yadgar.backend.embed_service.embed_service import (
        _dispose_engine_two,
        _migrate_engine_two,
    )

    monkeypatch.setenv("YADGAR_MARIADB_CLIENT_CNF", str(live_mariadb["cnf"]))
    before = _st._sql_storage
    _st._sql_storage = _init_sql_storage()
    assert _st._sql_storage is not None
    try:
        assert await _migrate_engine_two() == EXPECTED_HEAD
        assert "config" in await _st._sql_storage.list_tables()
        assert await _scalar(_st._sql_storage, "SELECT COUNT(*) FROM config") == 0

        # And the teardown step releases the pool.
        await _dispose_engine_two()
    finally:
        from sqlalchemy import text  # noqa: PLC0415

        async with _st._sql_storage.engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS config"))
            await conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        await _st._sql_storage.dispose()
        _st._sql_storage = before
