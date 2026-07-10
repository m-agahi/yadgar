"""Probe tests for Car 2 lazy fixture gating — NOT collected in normal sweeps.

This file deliberately lacks the ``test_`` filename prefix so directory-based
collection skips it.  Each probe is run in a DEDICATED child pytest process by
``test_lazy_fixtures_car2.py`` (explicit path :: node-id selection), because the
lazy state under test (``_SURREAL_HANDLE``, torch import) is worker-process-global
and would be polluted by sibling tests on a shared xdist worker.

Probe contract (Car 2 of the test-suite hardening train):
  1. A logic-only test never spawns the session SurrealDB and never imports
     torch / sentence-transformers.
  2. Constructing a StorageEngine lazily spawns the session SurrealDB on the
     reserved URL (server mode preserved for DB tests).
  3. ``init_engines()`` defers the eager embedding-model warmup; the model
     loads on the first actual encode instead.
"""

from __future__ import annotations

import shutil
import sys

import pytest

_requires_surreal = pytest.mark.skipif(
    not shutil.which("surreal"), reason="surreal binary not on PATH"
)


def test_probe_logic_only_no_surreal_no_model():
    """A pure-logic test must not pay for SurrealDB or the embedding model."""
    from yadgar.tests import conftest as _c

    assert _c._SURREAL_HANDLE is None, (
        "session SurrealDB was spawned for a logic-only test — lazy gating broken"
    )
    assert "torch" not in sys.modules, "torch imported for a logic-only test"
    assert "sentence_transformers" not in sys.modules, (
        "sentence_transformers imported for a logic-only test"
    )


@_requires_surreal
def test_probe_storage_engine_triggers_spawn(tmp_path):
    """First StorageEngine construction must lazily spawn the session server."""
    from yadgar._shared.storage import StorageEngine
    from yadgar.tests import conftest as _c

    assert _c._SURREAL_HANDLE is None, "server spawned before any DB demand"
    engine = StorageEngine(str(tmp_path / "probe.db"))
    try:
        assert _c._SURREAL_HANDLE is not None, (
            "StorageEngine construction must lazily spawn the session SurrealDB"
        )
        assert engine._db_url == _c._REAL_DB_URL, (
            "engine must run in server mode against the reserved session URL"
        )
    finally:
        engine.close()


@_requires_surreal
def test_probe_init_engines_defers_model_load(tmp_path):
    """init_engines() must NOT eagerly load the embedding model in tests.

    The eager ``_ensure_model()`` warmup in lifecycle.init_engines costs ~700MB
    RSS per xdist worker.  Every encode path calls ``_ensure_model()`` itself,
    so deferring the warmup is behavior-neutral: the model loads on first
    actual encode.
    """
    from yadgar.core import server

    server.init_engines(db_path=str(tmp_path / "probe_engines.db"))
    try:
        import yadgar._shared.runtime.state as _st

        assert _st._embeddings._model is None, (
            "init_engines eagerly loaded the embedding model — warmup deferral broken"
        )
        vec = _st._embeddings.encode("lazy fixture probe")
        assert vec is not None
        assert _st._embeddings._model is not None, "encode() must load the model on demand"
    finally:
        server.shutdown()
