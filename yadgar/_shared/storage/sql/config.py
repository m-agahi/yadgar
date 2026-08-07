"""Engine-#2 client configuration — MySQL option-file parsing.

Deliberately reaches NOTHING from the ``sql`` extra. ``sqlalchemy`` and
``asyncmy`` live there (pyproject.toml) and the yadgar-ci image does not bake
it (``Dockerfile.ci:116`` = ``--extra test --extra ml``, no auto-sync
pipeline). Splitting the credential half out means it — and its tests — keep
working everywhere, and only genuine engine construction needs the extra.
Parsing itself is stdlib; the only yadgar imports are ``paths`` and
``observe``, both of which every other ``_shared`` module already carries.

WHERE THE FILE COMES FROM
-------------------------
``entrypoint-backend.sh`` (``_bootstrap_mariadb_accounts``, engine-#2 car A)
writes a 0600 MySQL option file into the MariaDB datadir::

    [client]
    socket   = ${MARIADB_DATA_DIR}/mysqld.sock
    user     = ${MARIADB_APP_USER}
    password = <32-byte hex>
    database = ${MARIADB_DB}

That is the ONLY place the app password exists. It is a file rather than an
env var so no new secret reaches the compose env block,
``/etc/yadgar/secrets.env`` or task 0122's unit renderers.

WHY THERE IS NO ``password`` FIELD HERE
---------------------------------------
There deliberately is not one. ``asyncmy`` reads ``password`` itself out of the
same file via ``read_default_file`` (``asyncmy/connection.pyx:370-393``), so the
secret goes straight from the 0600 file into the driver and never passes through
a Python object, a URL string, a repr or a traceback. We parse only the three
NON-secret keys the engine needs to address the right server and schema —
notably ``user``, so a renamed ``MARIADB_APP_USER`` is picked up rather than
hardcoded.
"""

from __future__ import annotations

import configparser
import os
from dataclasses import dataclass
from pathlib import Path

import yadgar._shared.paths as _paths
from yadgar._shared.observability.observe import observe

# The option-file group car A writes, and asyncmy's own default group.
CLIENT_GROUP = "client"

# Basename of the option file inside the MariaDB datadir (car A).
CLIENT_CNF_NAME = "client.cnf"

# Datadir basename — a SIBLING of ``surreal_db`` under the shared data root,
# never inside the surrealkv tree (ADR-0196: engine #2 leaves the vacuum
# pipeline, and every vacuum copytree/reap glob is ``surreal_db``-prefixed).
DATADIR_NAME = "mariadb"


@dataclass(frozen=True, slots=True)
class MariaClientConfig:
    """Non-secret half of the engine-#2 client credentials.

    Attributes:
        option_file: path handed to the driver as ``read_default_file`` — this
            is how the password reaches it.
        user: the app account (``MARIADB_APP_USER``, default ``yadgar_app``).
        database: the engine-#2 schema (``MARIADB_DB``, default ``yadgar``).
        unix_socket: mysqld's socket. There is no TCP listener at all — car A
            starts mysqld ``--skip-networking``, not even on loopback.
    """

    option_file: Path
    user: str
    database: str
    unix_socket: str


@observe(tier="hot")
def default_option_file_path() -> Path:
    """Resolve the option-file path, mirroring ``entrypoint-backend.sh``.

    Order (first hit wins):

    1. ``YADGAR_MARIADB_CLIENT_CNF`` — explicit override; the seam tests and
       ad-hoc tooling point at a scratch instance through it.
    2. ``MARIADB_CLIENT_CNF`` — the entrypoint's own variable name, honoured
       so an operator who overrides it there does not have to set a second one.
    3. ``MARIADB_DATA_DIR``/``client.cnf``.
    4. ``SURREAL_DATA_ROOT``/``mariadb``/``client.cnf`` — the container shape
       (``SURREAL_DATA_ROOT`` defaults to ``/data``).
    5. A sibling of the surrealkv store: ``DB_PATH.parent/mariadb/client.cnf``.
       This is the same sibling relationship as (4), expressed in the terms the
       Python side already knows, so a host-side process resolves it without
       any engine-#2 environment at all.
    """
    for var in ("YADGAR_MARIADB_CLIENT_CNF", "MARIADB_CLIENT_CNF"):
        override = os.environ.get(var, "").strip()
        if override:
            return Path(override).expanduser()

    datadir = os.environ.get("MARIADB_DATA_DIR", "").strip()
    if datadir:
        return Path(datadir).expanduser() / CLIENT_CNF_NAME

    data_root = os.environ.get("SURREAL_DATA_ROOT", "").strip()
    if data_root:
        return Path(data_root).expanduser() / DATADIR_NAME / CLIENT_CNF_NAME

    return Path(_paths.DB_PATH).expanduser().parent / DATADIR_NAME / CLIENT_CNF_NAME


@observe(tier="stage")
def read_client_option_file(path: Path | str | None = None) -> MariaClientConfig:
    """Parse the ``[client]`` group of a MySQL option file.

    Args:
        path: option file to read; ``default_option_file_path()`` when omitted.

    Returns:
        The non-secret client configuration. The ``password`` key is present in
        the file and deliberately NOT read — see the module docstring.

    Raises:
        FileNotFoundError: no such option file.
        ValueError: no ``[client]`` group, or a required key is missing.
    """
    resolved = Path(path).expanduser() if path is not None else default_option_file_path()
    if not resolved.is_file():
        raise FileNotFoundError(f"MariaDB client option file not found: {resolved}")

    # ``allow_no_value`` so bare mysqld-style flags (``skip-networking``) parse;
    # ``strict=False`` so a duplicated key self-heals rather than exploding —
    # car A rewrites this file on every start and a half-written one must
    # degrade to a clear ValueError, not a configparser traceback.
    parser = configparser.ConfigParser(allow_no_value=True, strict=False, interpolation=None)
    parser.read(resolved, encoding="utf-8")

    if not parser.has_section(CLIENT_GROUP):
        raise ValueError(f"option file {resolved} has no [{CLIENT_GROUP}] group")

    # A loop rather than a nested helper: I33 classifies inner functions as
    # first-class coverage subjects, and a closure this small is not worth its
    # own span.
    values: dict[str, str] = {}
    for key in ("user", "database", "socket"):
        value = (parser.get(CLIENT_GROUP, key, fallback="") or "").strip()
        if not value:
            raise ValueError(f"option file {resolved} is missing [{CLIENT_GROUP}] {key}")
        values[key] = value

    return MariaClientConfig(
        option_file=resolved,
        user=values["user"],
        database=values["database"],
        unix_socket=values["socket"],
    )
