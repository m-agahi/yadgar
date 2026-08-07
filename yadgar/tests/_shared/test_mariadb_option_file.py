"""Engine-#2 client option-file parsing + default-path resolution (car C).

PURE tests: this module imports ONLY ``yadgar._shared.storage.sql.config``,
which carries no third-party import at all. That is deliberate — the yadgar-ci
image bakes ``--extra test --extra ml`` (Dockerfile.ci:116) and has no
auto-sync pipeline, so anything importing ``sqlalchemy`` / ``asyncmy`` cannot
run there yet. Credential parsing is the half that CAN, so it lives apart from
the engine module and is tested here without either.

The file under test is written by ``entrypoint-backend.sh``
(``_bootstrap_mariadb_accounts``) as a 0600 MySQL option file in the MariaDB
datadir; these tests pin the exact shape that script emits.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from yadgar._shared.storage.sql.config import (
    MariaClientConfig,
    default_option_file_path,
    read_client_option_file,
)

# Byte-for-byte the heredoc entrypoint-backend.sh writes (car A).
_ENTRYPOINT_CNF = """\
[client]
socket = /data/mariadb/mysqld.sock
user = yadgar_app
password = deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef
database = yadgar
"""


def _write_cnf(tmp_path: Path, body: str = _ENTRYPOINT_CNF) -> Path:
    path = tmp_path / "client.cnf"
    path.write_text(body, encoding="utf-8")
    return path


# ── parsing ──────────────────────────────────────────────────────────────


def test_reads_user_socket_and_database_from_entrypoint_file(tmp_path):
    cfg = read_client_option_file(_write_cnf(tmp_path))

    assert cfg.user == "yadgar_app"
    assert cfg.database == "yadgar"
    assert cfg.unix_socket == "/data/mariadb/mysqld.sock"
    assert cfg.option_file == tmp_path / "client.cnf"


def test_password_is_never_parsed_into_the_config_object(tmp_path):
    """The password reaches the driver via ``read_default_file``, never via us.

    Keeping it out of the dataclass keeps it out of reprs, tracebacks, logs and
    any URL string — the whole reason car A put it in a 0600 option file
    instead of an env var.
    """
    cfg = read_client_option_file(_write_cnf(tmp_path))

    assert not hasattr(cfg, "password")
    assert "deadbeef" not in repr(cfg)


def test_user_name_is_not_hardcoded(tmp_path):
    """A renamed MARIADB_APP_USER must be picked up, not assumed."""
    body = _ENTRYPOINT_CNF.replace("yadgar_app", "someone_else")
    cfg = read_client_option_file(_write_cnf(tmp_path, body))

    assert cfg.user == "someone_else"


def test_tolerates_bare_flags_and_extra_groups(tmp_path):
    """Real my.cnf files carry valueless flags and non-[client] groups."""
    body = (
        "[mysqld]\nskip-networking\n\n"
        "[client]\n"
        "socket = /tmp/s.sock\n"
        "user = u\n"
        "password = p\n"
        "database = d\n"
    )
    cfg = read_client_option_file(_write_cnf(tmp_path, body))

    assert (cfg.user, cfg.database, cfg.unix_socket) == ("u", "d", "/tmp/s.sock")


def test_missing_file_raises_filenotfound(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_client_option_file(tmp_path / "absent.cnf")


def test_missing_client_group_raises_valueerror(tmp_path):
    with pytest.raises(ValueError, match="client"):
        read_client_option_file(_write_cnf(tmp_path, "[mysqld]\nskip-networking\n"))


@pytest.mark.parametrize("missing", ["socket", "user", "database"])
def test_missing_required_key_raises_valueerror_naming_it(tmp_path, missing):
    body = "\n".join(line for line in _ENTRYPOINT_CNF.splitlines() if not line.startswith(missing))
    with pytest.raises(ValueError, match=missing):
        read_client_option_file(_write_cnf(tmp_path, body))


# ── default path resolution ──────────────────────────────────────────────


def test_explicit_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("YADGAR_MARIADB_CLIENT_CNF", str(tmp_path / "x.cnf"))
    monkeypatch.setenv("MARIADB_DATA_DIR", "/ignored")
    assert default_option_file_path() == tmp_path / "x.cnf"


def test_datadir_env_yields_sibling_client_cnf(monkeypatch, tmp_path):
    monkeypatch.delenv("YADGAR_MARIADB_CLIENT_CNF", raising=False)
    monkeypatch.setenv("MARIADB_DATA_DIR", str(tmp_path / "mariadb"))
    assert default_option_file_path() == tmp_path / "mariadb" / "client.cnf"


def test_surreal_data_root_yields_mariadb_sibling(monkeypatch, tmp_path):
    """The datadir is a SIBLING of surreal_db under the shared data root."""
    monkeypatch.delenv("YADGAR_MARIADB_CLIENT_CNF", raising=False)
    monkeypatch.delenv("MARIADB_CLIENT_CNF", raising=False)
    monkeypatch.delenv("MARIADB_DATA_DIR", raising=False)
    monkeypatch.setenv("SURREAL_DATA_ROOT", str(tmp_path))
    assert default_option_file_path() == tmp_path / "mariadb" / "client.cnf"


def test_falls_back_to_a_sibling_of_the_surrealkv_store(monkeypatch, tmp_path):
    for var in (
        "YADGAR_MARIADB_CLIENT_CNF",
        "MARIADB_CLIENT_CNF",
        "MARIADB_DATA_DIR",
        "SURREAL_DATA_ROOT",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("YADGAR_DB_PATH", str(tmp_path / "surreal_db"))

    assert default_option_file_path() == tmp_path / "mariadb" / "client.cnf"


def test_config_is_frozen(tmp_path):
    cfg = read_client_option_file(_write_cnf(tmp_path))
    with pytest.raises((AttributeError, TypeError)):
        cfg.user = "other"  # type: ignore[misc]


def test_dataclass_is_constructible_directly():
    """Tests and car D build one without touching the filesystem."""
    cfg = MariaClientConfig(
        option_file=Path("/nowhere/client.cnf"),
        user="u",
        database="d",
        unix_socket="/nowhere/mysqld.sock",
    )
    assert cfg.database == "d"
