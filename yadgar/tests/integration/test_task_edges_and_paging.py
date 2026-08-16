"""``task_blocked_by`` edges and ``limit``/``offset``, EXECUTED against MariaDB.

Marked ``integration`` — the default addopts exclude it
(``-m 'not integration and not e2e'``). Run explicitly::

    pytest yadgar/tests/integration/test_task_edges_and_paging.py -m integration -v

WHY THIS FILE EXISTS AT ALL
---------------------------
Car E (edges) and Car D (paging) are both claims about what the SERVER does
with a statement, and both replace a surface that reported success while doing
nothing:

* ``task_blocked_by`` has been writable and unreadable since migration 002.
  ``list_task_blocked_by`` existed with no caller, no inverse and no admin op,
  and a failed edge write was logged and answered ``ok: true`` — so the
  2026-08-15 backfill wrote six dependency edges and could not confirm one of
  them. The only thing that settles "the edge is in the table" is reading it
  back off a real server, from BOTH ends of the join.

* ``limit`` / ``offset`` were accepted by ``task_list``, forwarded when
  non-default, and never reached a ``LIMIT`` clause — ``limit=5`` returned all
  77 rows, confirmed live 2026-08-16. A source-string assertion would pin the
  presence of the word ``LIMIT``, which is exactly the shape of test that let
  the L10 row-constructor bug live its whole life (see
  ``test_task_list_status_filter.py``). Only executing it against a server
  counts the rows.

The engine methods are driven directly here rather than through the MCP tool:
what is under test is the SQL the storage layer emits, and the tool layer's own
forwarding is covered by the no-DB unit tests.
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
_APP_PASS = "edges-integration-password"
_BOOT_TIMEOUT_SEC = 180.0
_MOUNT_VISIBLE_TIMEOUT_SEC = 30.0

_PROJECT = "m-agahi/yadgar-edges"


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

    name = f"yadgar-edg-mdb-{uuid.uuid4().hex[:8]}"
    sock_dir = make_socket_dir(runtime, image=_IMAGE, prefix="ymdbe")
    socket_path = sock_dir / "mysqld.sock"

    started = subprocess.run(
        [
            runtime, "run", "-d", "--name", name,
            "--memory", "512m", "--cpus", "1",
            "-e", "MARIADB_ROOT_PASSWORD=edges-root",
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
    """A migrated schema holding six open tasks in one project.

    Six, not two: the paging tests need a set big enough that ``limit=5``
    returning "everything" and ``limit=5`` returning five are distinguishable,
    which two rows cannot show.
    """
    await upgrade_to_head(engine.engine)
    try:
        await engine.create_project_row(key=_PROJECT, kind="git")
        for n in range(1, 7):
            await engine.create_task_row(
                project_id=_PROJECT,
                title=f"task {n}",
                status="pending",
            )
        yield engine
    finally:
        await _reset_to_base(engine)


async def _ids(eng) -> list[int]:
    rows = await eng.list_task_rows(project_id=_PROJECT, status=None)
    return [int(r["id"]) for r in rows]


# ── Car E: the edges are readable, from both ends ────────────────────────────


async def test_written_edges_read_back_as_the_actual_ids(seeded):
    """THE regression: six edges were written in the backfill and none confirmed."""
    ids = await _ids(seeded)
    blocked, first, second = ids[0], ids[1], ids[2]

    await seeded.add_task_blocked_by(blocked, first)
    await seeded.add_task_blocked_by(blocked, second)

    assert await seeded.list_task_blocked_by(blocked) == sorted([first, second])


async def test_the_same_edge_reads_back_from_the_blocks_end(seeded):
    """``list_task_blocks`` is the inverse reader Car E adds; it had none before."""
    ids = await _ids(seeded)
    blocked, blocker = ids[0], ids[1]

    await seeded.add_task_blocked_by(blocked, blocker)

    assert await seeded.list_task_blocks(blocker) == [blocked]
    assert await seeded.list_task_blocks(blocked) == []


async def test_remove_task_blocked_by_deletes_the_row(seeded):
    """The DELETE half — it used to be inline SQL in the admin op body."""
    ids = await _ids(seeded)
    blocked, blocker = ids[0], ids[1]
    await seeded.add_task_blocked_by(blocked, blocker)

    await seeded.remove_task_blocked_by(blocked, blocker)

    assert await seeded.list_task_blocked_by(blocked) == []
    assert await seeded.list_task_blocks(blocker) == []


async def test_remove_task_blocked_by_on_an_absent_edge_is_a_no_op(seeded):
    """Idempotent delete — the reconciler must not need to check first."""
    ids = await _ids(seeded)

    await seeded.remove_task_blocked_by(ids[0], ids[1])

    assert await seeded.list_task_blocked_by(ids[0]) == []


async def test_list_task_edges_returns_both_directions_in_one_query(seeded):
    """The bulk reader the LIST path uses instead of 2N round-trips."""
    ids = await _ids(seeded)
    a, b, c = ids[0], ids[1], ids[2]
    await seeded.add_task_blocked_by(a, b)  # a is blocked by b
    await seeded.add_task_blocked_by(c, a)  # a blocks c

    edges = await seeded.list_task_edges([a, b, c])

    assert edges[a] == {"blocked_by": [b], "blocks": [c]}
    assert edges[b] == {"blocked_by": [], "blocks": [a]}
    assert edges[c] == {"blocked_by": [a], "blocks": []}


async def test_list_task_edges_keys_every_requested_id(seeded):
    """ "Asked and has none" must be distinguishable from "did not ask"."""
    ids = await _ids(seeded)

    edges = await seeded.list_task_edges(ids)

    assert set(edges) == set(ids)
    assert all(e == {"blocked_by": [], "blocks": []} for e in edges.values())


async def test_list_task_edges_on_an_empty_id_list_runs_no_query(seeded):
    """An empty page must not reach the server with ``IN ()``."""
    assert await seeded.list_task_edges([]) == {}


# ── Car D: limit / offset reach a real clause ────────────────────────────────


async def test_limit_returns_exactly_that_many_rows(seeded):
    """THE regression: ``limit=5`` returned all 77 rows on the live corpus."""
    rows = await seeded.list_task_rows(project_id=_PROJECT, status=None, limit=5)

    assert len(rows) == 5


async def test_no_limit_still_returns_every_row(seeded):
    """The default must stay uncapped — the seeder needs the complete open set."""
    rows = await seeded.list_task_rows(project_id=_PROJECT, status=None)

    assert len(rows) == 6


async def test_offset_skips_from_the_front(seeded):
    ids = await _ids(seeded)

    rows = await seeded.list_task_rows(project_id=_PROJECT, status=None, limit=2, offset=2)

    assert [int(r["id"]) for r in rows] == ids[2:4]


async def test_offset_without_limit_runs_and_skips(seeded):
    """MariaDB has no bare OFFSET; the maximal-LIMIT idiom must actually execute."""
    ids = await _ids(seeded)

    rows = await seeded.list_task_rows(project_id=_PROJECT, status=None, offset=4)

    assert [int(r["id"]) for r in rows] == ids[4:]


async def test_paging_composes_with_the_status_filter(seeded):
    """The clause is appended after an expanding bindparam — order matters."""
    rows = await seeded.list_task_rows(
        project_id=_PROJECT, status=["pending", "in_progress"], limit=3
    )

    assert len(rows) == 3


async def test_limit_zero_returns_no_rows(seeded):
    """``0`` is a stated cap, not a synonym for "unlimited"."""
    rows = await seeded.list_task_rows(project_id=_PROJECT, status=None, limit=0)

    assert rows == []


async def test_offset_past_the_end_returns_no_rows(seeded):
    rows = await seeded.list_task_rows(project_id=_PROJECT, status=None, limit=5, offset=99)

    assert rows == []


async def test_the_cross_project_reader_pages_too(seeded):
    rows = await seeded.list_task_rows_all_projects(status=None, limit=2)

    assert len(rows) == 2


async def test_a_negative_limit_is_rejected_not_sent(seeded):
    with pytest.raises(ValueError, match="limit"):
        await seeded.list_task_rows(project_id=_PROJECT, status=None, limit=-1)
