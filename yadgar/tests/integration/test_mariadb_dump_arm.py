"""Engine-#2 car F: the ``mariadb_dump`` op against a REAL MariaDB.

Marked ``integration`` — the default addopts exclude it
(``-m 'not integration and not e2e'``). Run explicitly::

    pytest yadgar/tests/integration/test_mariadb_dump_arm.py -m integration -v

WHY THIS EXISTS AS AN INTEGRATION TEST
--------------------------------------
The unit tests fake ``subprocess.run``, so they pin the op's CONTRACT — where
the artifact lands, what a failure does — but not that ``mariadb-dump`` accepts
the argv we build or that the option file authenticates. Two things only a real
server settles:

  * ``--defaults-file`` must come FIRST in argv; mariadb-dump rejects it in any
    other position, and a wrong order fails at runtime with green unit tests;
  * the artifact actually carries the schema. For a table whose target state is
    ZERO ROWS (ADR-0203), "the dump succeeded" and "the dump is a header" differ
    by a few hundred bytes — asserting the ``CREATE TABLE`` is the only check
    that separates them, and it is the shape of the 2026-06-16 failure (a
    partial restore that PASSED a ``>=`` check).

The scratch container mirrors car A's shape only where it matters — a database,
a password-auth app account, a unix socket — and never touches the live data
root at ``~/.local/share/yadgar``.

Deliberately does NOT import the ``sql`` extra: the op shells a binary, so
``asyncmy``/``sqlalchemy`` are irrelevant to it. The skips here are the
container runtime and the client binary, not the extra.
"""

from __future__ import annotations

import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from yadgar.backend.admin_exec import backup_sql
from yadgar.tests.integration._podman import podman_env

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("engine2_mariadb")]

_IMAGE = "docker.io/library/mariadb:11.4"
_DB = "yadgar"
_APP_USER = "yadgar_app"
_APP_PASS = "carf-integration-password"
_BOOT_TIMEOUT_SEC = 180.0


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


def _mariadb_client(runtime: str, name: str, sql: str) -> subprocess.CompletedProcess[str]:
    """Run one statement as the APP user over the container's socket.

    Deliberately NOT root. The image's entrypoint runs a temporary server during
    datadir init, and ``mariadb-admin ping`` answers on it BEFORE the accounts
    are provisioned — a root probe therefore reports "ready" while root's
    password does not exist yet, which made this fixture flaky. A successful app
    connection is the readiness signal that actually implies the accounts and the
    database are in place, and it is the same account the dump uses.
    """
    return subprocess.run(
        [
            runtime, "exec", name, "mariadb", "--socket=/sockets/mysqld.sock",
            f"-u{_APP_USER}", f"-p{_APP_PASS}", _DB, "-e", sql,
        ],
        capture_output=True, text=True, check=False, timeout=60, env=podman_env(),
    )  # fmt: skip


def _await_ready(runtime: str, name: str) -> None:
    """Poll until the app account can reach the target database, or fail the fixture."""
    deadline = time.monotonic() + _BOOT_TIMEOUT_SEC
    last = ""
    while time.monotonic() < deadline:
        probe = _mariadb_client(runtime, name, "SELECT 1")
        if probe.returncode == 0:
            return
        last = (probe.stderr or probe.stdout).strip()
        time.sleep(2.0)
    pytest.fail(f"MariaDB never became ready within {_BOOT_TIMEOUT_SEC}s: {last}")


def _remove_socket_dir(runtime: str, sock_dir: Path) -> None:
    """Delete the mount dir, including what the container's uid took ownership of.

    Mirrors ``test_mariadb_connection.py``: the image chowns the socket mount to
    its own ``mysql`` user, which under rootless podman is a SUBUID the host user
    cannot rmdir. ``podman unshare`` re-enters the namespace where it can.
    """
    shutil.rmtree(sock_dir, ignore_errors=True)
    if sock_dir.exists() and Path(runtime).name == "podman":
        subprocess.run(
            [runtime, "unshare", "rm", "-rf", str(sock_dir)],
            capture_output=True, check=False, timeout=60, env=podman_env(),
        )  # fmt: skip


