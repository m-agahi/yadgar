"""Clearing ``plan_path`` / ``body_slug`` to SQL NULL, EXECUTED against MariaDB.

Marked ``integration`` — the default addopts exclude it
(``-m 'not integration and not e2e'``). Run explicitly::

    pytest yadgar/tests/integration/test_task_write_clear_columns.py -m integration -v

WHY THIS FILE EXISTS AT ALL
---------------------------
Car C part 2 adds ``clear_plan_path`` / ``clear_body_slug`` to ``task_write``
because neither column could be cleared: ``None`` means "the caller did not
mention this column" and drops the key from the UPDATE, while ``""`` writes a
real empty string. The unit round-trip
(``yadgar/tests/core/test_task_write_roundtrip.py``) can prove the tool puts an
explicit ``None`` on the wire — it cannot prove the column ends up ``NULL``
rather than ``''``, because its storage double has no SQL in it and no unique
index.

The distinction is not cosmetic. ``uq_task_project_body_slug``
(migration ``002_ledger_tables.py``) permits repeated ``NULL``s and rejects
repeated ``''``, so the empty-string form silently works once per project and
then throws on the second row cleared that way. Only a real server settles
which of the two the stack produces, and only two rows expose the index.

The core payload builder is driven directly here rather than mocked: what is
under test is the SEAM — that the ``None`` the builder emits survives the
storage layer as ``SET plan_path = NULL``.
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
from yadgar._shared.storage.sql.migrate import upgrade_to_head  # noqa: E402
from yadgar.tests.integration._podman import (  # noqa: E402
    container_is_running,
    container_logs,
    make_socket_dir,
    podman_env,
    remove_container_dir,
    select_container_runtime,
)

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("engine2_mariadb")]

# ADR-0212 pins the engine-#2 server version; the sibling files use the same tag
# so the four MariaDB integration modules share one pulled image.
_IMAGE = "docker.io/library/mariadb:11.4"
_DB = "yadgar"
_APP_USER = "yadgar_app"
_APP_PASS = "clear-integration-password"
_BOOT_TIMEOUT_SEC = 180.0
_MOUNT_VISIBLE_TIMEOUT_SEC = 30.0

_PROJECT = "m-agahi/yadgar-clear"


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

    Fixture body duplicated from the sibling integration modules on purpose —
    the ``xdist_group`` marker is what keeps the five files off each other, and
    a shared module-scoped fixture would defeat it. Only the podman helpers are
    imported (car G6: the socket directory cannot be ``/tmp`` under a
    dind-backed runner).
    """
    runtime = select_container_runtime()
    if runtime is None:
        pytest.skip(
            "no working container runtime on this host "
            "(podman/docker absent, or present but non-functional)"
        )

    name = f"yadgar-clr-mdb-{uuid.uuid4().hex[:8]}"
    sock_dir = make_socket_dir(runtime, image=_IMAGE, prefix="ymdbc")
    socket_path = sock_dir / "mysqld.sock"

    started = subprocess.run(
        [
            runtime, "run", "-d", "--name", name,
            "--memory", "512m", "--cpus", "1",
            "-e", "MARIADB_ROOT_PASSWORD=clear-root",
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


async def _reset_to_base(eng: MariaStorageEngine) -> None:
    """Drop whatever tables the catalog ACTUALLY holds, FK checks off.

    Catalog-driven rather than a hardcoded DROP list or ``alembic downgrade
    base`` — car G6 measured both failure modes (a hardcoded list falls behind
    the migration chain; a stamp-driven downgrade dies on a corrupted stamp and
    takes every later test with it).
    """
    from sqlalchemy import text  # noqa: PLC0415

    tables = await eng.list_tables()
    if not tables:
        return
    async with eng.engine.begin() as conn:
        await conn.execute(text("SET FOREIGN_KEY_CHECKS=0"))
        for table in tables:
            # names come straight from information_schema — catalog state, not input
            await conn.execute(text(f"DROP TABLE IF EXISTS `{table}`"))
        await conn.execute(text("SET FOREIGN_KEY_CHECKS=1"))


@pytest.fixture
async def seeded(engine):
    """A migrated schema holding two tasks that both carry plan_path + body_slug.

    Two, not one: clearing a single row cannot expose
    ``uq_task_project_body_slug``, which is the constraint that makes the
    NULL-vs-``''`` distinction load-bearing rather than stylistic.
    """
    await upgrade_to_head(engine.engine)
    try:
        await engine.create_project_row(key=_PROJECT, kind="git")
        for n in (1, 2):
            await engine.create_task_row(
                project_id=_PROJECT,
                title=f"task {n}",
                status="pending",
                plan_path=f"docs/plans/superseded-{n}.md",
                body_slug=f"m-agahi_yadgar_task-{n}",
            )
        yield engine
    finally:
        await _reset_to_base(engine)


async def _task_ids(eng) -> list[int]:
    rows = await eng.list_task_rows(project_id=_PROJECT, status=["pending"])
    return [int(r["id"]) for r in rows]


def _clear_payload(task_id: int, *, plan_path: bool = False, body_slug: bool = False) -> dict:
    """The payload the REAL core builder emits for a clear — no hand-written dict."""
    from yadgar.core.server.tools.task import _build_update_payload, _TaskUpdateFields

    payload = _build_update_payload(
        task_id,
        # No title: the two-row test would otherwise rename every row it
        # clears, and the point here is the cleared column, nothing else.
        _TaskUpdateFields(clear_plan_path=plan_path, clear_body_slug=body_slug),
    )
    payload.pop("id")
    return payload


async def test_clear_plan_path_writes_sql_null(seeded):
    """``SET plan_path = NULL`` — the read-back is None, not ``''``."""
    from sqlalchemy import text

    task_id = (await _task_ids(seeded))[0]
    await seeded.update_task_row(task_id, **_clear_payload(task_id, plan_path=True))

    row = await seeded.get_task_row(task_id)
    assert row is not None
    assert row["plan_path"] is None
    # The driver maps both NULL and '' to a falsy Python value on some paths;
    # ask the server which one it stored.
    async with seeded.engine.connect() as conn:
        result = await conn.execute(
            text("SELECT plan_path IS NULL AS is_null FROM task WHERE id = :id"),
            {"id": task_id},
        )
        assert int(result.first()[0]) == 1


async def test_clear_body_slug_writes_sql_null(seeded):
    from sqlalchemy import text

    task_id = (await _task_ids(seeded))[0]
    await seeded.update_task_row(task_id, **_clear_payload(task_id, body_slug=True))

    async with seeded.engine.connect() as conn:
        result = await conn.execute(
            text("SELECT body_slug IS NULL AS is_null FROM task WHERE id = :id"),
            {"id": task_id},
        )
        assert int(result.first()[0]) == 1


async def test_clearing_body_slug_on_two_rows_does_not_violate_the_unique_index(seeded):
    """THE reason ``''`` is unsafe. MySQL permits repeated NULLs, not repeated ''."""
    for task_id in await _task_ids(seeded):
        await seeded.update_task_row(task_id, **_clear_payload(task_id, body_slug=True))

    rows = await seeded.list_task_rows(project_id=_PROJECT, status=["pending"])
    assert [r["body_slug"] for r in rows] == [None, None]


async def test_the_empty_string_form_is_what_would_have_broken(seeded):
    """The control: prove the constraint bites, so the NULL test is not vacuous.

    Without this, a passing NULL test could not distinguish "the fix works"
    from "the unique index does not exist on this schema".
    """
    task_ids = await _task_ids(seeded)
    await seeded.update_task_row(task_ids[0], body_slug="")

    with pytest.raises(Exception, match="Duplicate|1062"):
        await seeded.update_task_row(task_ids[1], body_slug="")


async def test_clear_does_not_disturb_the_other_column(seeded):
    """Clearing one column leaves the other exactly as it was."""
    task_id = (await _task_ids(seeded))[0]
    await seeded.update_task_row(task_id, **_clear_payload(task_id, plan_path=True))

    row = await seeded.get_task_row(task_id)
    assert row is not None
    assert row["plan_path"] is None
    assert row["body_slug"] == "m-agahi_yadgar_task-1"
    assert row["title"] == "task 1"
