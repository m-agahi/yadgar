"""Engine-#2 composition-root wiring (car C).

Runs with NO database and — critically — with no ``sqlalchemy`` / ``asyncmy``
import at module scope. Everything here is either pure, or reaches the engine
module through the composition root's own lazy import, so this file stays
runnable on the yadgar-ci image until it is rebuilt with ``--extra sql``.

Three things are pinned:

* the composition root does NOT import the engine-#2 module at import time
  (proved in a CLEAN interpreter, not by poking ``sys.modules`` — under
  ``-n 4 --dist loadgroup`` another test in the same worker may already have
  imported it);
* engine #2 absent is NOT fatal — ``_init_sql_storage`` returns ``None`` and
  ``_get_sql_storage`` returns ``None`` rather than asserting, matching the
  non-fatal posture ``entrypoint-backend.sh`` deliberately chose (every
  MariaDB failure there is a WARNING and the container stays healthy);
* the backend — and ONLY the backend — asks for it. ADR-0078/ADR-0200 keep
  core off every database, so ``sql_storage`` defaults to False.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from inspect import signature
from pathlib import Path

import pytest

import yadgar._shared.runtime.state as _st
from yadgar._shared.runtime.lifecycle import (
    _get_sql_storage,
    _init_sql_storage,
    init_engines,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ENGINE_MODULE = "yadgar._shared.storage.sql.mariadb"
_EMBED_SERVICE = _REPO_ROOT / "yadgar" / "backend" / "embed_service" / "embed_service.py"


@pytest.fixture(autouse=True)
def _preserve_sql_storage_slot():
    before = _st._sql_storage
    yield
    _st._sql_storage = before


# ── lazy import ──────────────────────────────────────────────────────────


def test_composition_root_does_not_import_the_engine_module():
    """Importing lifecycle must not pull in the engine-#2 module.

    A clean interpreter, because ``sys.modules`` is process-wide and this
    suite runs under xdist — an in-process assertion would pass or fail on
    test ordering rather than on the import graph.
    """
    probe = (
        "import sys\n"
        "import yadgar._shared.runtime.lifecycle\n"
        f"assert {_ENGINE_MODULE!r} not in sys.modules, 'engine-#2 module imported eagerly'\n"
        "assert 'sqlalchemy' not in sys.modules, 'sqlalchemy imported eagerly'\n"
        "assert 'asyncmy' not in sys.modules, 'asyncmy imported eagerly'\n"
    )
    subprocess.run([sys.executable, "-c", probe], check=True, cwd=_REPO_ROOT, timeout=180)


def test_composition_root_imports_with_asyncmy_and_sqlalchemy_unavailable():
    """The CI-image case: neither driver installed, import must still succeed.

    ``Dockerfile.ci:116`` bakes only ``--extra test --extra ml`` and the image
    has no auto-sync pipeline, so a hard import at the composition root would
    break EVERY CI test until it is rebuilt. Simulated with a meta-path finder
    that makes both packages unimportable.
    """
    probe = (
        "import sys\n"
        "_BLOCKED = ('asyncmy', 'sqlalchemy')\n"
        "class _Blocker:\n"
        "    def find_module(self, name, path=None):\n"
        "        return None\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in _BLOCKED:\n"
        "            raise ImportError('blocked: ' + name)\n"
        "        return None\n"
        "sys.meta_path.insert(0, _Blocker())\n"
        "for _m in list(sys.modules):\n"
        "    if _m.split('.')[0] in _BLOCKED:\n"
        "        del sys.modules[_m]\n"
        "import yadgar._shared.runtime.lifecycle as _lc\n"
        "assert _lc.init_engines is not None\n"
        # the credential half is pure stdlib and must stay reachable
        "from yadgar._shared.storage.sql.config import read_client_option_file\n"
        "assert read_client_option_file is not None\n"
    )
    subprocess.run([sys.executable, "-c", probe], check=True, cwd=_REPO_ROOT, timeout=180)


# ── non-fatal absence ────────────────────────────────────────────────────


def test_init_sql_storage_returns_none_when_option_file_absent(monkeypatch, tmp_path):
    monkeypatch.setenv("YADGAR_MARIADB_CLIENT_CNF", str(tmp_path / "absent.cnf"))
    assert _init_sql_storage() is None


def test_init_sql_storage_returns_none_when_construction_fails(monkeypatch, tmp_path):
    """A malformed option file degrades to None, it does not raise."""
    cnf = tmp_path / "client.cnf"
    cnf.write_text("[mysqld]\nskip-networking\n", encoding="utf-8")
    monkeypatch.setenv("YADGAR_MARIADB_CLIENT_CNF", str(cnf))

    assert _init_sql_storage() is None


def test_get_sql_storage_returns_none_instead_of_asserting():
    """Unlike every other getter here — engine-#2 absence is not fatal."""
    _st._sql_storage = None
    assert _get_sql_storage() is None


def test_get_sql_storage_returns_the_slot_when_present():
    sentinel = object()
    _st._sql_storage = sentinel
    assert _get_sql_storage() is sentinel


# ── who asks for it ──────────────────────────────────────────────────────


def test_init_engines_defaults_sql_storage_off():
    """Core calls init_engines too; ADR-0078/0200 keep it off every database."""
    param = signature(init_engines).parameters["sql_storage"]
    assert param.default is False


def _init_engines_call_in(func: ast.FunctionDef) -> ast.Call | None:
    for node in ast.walk(func):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "_init_engines":
                return node
    return None


def test_backend_boot_asks_for_engine_two():
    """The named caller (acceptance rule): the backend composition entry.

    Asserted by parsing the source rather than importing it — importing
    ``embed_service`` drags in FastAPI, torch and the whole model stack for
    what is a one-keyword wiring fact.
    """
    tree = ast.parse(_EMBED_SERVICE.read_text(encoding="utf-8"))
    func = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_ensure_recall_engines"
    )
    call = _init_engines_call_in(func)
    assert call is not None, "_ensure_recall_engines no longer calls _init_engines"

    kwargs = {kw.arg: kw.value for kw in call.keywords}
    assert "sql_storage" in kwargs, "backend boot does not request engine #2"
    assert isinstance(kwargs["sql_storage"], ast.Constant)
    assert kwargs["sql_storage"].value is True
