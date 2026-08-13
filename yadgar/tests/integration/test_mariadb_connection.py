"""Engine-#2 connection against a REAL MariaDB (ADR-0195 car C).

Marked ``integration`` — the default addopts exclude it
(``-m 'not integration and not e2e'``). Run explicitly::

    pytest yadgar/tests/integration/test_mariadb_connection.py -m integration -v

WHY THIS EXISTS AS AN INTEGRATION TEST
--------------------------------------
The credential path cannot be proven without a server. It is composed of two
pieces of reasoning that only a live handshake settles:

  * ``asyncmy`` reads ``password`` out of car A's 0600 option file via
    ``read_default_file``, while the EXPLICIT ``user``/``database`` we pass
    still win (``asyncmy/connection.pyx:378-393`` — ``_config`` falls back to
    the file only when the argument is falsy);
  * therefore the password never enters a URL, a repr or this process.

``test_option_file_without_a_password_is_denied`` is the control
that makes the rest meaningful: strip the password line and the server answers
``using password: NO``. Without it, "it connected" would not prove WHERE the
credential came from.

Asserting ``CURRENT_USER()`` rather than only ``DATABASE()`` is the same idea —
``db=yadgar`` alone cannot distinguish "the option file was honoured" from
"something else authenticated us".

Car C connects, verifies and exposes the handle. It creates nothing: the empty
``list_tables()`` here is the engine-direct form of exit criterion 1 (car D
owns the ``config`` schema; ADR-0203 keeps it zero-row).
"""

from __future__ import annotations

import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy", reason="sqlalchemy not installed (sql extra)")

from yadgar._shared.storage.sql import (  # noqa: E402
    MariaStorageEngine,
    default_option_file_path,
)
from yadgar.tests.integration._podman import (  # noqa: E402
    container_is_running,
    container_logs,
    make_socket_dir,
    podman_env,
    remove_container_dir,
    select_container_runtime,
)

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("engine2_mariadb")]

# ADR-0205 measured idle RSS on 11.4; the client path is version-immaterial.
_IMAGE = "docker.io/library/mariadb:11.4"
_DB = "yadgar"
_APP_USER = "yadgar_app"
_APP_PASS = "carc-integration-password"
_BOOT_TIMEOUT_SEC = 180.0
# The socket file is the FIRST thing mysqld creates; if it has not reached our
# side of the mount by here, waiting out the full boot timeout cannot help.
_MOUNT_VISIBLE_TIMEOUT_SEC = 30.0


def _cnf_body(socket: str, *, password: str | None = _APP_PASS) -> str:
    """Byte-shape of the option file entrypoint-backend.sh writes (car A)."""
    lines = [
        "[client]",
        f"socket = {socket}",
        f"user = {_APP_USER}",
    ]
    if password is not None:
        lines.append(f"password = {password}")
    lines.append(f"database = {_DB}")
    return "\n".join(lines) + "\n"


