"""Engine-#2 car F: the ``mariadb_dump`` backend admin op.

WHY THE DUMP RUNS IN THE BACKEND CONTAINER
------------------------------------------
Car C's ``client.cnf`` carries a CONTAINER-ABSOLUTE socket path
(``/data/mariadb/mysqld.sock``) and ``MariaStorageEngine`` construction is
connectionless, so a host-side ``_init_sql_storage()`` hands back a handle that
can never connect and fails SILENTLY. mariadbd also runs ``--skip-networking``
(ADR-0212), and the host has no ``mariadb-dump`` binary at all. Running the dump
as an admin op puts it in the one process whose filesystem namespace makes both
the socket path and the option file true.

The same trap has a second costume: the DESTINATION. An absolute host path in
the payload would resolve inside the container's namespace, land in its
writable layer, and report success while the host sees nothing. So the op
resolves its own destination container-side and REPORTS the basename; the
payload carries a label, never a path. These tests pin that.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from yadgar.backend.admin_exec import admin_ops, backup_sql


def _write_cnf(tmp_path: Path, *, socket: str = "/data/mariadb/mysqld.sock") -> Path:
    datadir = tmp_path / "mariadb"
    datadir.mkdir(parents=True, exist_ok=True)
    cnf = datadir / "client.cnf"
    cnf.write_text(
        "\n".join(
            [
                "[client]",
                f"socket = {socket}",
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


class _FakeCompleted:
    def __init__(self, returncode: int = 0, stderr: bytes = b"") -> None:
        self.returncode = returncode
        self.stderr = stderr


def test_mariadb_dump_op_registered():
    """The op is on the /admin dispatch table — the route validates against it."""
    assert "mariadb_dump" in admin_ops()


def test_entrypoint_exports_the_vars_the_option_file_ladder_reads():
    """The backend entrypoint must EXPORT the engine-#2 paths, not just assign them.

    ``default_option_file_path()`` resolves ``MARIADB_CLIENT_CNF`` /
    ``MARIADB_DATA_DIR`` / ``SURREAL_DATA_ROOT`` from the ENVIRONMENT. Car A set
    them as shell locals under a comment saying the client side "needs no env
    plumbing at all" — true of asyncmy's transport, false of finding the file in
    the first place. Unexported, the ladder falls through to
    ``_paths.DB_PATH.parent/mariadb/client.cnf``, and the backend container sets
    neither ``YADGAR_DATA_DIR`` nor ``YADGAR_DB_PATH`` (only the CORE container
    gets ``-e YADGAR_DATA_DIR=/data``), so it lands under ``$HOME`` and misses
    ``/data/mariadb/client.cnf``. Cars C, D and F all die on that one line.
    """
    root = Path(__file__).resolve().parents[3]
    body = (root / "entrypoint-backend.sh").read_text(encoding="utf-8")
    exported = [ln for ln in body.splitlines() if ln.strip().startswith("export ")]
    for var in ("SURREAL_DATA_ROOT", "MARIADB_DATA_DIR", "MARIADB_CLIENT_CNF"):
        assert any(var in ln for ln in exported), f"{var} must be exported, not a shell local"


def test_dump_dir_is_a_sibling_of_the_datadir_under_the_shared_root(monkeypatch, tmp_path):
    """Destination is resolved container-side from the data root, never from payload."""
    monkeypatch.setenv("SURREAL_DATA_ROOT", str(tmp_path))
    assert backup_sql._dump_dir(tmp_path / "mariadb" / "client.cnf") == (
        tmp_path / "backups" / "mariadb"
    )


def test_dump_dir_falls_back_to_the_option_files_grandparent(monkeypatch, tmp_path):
    """No SURREAL_DATA_ROOT → derive the root from where the option file was found."""
    monkeypatch.delenv("SURREAL_DATA_ROOT", raising=False)
    monkeypatch.setenv("YADGAR_SQL_BACKUP_DIR", "")
    assert backup_sql._dump_dir(tmp_path / "mariadb" / "client.cnf") == (
        tmp_path / "backups" / "mariadb"
    )


def test_dump_writes_and_reports_a_basename_not_a_caller_path(monkeypatch, tmp_path):
    """Happy path: op writes under its OWN root and returns the basename to match on."""
    cnf = _write_cnf(tmp_path)
    monkeypatch.setenv("YADGAR_MARIADB_CLIENT_CNF", str(cnf))
    monkeypatch.setenv("SURREAL_DATA_ROOT", str(tmp_path))

    def _fake_run(cmd, **kwargs):
        # mariadb-dump writes to the handle the op opened for it.
        kwargs["stdout"].write(b"-- dump\nCREATE TABLE `config` (id INT);\n")
        return _FakeCompleted(0)

    with (
        patch.object(backup_sql.shutil, "which", return_value="/usr/bin/mariadb-dump"),
        patch.object(backup_sql.subprocess, "run", side_effect=_fake_run),
    ):
        result = backup_sql.mariadb_dump({"label": "nightly-quiesce"})

    assert result["ok"] is True
    assert result["database"] == "yadgar"
    assert result["bytes"] > 0
    written = tmp_path / "backups" / "mariadb" / result["filename"]
    assert written.is_file()
    assert "nightly-quiesce" in result["filename"]
    # The op must not echo a caller-supplied path — the payload had none.
    assert Path(result["filename"]).name == result["filename"]


def test_dump_ignores_any_caller_supplied_destination(monkeypatch, tmp_path):
    """A path in the payload is INERT — the container's own root always wins."""
    cnf = _write_cnf(tmp_path)
    monkeypatch.setenv("YADGAR_MARIADB_CLIENT_CNF", str(cnf))
    monkeypatch.setenv("SURREAL_DATA_ROOT", str(tmp_path))
    hostile = tmp_path / "elsewhere"

    def _fake_run(cmd, **kwargs):
        kwargs["stdout"].write(b"CREATE TABLE `config` (id INT);\n")
        return _FakeCompleted(0)

    with (
        patch.object(backup_sql.shutil, "which", return_value="/usr/bin/mariadb-dump"),
        patch.object(backup_sql.subprocess, "run", side_effect=_fake_run),
    ):
        result = backup_sql.mariadb_dump({"label": "x", "path": str(hostile / "evil.sql")})

    assert not hostile.exists()
    assert (tmp_path / "backups" / "mariadb" / result["filename"]).is_file()


