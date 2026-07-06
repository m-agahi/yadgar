"""Backend recall-engine bootstrap contract (#44).

The prod BACKEND container carries the shared ``YADGAR_OFFLOAD_TOOLS`` flag (it
is a single config) but has NO ``YADGAR_EMBED_URL`` (it *is* the embed service →
wants LOCAL in-process engines).  Before the fix, the backend ``/recall``
bootstrap called ``init_engines()`` → ``_init_embedding_client()`` which tripped
the CORE offload guard and raised ``RuntimeError`` → the route 500'd → prod fell
back to in-core recall (worked, but ~2 s wasted).

The offload guard is a CORE concern: core offloads tool bodies to threads, so it
needs REMOTE engines for GIL-safety.  The BACKEND is a single-purpose ML service
where LOCAL engines are correct AND GIL-safe (it is not running the core tool
pool).  The fix gives the backend recall-init an explicit local-engine path that
bypasses the guard, WITHOUT weakening the guard on the core path.

These are the tests that would have caught #44:

  * ``test_local_engines_bypasses_offload_guard`` — RED on pre-fix code
    (RuntimeError), GREEN after: ``local_engines=True`` selects LOCAL engines
    under the prod backend env and does NOT raise.
  * The CORE guard MUST stay intact — asserted here too (symmetry with
    ``test_offload_wiring.py::test_claim1_offload_on_local_engine_fails_loud``).
"""

from __future__ import annotations

import pytest


def test_local_engines_bypasses_offload_guard(monkeypatch):
    """Backend prod env (offload ON + no EMBED_URL) + local_engines=True → LOCAL, no raise.

    This is the exact prod BACKEND condition from #44.  With the explicit
    local-engine request the backend recall bootstrap must build LocalMLClient +
    in-process EmbeddingEngine, NOT trip the core offload guard.
    """
    from yadgar.backend.ml_client import LocalMLClient
    from yadgar.config import get_settings
    from yadgar.server.lifecycle import _init_embedding_client

    # Prod BACKEND container condition: shared offload flag ON, but it IS the
    # embed service so no EMBED_URL is set.
    monkeypatch.setenv("YADGAR_OFFLOAD_TOOLS", "1")
    monkeypatch.delenv("YADGAR_EMBED_URL", raising=False)

    embeddings, ml_client = _init_embedding_client(None, get_settings(), local_engines=True)

    assert embeddings is not None
    assert ml_client is not None
    # Must be the LOCAL client — the whole point is in-process torch engines.
    assert isinstance(ml_client, LocalMLClient), (
        "backend recall bootstrap must select LocalMLClient, not a remote shim"
    )


def test_core_offload_guard_still_fires(monkeypatch):
    """CORE default (local_engines=False) under offload ON + no EMBED_URL → still raises.

    The guard is correct for the core path (tool-body offload needs remote
    engines for GIL-safety).  The fix must NOT weaken it: without an explicit
    local-engine request the guard fires exactly as before.
    """
    from yadgar.config import get_settings
    from yadgar.server.lifecycle import _init_embedding_client

    monkeypatch.setenv("YADGAR_OFFLOAD_TOOLS", "1")
    monkeypatch.delenv("YADGAR_EMBED_URL", raising=False)

    with pytest.raises(RuntimeError, match="OFFLOAD_TOOLS"):
        _init_embedding_client(None, get_settings())
