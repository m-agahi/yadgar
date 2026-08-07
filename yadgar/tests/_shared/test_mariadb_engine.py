"""``MariaStorageEngine`` construction — no database, no connection (car C).

Needs ``sqlalchemy`` (the ``sql`` extra), so the whole module skips when it is
absent; the yadgar-ci image bakes only ``--extra test --extra ml`` today. Skip
reason registered in ``yadgar/tests/skip_inventory.json``.

Construction is CONNECTIONLESS on purpose. ``init_engines`` is sync and, on the
backend boot path, runs inside a worker thread (``asyncio.to_thread`` →
``_start_queue_drainer`` → ``_ensure_recall_engines``). Driving a coroutine
from there would need a private event loop, and ``AsyncAdaptedQueuePool`` would
then hold a connection bound to a loop that is closed the moment the thread
returns. Every later connect from the real loop would fail. So the constructor
only builds the ``AsyncEngine``; ``verify()`` is a separate coroutine awaited
from an async caller.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy", reason="sqlalchemy not installed (sql extra)")

from yadgar._shared.storage.sql import MariaStorageEngine  # noqa: E402
from yadgar._shared.storage.sql.config import MariaClientConfig  # noqa: E402

_PASSWORD = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
_CNF = f"""\
[client]
socket = /nonexistent/mariadb/mysqld.sock
user = yadgar_app
password = {_PASSWORD}
database = yadgar
"""


@pytest.fixture
def cfg() -> MariaClientConfig:
    return MariaClientConfig(
        option_file=Path("/nonexistent/mariadb/client.cnf"),
        user="yadgar_app",
        database="yadgar",
        unix_socket="/nonexistent/mariadb/mysqld.sock",
    )


def test_construction_does_not_connect(cfg):
    """The socket does not exist; constructing must still succeed."""
    engine = MariaStorageEngine(cfg)
    assert engine.engine is not None


def test_url_names_the_async_driver(cfg):
    assert MariaStorageEngine(cfg).url.startswith("mysql+asyncmy://")


def test_url_carries_user_and_database_but_no_host(cfg):
    url = MariaStorageEngine(cfg).url
    assert "yadgar_app" in url
    assert url.endswith("/yadgar")


def test_password_never_appears_in_url_or_repr(cfg, tmp_path):
    """The driver reads it from the 0600 option file; we never handle it."""
    real_cnf = tmp_path / "client.cnf"
    real_cnf.write_text(_CNF, encoding="utf-8")
    engine = MariaStorageEngine.from_option_file(real_cnf)

    assert _PASSWORD not in engine.url
    assert _PASSWORD not in repr(engine)
    assert _PASSWORD not in str(engine.connect_args)


def test_connect_args_delegate_credentials_to_the_option_file(cfg):
    args = MariaStorageEngine(cfg).connect_args

    assert args["read_default_file"] == "/nonexistent/mariadb/client.cnf"
    assert args["read_default_group"] == "client"
    assert args["unix_socket"] == "/nonexistent/mariadb/mysqld.sock"
    assert "password" not in args


def test_from_option_file_parses_and_builds(tmp_path):
    cnf = tmp_path / "client.cnf"
    cnf.write_text(_CNF, encoding="utf-8")

    engine = MariaStorageEngine.from_option_file(cnf)

    assert engine.config.user == "yadgar_app"
    assert engine.config.database == "yadgar"
    assert engine.connect_args["read_default_file"] == str(cnf)


def test_engine_is_a_sqlalchemy_async_engine(cfg):
    from sqlalchemy.ext.asyncio import AsyncEngine

    assert isinstance(MariaStorageEngine(cfg).engine, AsyncEngine)


def test_pool_pre_ping_is_on(cfg):
    """mysqld restarts independently of the app (entrypoint-backend.sh treats a
    MariaDB failure as non-fatal), so a pooled connection can outlive its
    server. Pre-ping is what stops that becoming a stale-handle error."""
    assert MariaStorageEngine(cfg).engine.pool._pre_ping is True


async def test_dispose_is_safe_without_ever_connecting(cfg):
    await MariaStorageEngine(cfg).dispose()