def test_dump_hard_fails_when_the_binary_is_absent(monkeypatch, tmp_path):
    """No mariadb-dump → RuntimeError. A soft return here is a silent empty backup."""
    cnf = _write_cnf(tmp_path)
    monkeypatch.setenv("YADGAR_MARIADB_CLIENT_CNF", str(cnf))
    monkeypatch.setenv("SURREAL_DATA_ROOT", str(tmp_path))

    with (
        patch.object(backup_sql.shutil, "which", return_value=None),
        pytest.raises(RuntimeError, match="mariadb-dump"),
    ):
        backup_sql.mariadb_dump({})


def test_dump_hard_fails_and_removes_the_partial_on_nonzero_exit(monkeypatch, tmp_path):
    """A failed dump must leave NO artifact — a partial file reads as a good backup."""
    cnf = _write_cnf(tmp_path)
    monkeypatch.setenv("YADGAR_MARIADB_CLIENT_CNF", str(cnf))
    monkeypatch.setenv("SURREAL_DATA_ROOT", str(tmp_path))

    def _fake_run(cmd, **kwargs):
        kwargs["stdout"].write(b"-- half a dump")
        return _FakeCompleted(2, b"mariadb-dump: Got error 1045")

    with (
        patch.object(backup_sql.shutil, "which", return_value="/usr/bin/mariadb-dump"),
        patch.object(backup_sql.subprocess, "run", side_effect=_fake_run),
        pytest.raises(RuntimeError, match="1045"),
    ):
        backup_sql.mariadb_dump({})

    assert list((tmp_path / "backups" / "mariadb").glob("*.sql")) == []


def test_dump_hard_fails_on_an_empty_artifact(monkeypatch, tmp_path):
    """Exit 0 with a zero-byte file is the silent-empty-backup shape. Reject it."""
    cnf = _write_cnf(tmp_path)
    monkeypatch.setenv("YADGAR_MARIADB_CLIENT_CNF", str(cnf))
    monkeypatch.setenv("SURREAL_DATA_ROOT", str(tmp_path))

    with (
        patch.object(backup_sql.shutil, "which", return_value="/usr/bin/mariadb-dump"),
        patch.object(backup_sql.subprocess, "run", return_value=_FakeCompleted(0)),
        pytest.raises(RuntimeError, match="empty"),
    ):
        backup_sql.mariadb_dump({})

    assert list((tmp_path / "backups" / "mariadb").glob("*.sql")) == []


def test_dump_command_reads_credentials_from_the_option_file(monkeypatch, tmp_path):
    """--defaults-file, not a URL: the password never enters argv or a repr."""
    cnf = _write_cnf(tmp_path)
    monkeypatch.setenv("YADGAR_MARIADB_CLIENT_CNF", str(cnf))
    monkeypatch.setenv("SURREAL_DATA_ROOT", str(tmp_path))
    seen: dict[str, list[str]] = {}

    def _fake_run(cmd, **kwargs):
        seen["cmd"] = list(cmd)
        kwargs["stdout"].write(b"CREATE TABLE `config` (id INT);\n")
        return _FakeCompleted(0)

    with (
        patch.object(backup_sql.shutil, "which", return_value="/usr/bin/mariadb-dump"),
        patch.object(backup_sql.subprocess, "run", side_effect=_fake_run),
    ):
        backup_sql.mariadb_dump({})

    cmd = seen["cmd"]
    assert f"--defaults-file={cnf}" in cmd
    assert "--single-transaction" in cmd
    assert "yadgar" in cmd
    assert not any("hunter2" in part for part in cmd)
