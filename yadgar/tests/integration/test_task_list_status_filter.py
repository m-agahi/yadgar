"""``task_list``'s status filter, EXECUTED against a REAL MariaDB (L10).

Marked ``integration`` — the default addopts exclude it
(``-m 'not integration and not e2e'``). Run explicitly::

    pytest yadgar/tests/integration/test_task_list_status_filter.py -m integration -v

WHY THIS FILE EXISTS AT ALL
---------------------------
``list_task_rows`` wrote ``" AND status IN (:status)"`` and then bound
``:status`` with SQLAlchemy's ``bindparam(..., expanding=True)``. The expanding
bindparam renders its OWN parenthesised placeholder list at execution, so the
statement that reached the server was ``status IN ((%s, %s))``. ``(a, b)`` is a
ROW CONSTRUCTOR in MariaDB, so the clause became
``status = ROW('pending','in_progress')`` and the server answered::

    (asyncmy.errors.OperationalError) (4078, "Illegal parameter data types
    varchar and row for operation '='")

A single status never tripped it — ``(x)`` is just ``x`` — which is why the
one shape that always failed was D37's open-only default, the shape every bare
``task_list(project_id=...)`` call uses.

WHY IT IS A LIVE TEST AND NOT A COMPILED-SQL ASSERTION
-----------------------------------------------------
The test this replaces (``_shared``-style source-string matching in
``yadgar/tests/core/test_task_list_bindparam.py``) asserted the literal
``"status IN (:status)"`` as a REQUIREMENT — it pinned the bug and passed for
the bug's entire life without ever compiling, let alone executing, anything.
A compiled-SQL string assertion would not have been much better: with
``expanding=True`` SQLAlchemy defers rendering the placeholder list until bind
values are supplied, so compiling without values inspects an artifact that
never reaches the database — the same class of error.

Only running the statement, with TWO statuses, against a real server settles
it: the query either returns rows or the driver raises 4078.
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
_APP_PASS = "l10-integration-password"
_BOOT_TIMEOUT_SEC = 180.0
_MOUNT_VISIBLE_TIMEOUT_SEC = 30.0

_PROJECT = "m-agahi/yadgar-l10"
_OTHER_PROJECT = "m-agahi/other-l10"


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

    name = f"yadgar-l10-mdb-{uuid.uuid4().hex[:8]}"
    sock_dir = make_socket_dir(runtime, image=_IMAGE, prefix="ymdbl")
    socket_path = sock_dir / "mysqld.sock"

    started = subprocess.run(
        [
            runtime, "run", "-d", "--name", name,
            "--memory", "512m", "--cpus", "1",
            "-e", "MARIADB_ROOT_PASSWORD=l10-root",
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
    """A migrated schema holding one row per status, across two projects.

    ``create_task_row`` is registry-guarded (C6), so the ``project`` rows are
    written first — the FK is absent on ``task.project_id`` (002) but the
    guard is not.
    """
    await upgrade_to_head(engine.engine)
    try:
        for key in (_PROJECT, _OTHER_PROJECT):
            await engine.create_project_row(key=key, kind="git")
        for status in ("pending", "in_progress", "completed", "archived"):
            await engine.create_task_row(
                project_id=_PROJECT,
                title=f"{status} task",
                status=status,
            )
        await engine.create_task_row(
            project_id=_OTHER_PROJECT,
            title="other project pending task",
            status="pending",
        )
        yield engine
    finally:
        await _reset_to_base(engine)


# ── the two-status filter, executed ──────────────────────────────────────────


async def test_two_status_filter_executes_and_returns_only_the_open_rows(seeded):
    """D37's open-only default. THE regression: this raised 4078 before the fix."""
    rows = await seeded.list_task_rows(
        project_id=_PROJECT,
        status=["pending", "in_progress"],
    )

    assert sorted(r["status"] for r in rows) == ["in_progress", "pending"]


async def test_two_status_filter_still_scopes_to_the_project(seeded):
    """The IN-clause fix must not widen the project scope."""
    rows = await seeded.list_task_rows(
        project_id=_PROJECT,
        status=["pending", "in_progress"],
    )

    assert [r["title"] for r in rows] == ["pending task", "in_progress task"]