@pytest.fixture(scope="module")
def live_mariadb():
    """Spin a scratch MariaDB reachable over a unix socket; tear it down.

    NOT ``tmp_path``: a unix socket path caps at ~107 bytes and pytest's tmp
    dirs are long enough to blow it. ``make_socket_dir`` keeps the leaf short.

    The directory comes from ``shared_mount_root`` rather than ``/tmp``: the
    socket is created by the container and consumed by THIS process, so the
    mount source has to mean the same directory on both sides. It does not
    under a dind-backed runner, where ``/tmp`` is the daemon's own — see the
    long note in ``_podman.py``.

    The container mirrors car A's shape where it matters — a database plus a
    password-auth app account scoped to it — and nothing else. It never touches
    the live data root.
    """
    runtime = select_container_runtime()
    if runtime is None:
        pytest.skip(
            "no working container runtime on this host "
            "(podman/docker absent, or present but non-functional)"
        )

    name = f"yadgar-carc-mdb-{uuid.uuid4().hex[:8]}"
    sock_dir = make_socket_dir(runtime, image=_IMAGE, prefix="ymdb")
    socket_path = sock_dir / "mysqld.sock"

    started = subprocess.run(
        [
            runtime, "run", "-d", "--name", name,
            "--memory", "512m", "--cpus", "1",
            "-e", "MARIADB_ROOT_PASSWORD=carc-root",
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
        yield {"cnf": cnf, "socket": socket_path, "dir": sock_dir}
    finally:
        # -v: the image declares /var/lib/mysql a VOLUME, so every run creates an
        # anonymous one. Without this each run leaks a datadir-sized volume.
        subprocess.run(
            [runtime, "rm", "-f", "-v", name],
            capture_output=True, check=False, timeout=120, env=podman_env(),
        )  # fmt: skip
        remove_container_dir(runtime, sock_dir, image=_IMAGE)


def _await_ready(cnf: Path, runtime: str, name: str, socket_path: Path) -> None:
    """Block until a real query succeeds. The socket appears before the server
    is usable — the official image runs a bootstrap server first, so waiting on
    the socket file alone races the account creation.

    Two fast exits keep a hopeless wait from consuming the whole timeout, which
    is what the pre-fix run did: 180s of retries against a socket that could
    never appear, four times over. If the container has DIED the retry cannot
    succeed, and if the socket file has not shown up long after the server is
    answering inside the container, the mount is not reaching us — both fail
    immediately and name what they observed.
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


# ── the connection ───────────────────────────────────────────────────────


async def test_lands_on_the_engine_two_database(engine):
    assert (await engine.verify())["database"] == _DB


async def test_server_authenticated_the_account_named_in_the_option_file(engine, live_mariadb):
    """``CURRENT_USER()`` is the SERVER's view — the discriminating assertion.

    Compared against the user parsed out of the option file, never a literal,
    so a renamed ``MARIADB_APP_USER`` is followed rather than assumed. The host
    part is ignored: car A grants on ``'localhost'``, this container on ``%``.
    """
    expected = MariaStorageEngine.from_option_file(live_mariadb["cnf"]).config.user
    assert (await engine.verify())["user"].split("@", 1)[0] == expected


async def test_schema_is_empty(engine):
    """Car C creates nothing. Car D owns the schema; ADR-0203 keeps it 0 rows."""
    assert await engine.list_tables() == []


async def test_password_never_reaches_the_url_or_repr(engine):
    await engine.verify()  # a real connection has happened by here
    assert _APP_PASS not in engine.url
    assert _APP_PASS not in repr(engine)
    assert _APP_PASS not in str(engine.connect_args)


async def test_option_file_without_a_password_is_denied(live_mariadb):
    """Control: proves ``read_default_file`` IS the credential source.

    Without it, a passing connection would not distinguish the option file from
    any other way the driver might have been authenticated.
    """
    from sqlalchemy.exc import OperationalError

    nopass = live_mariadb["dir"] / "nopass.cnf"
    nopass.write_text(_cnf_body(str(live_mariadb["socket"]), password=None), encoding="utf-8")
    eng = MariaStorageEngine.from_option_file(nopass)
    try:
        with pytest.raises(OperationalError, match="using password: NO"):
            await eng.verify()
    finally:
        await eng.dispose()


# ── the composition root, against a live server ──────────────────────────


async def test_default_path_resolution_reaches_the_written_option_file(monkeypatch, live_mariadb):
    monkeypatch.setenv("YADGAR_MARIADB_CLIENT_CNF", str(live_mariadb["cnf"]))
    assert default_option_file_path() == live_mariadb["cnf"]

    eng = MariaStorageEngine.from_option_file()  # no argument: uses the default
    try:
        assert (await eng.verify())["database"] == _DB
    finally:
        await eng.dispose()


async def test_composition_root_builds_a_working_engine(monkeypatch, live_mariadb):
    """``lifecycle._init_sql_storage`` — the exact call ``init_engines`` makes
    when the backend passes ``sql_storage=True``."""
    from yadgar._shared.runtime.lifecycle import _init_sql_storage

    monkeypatch.setenv("YADGAR_MARIADB_CLIENT_CNF", str(live_mariadb["cnf"]))
    eng = _init_sql_storage()
    assert eng is not None
    try:
        assert (await eng.verify())["database"] == _DB
        assert await eng.list_tables() == []
    finally:
        await eng.dispose()
