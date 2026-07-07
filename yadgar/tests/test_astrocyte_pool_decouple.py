"""R2a Car A characterization: AstrocytePool composition-root invariants.

Pins the behavior the ``pool=`` decouple refactor MUST preserve:

  1. After ``init_engines``, ``_st._pool`` is a single AstrocytePool instance and
     ``AstrocytePool.init_processes`` was called exactly ONCE (no double-build).
  2. ``_st._consolidation.pool is _st._pool`` — identity: the ConsolidationScheduler
     exposes the SAME object the composition root assigned as ``_st._pool``.

Both invariants must hold BEFORE the refactor (own-built pool exposed via
``_st._consolidation.pool``) AND AFTER (standalone pool injected into the
scheduler). The spy patches the method on the class object so it catches both the
old ``_get_pool_class()`` lazy-import path and the new top-level import — both
resolve to the same ``AstrocytePool`` class.
"""

from __future__ import annotations

import pytest

from yadgar.core import server

pytestmark = pytest.mark.usefixtures("recall_backend_bypass")

_MODEL = "all-MiniLM-L6-v2"


def _spy_init_processes(monkeypatch):
    """Wrap AstrocytePool.init_processes to count calls (call-through, not a stub)."""
    import yadgar._shared.astrocyte_pool as ap

    calls = {"n": 0}
    real = ap.AstrocytePool.init_processes

    def wrapper(self, *a, **k):
        calls["n"] += 1
        return real(self, *a, **k)

    monkeypatch.setattr(ap.AstrocytePool, "init_processes", wrapper)
    return calls


@pytest.mark.parametrize("engine_set", ["slim", "full"])
def test_pool_identity_and_single_init(tmp_path, monkeypatch, engine_set):
    """_st._pool is a single AstrocytePool, init_processes once, scheduler shares it."""
    from yadgar._shared.astrocyte_pool import AstrocytePool

    calls = _spy_init_processes(monkeypatch)

    db_path = str(tmp_path / f"pool_{engine_set}.db")
    server.init_engines(db_path=db_path, embedding_model=_MODEL, engine_set=engine_set)
    try:
        import yadgar._shared.runtime.state as _st

        # (a) single AstrocytePool instance, init_processes called exactly once.
        # Holds for BOTH slim and full: the shared root builds _pool standalone.
        assert isinstance(_st._pool, AstrocytePool), "_st._pool must be an AstrocytePool"
        assert calls["n"] == 1, f"init_processes called {calls['n']}x, expected exactly 1"

        # (b) identity: scheduler.pool is the same object as _st._pool.
        # R2a Car B: _consolidation is now FULL-ONLY (built by core/bootstrap);
        # slim leaves it None. The pool-injection identity is only assertable when
        # the scheduler exists.
        if engine_set == "full":
            assert _st._consolidation is not None
            assert _st._consolidation.pool is _st._pool, (
                "_st._consolidation.pool must BE the same object as _st._pool"
            )
        else:
            assert _st._consolidation is None, "slim must NOT build _consolidation (Car B)"
    finally:
        server.shutdown()