async def test_single_status_filter_still_executes(seeded):
    """The control: one status worked THROUGH the bug (``(x)`` ≡ ``x``).

    Without it, a passing two-status test could not distinguish "the paren fix
    worked" from "the expanding bindparam stopped working entirely".
    """
    rows = await seeded.list_task_rows(project_id=_PROJECT, status=["completed"])

    assert [r["status"] for r in rows] == ["completed"]


async def test_three_status_filter_executes(seeded):
    """Two is where the row constructor starts biting; three must work too."""
    rows = await seeded.list_task_rows(
        project_id=_PROJECT,
        status=["pending", "in_progress", "completed"],
    )

    assert len(rows) == 3


async def test_no_status_filter_returns_every_row_for_the_project(seeded):
    """``None`` = no filter — the ``include_closed=True`` path."""
    rows = await seeded.list_task_rows(project_id=_PROJECT, status=None)

    assert len(rows) == 4


# ── the cross-project reader carries the same clause ─────────────────────────


async def test_all_projects_two_status_filter_executes(seeded):
    """``list_task_rows_all_projects`` had the identical defect at ``:397``."""
    rows = await seeded.list_task_rows_all_projects(status=["pending", "in_progress"])

    # 2 open rows in _PROJECT + 1 pending row in _OTHER_PROJECT
    assert len(rows) == 3
    assert {r["project_id"] for r in rows} == {_PROJECT, _OTHER_PROJECT}


async def test_all_projects_no_status_filter_executes(seeded):
    rows = await seeded.list_task_rows_all_projects(status=None)

    assert len(rows) == 5


# ── the projection (``summary``) ─────────────────────────────────────────────
#
# Asserted against a real server rather than the SELECT string: the point of
# the projection is which keys COME BACK, and only the driver settles that.

_FULL_KEYS = {
    "id",
    "project_id",
    "title",
    "status",
    "state",
    "active_form",
    "plan_path",
    "body_slug",
    "completed_at",
    "created_at",
    "updated_at",
}
_SUMMARY_KEYS = {"id", "title", "status"}


async def test_summary_projection_returns_exactly_id_title_status(seeded):
    rows = await seeded.list_task_rows(
        project_id=_PROJECT,
        status=["pending", "in_progress"],
        summary=True,
    )

    assert rows, "the projection must not change which rows come back"
    assert all(set(r) == _SUMMARY_KEYS for r in rows)


async def test_summary_projection_does_not_change_the_row_count(seeded):
    """Width only. The lean shape is not a cap."""
    lean = await seeded.list_task_rows(project_id=_PROJECT, status=None, summary=True)
    full = await seeded.list_task_rows(project_id=_PROJECT, status=None, summary=False)

    assert len(lean) == len(full) == 4
    assert [r["id"] for r in lean] == [r["id"] for r in full]


async def test_full_projection_is_the_default_and_carries_all_eleven(seeded):
    """The default is FULL — ``nightly_sweep`` passes no ``summary`` kwarg and
    reads ``body_slug`` / ``completed_at`` / ``project_id`` off these rows."""
    rows = await seeded.list_task_rows(project_id=_PROJECT, status=["pending"])

    assert rows
    assert all(set(r) == _FULL_KEYS for r in rows)


async def test_all_projects_summary_projection(seeded):
    rows = await seeded.list_task_rows_all_projects(status=None, summary=True)

    assert len(rows) == 5
    assert all(set(r) == _SUMMARY_KEYS for r in rows)


async def test_all_projects_default_keeps_project_id(seeded):
    """``nightly_sweep._resolve_projects`` derives the whole sweep set from it."""
    rows = await seeded.list_task_rows_all_projects()

    assert {r["project_id"] for r in rows} == {_PROJECT, _OTHER_PROJECT}


async def test_get_task_row_is_never_narrowed(seeded):
    """The single-row read stays full — it has no ``summary`` switch at all."""
    listed = await seeded.list_task_rows(project_id=_PROJECT, status=["pending"], summary=True)
    row = await seeded.get_task_row(int(listed[0]["id"]))

    assert row is not None
    assert set(row) == _FULL_KEYS