@pytest.fixture(scope="module")
def live_mariadb():
    """Scratch MariaDB over a unix socket, with a ``config`` table; torn down after.

    NOT ``tmp_path``: a unix socket path caps at ~107 bytes and pytest's tmp
    dirs are long enough to blow it.
    """
    runtime = shutil.which("podman") or shutil.which("docker")
    if runtime is None:
        pytest.skip("docker/podman not available on this host")
    if shutil.which("mariadb-dump") is None:
        pytest.skip("mariadb-dump not available on this host")

    name = f"yadgar-carf-mdb-{uuid.uuid4().hex[:8]}"
    sock_dir = Path(f"/tmp/ymdbf-{uuid.uuid4().hex[:8]}")
    sock_dir.mkdir(mode=0o777, parents=True)
    sock_dir.chmod(0o777)  # mkdir mode is umask-masked
    socket_path = sock_dir / "mysqld.sock"

    started = subprocess.run(
        [
            runtime, "run", "-d", "--name", name,
            "--memory", "512m", "--cpus", "1",
            "-e", "MARIADB_ROOT_PASSWORD=carf-root",
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
        # Stand in for car D's Alembic chain: the arm under test is the DUMP,
        # and creating the table with the client avoids dragging the `sql` extra
        # (absent from the yadgar-ci image) into a test that does not need it.
        created = _mariadb_client(
            runtime,
            name,
            "CREATE TABLE config (id BIGINT AUTO_INCREMENT PRIMARY KEY, k VARCHAR(255))",
        )
        assert created.returncode == 0, created.stderr

        cnf = sock_dir / "client.cnf"
        cnf.write_text(_cnf_body(str(socket_path)), encoding="utf-8")
        cnf.chmod(0o600)
        yield {"cnf": cnf, "dir": sock_dir}
    finally:
        # -v: the image declares /var/lib/mysql a VOLUME, so every run creates an
        # anonymous one. Without this each run leaks a datadir-sized volume.
        subprocess.run(
            [runtime, "rm", "-f", "-v", name],
            capture_output=True, check=False, timeout=120, env=podman_env(),
        )  # fmt: skip
        _remove_socket_dir(runtime, sock_dir)


def test_dump_against_a_real_server_carries_the_config_schema(live_mariadb, tmp_path, monkeypatch):
    """End-to-end: real argv, real auth, and the zero-row table is IN the artifact."""
    monkeypatch.setenv("YADGAR_MARIADB_CLIENT_CNF", str(live_mariadb["cnf"]))
    monkeypatch.setenv("YADGAR_SQL_BACKUP_DIR", str(tmp_path / "dumps"))

    result = backup_sql.mariadb_dump({"label": "nightly-quiesce"})

    assert result["ok"] is True
    assert result["database"] == _DB
    written = tmp_path / "dumps" / result["filename"]
    assert written.is_file()
    body = written.read_text(encoding="utf-8")
    # The whole point: a zero-row table means size cannot distinguish a good
    # dump from a header, so assert the schema by name.
    assert "CREATE TABLE `config`" in body
    assert "CREATE DATABASE" in body and _DB in body


def test_dump_with_a_bad_credential_hard_fails_and_leaves_nothing(
    live_mariadb, tmp_path, monkeypatch
):
    """The control: wrong password → RuntimeError and NO artifact left behind.

    Without this, "it produced a file" would not prove the credential path was
    exercised at all.
    """
    bad = tmp_path / "bad.cnf"
    bad.write_text(
        _cnf_body(str(live_mariadb["dir"] / "mysqld.sock")).replace(_APP_PASS, "wrong"),
        encoding="utf-8",
    )
    bad.chmod(0o600)
    monkeypatch.setenv("YADGAR_MARIADB_CLIENT_CNF", str(bad))
    monkeypatch.setenv("YADGAR_SQL_BACKUP_DIR", str(tmp_path / "dumps2"))

    with pytest.raises(RuntimeError):
        backup_sql.mariadb_dump({})

    assert list((tmp_path / "dumps2").glob("*.sql")) == []
