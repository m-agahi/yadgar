"""Shared in-process backend harness for unit and e2e test suites.

Factors out the four wiring pieces that the e2e conftest introduced for the R3
write-path port, so both conftests share one implementation:

1. drainer_wiring  — builds QueueDrainer + ConsolidationScheduler in-process
                     against the shared runtime storage; tears them down cleanly.
2. admin_bypass    — patches _forward_admin → run_admin_op (in-process).
3. recall_bypass   — patches _forward_to_backend → _fanout_recall (in-process).
4. consolidate_bypass — patches orchestrator._forward_to_backend → in-process
                        run_consolidation_cycle.

All four functions are CALL-TIME guarded on YADGAR_EMBED_URL: when the env var
is set (real-backend e2e tests), they delegate to the original implementation so
the real HTTP path is exercised unchanged.

Usage in conftests::

    from yadgar.tests._backend_harness import (
        wire_drainer,
        patch_admin_bypass,
        patch_recall_bypass,
        patch_consolidate_bypass,
    )
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from typing import Any

# ---------------------------------------------------------------------------
# 1. Drainer + consolidation wiring
# ---------------------------------------------------------------------------


@contextmanager
def wire_drainer(get_file_queue_fn):
    """Build the QueueDrainer + ConsolidationScheduler in-process.

    Assigns the drainer to ``_st._queue_drainer`` (via the server module) so
    existing ``drain_now()`` helpers work unchanged.  Also populates
    ``_st._consolidation`` / ``_st._sleep`` / ``_st._cls`` the way the backend
    service does.

    Args:
        get_file_queue_fn: callable returning the live per-test FileQueue (e.g.
            ``server._get_file_queue``).  Called INSIDE this manager so the
            queue is already reset by `_isolate_file_queue` when we run.

    Yields:
        The live QueueDrainer instance.

    Teardown:
        Stops the drainer (no-op on an already-stopped thread) and restores
        ``_st._consolidation`` to its previous value.
    """
    import yadgar._shared.runtime.state as _st
    from yadgar._shared.config import get_settings
    from yadgar.backend.consolidation import ConsolidationScheduler
    from yadgar.backend.queue_drainer import QueueDrainer
    from yadgar.core import server as _server

    fq = get_file_queue_fn()
    drainer = QueueDrainer(
        queue=fq,
        storage_factory=lambda: _st._storage,
        # drain_interval=9999: background loop stays inert; tests call drain_now()
        # synchronously.  A short interval risks hitting the DB during per-test
        # teardown (the _q_server teardown error seen in e2e reruns).
        drain_interval=9999,
    )
    drainer.start()
    _server._queue_drainer = drainer

    prev_consolidation = _st._consolidation
    scheduler = ConsolidationScheduler(
        _st._storage, _st._embeddings, get_settings(), pool=_st._pool
    )
    _st._consolidation = scheduler
    _st._sleep = scheduler._sleep_engine
    _st._cls = scheduler.cls

    try:
        yield drainer
    finally:
        try:
            drainer.stop()
        except Exception:  # noqa: BLE001 — teardown must never raise
            pass
        _st._consolidation = prev_consolidation


# ---------------------------------------------------------------------------
# 2. Admin bypass (_forward_admin → run_admin_op in-process)
# ---------------------------------------------------------------------------


def patch_admin_bypass(monkeypatch: Any) -> None:
    """Patch ``_forward_admin`` → ``run_admin_op`` (in-process, no HTTP).

    CALL-TIME guarded: when YADGAR_EMBED_URL is set the original HTTP forwarder
    is used unchanged (real-backend tests exercise the real HTTP contract).

    Patches the source module AND every consumer module that imported the helper
    by name.  Consumers list mirrors e2e conftest exactly.

    Args:
        monkeypatch: pytest's monkeypatch fixture (function-scoped).
    """
    import yadgar.core.forward as _forward_module
    from yadgar.backend.admin_exec import run_admin_op_blocking

    _orig_forward_admin = _forward_module._forward_admin

    def _bypass_admin(op, payload, timeout_s=30.0):
        if os.environ.get("YADGAR_EMBED_URL"):
            return _orig_forward_admin(op, payload, timeout_s=timeout_s)
        # Blocking variant: engine-#2 car H made check_invariants a coroutine op.
        return run_admin_op_blocking(op, payload)

    monkeypatch.setattr(_forward_module, "_forward_admin", _bypass_admin)
    for _consumer in (
        "yadgar.core.server.tools.bookmarks",
        "yadgar.core.server.tools.blocks",
        "yadgar.core.server.tools.admin_other",
        "yadgar.core.server.tools.admin_archive",
        "yadgar.core.server.tools.wiki",
        "yadgar.core.server.tools.agent_prompts",
        "yadgar.core.server.tools.audit",
        "yadgar.core.server.tools.admin_invariants",
        "yadgar.core.server.tools.project",
        "yadgar.core.server.tools.dispatch_helper",
        # consolidation orchestrator binds _forward_admin (check_invariants tail)
        "yadgar.core.consolidation.orchestrator",
    ):
        _mod = sys.modules.get(_consumer)
        if _mod is not None and hasattr(_mod, "_forward_admin"):
            monkeypatch.setattr(_mod, "_forward_admin", _bypass_admin)


# ---------------------------------------------------------------------------
# 3. Recall bypass (_forward_to_backend → _fanout_recall in-process)
# ---------------------------------------------------------------------------


def patch_recall_bypass(monkeypatch: Any) -> None:
    """Patch recall's ``_forward_to_backend`` → ``_fanout_recall`` (in-process).

    CALL-TIME guarded: when YADGAR_EMBED_URL is set the original HTTP forwarder
    is used unchanged.  Landscape mode returns [] (no AstrocytePool consensus
    in-process) — mirrors recall_backend_bypass in the unit conftest.

    Args:
        monkeypatch: pytest's monkeypatch fixture (function-scoped).
    """
    from yadgar.backend.retrieval.compose import ensure_retrieval_engine
    from yadgar.backend.retrieval.recall_pipeline import _fanout_recall

    _recall_module = sys.modules["yadgar.core.server.tools.recall"]
    _orig_forward = _recall_module._forward_to_backend

    def _bypass_forward(  # noqa: PLR0913 — mirrors _forward_to_backend signature
        query,
        max_results,
        min_heat,
        directory,
        current_branch,
        default_branch,
        type_filter,
        tags,
        mode=None,
        profile=None,
        **kwargs,
    ):
        if os.environ.get("YADGAR_EMBED_URL"):
            return _orig_forward(
                query,
                max_results,
                min_heat,
                directory,
                current_branch,
                default_branch,
                type_filter,
                tags,
                mode=mode,
                profile=profile,
                **kwargs,
            )
        if mode is not None:
            # landscape / unknown modes not wired for direct in-process path
            return []
        # In unit tests, memories are stored with branch=YADGAR_CI_BRANCH.
        # recall.py detects current_branch from _detect_branch(), which returns
        # None for fake test directories (e.g. /home/user/project).  With
        # current_branch=None and default_branch='master' (git fallback), the
        # BranchFilter SQL clause is:
        #   (branch IS NONE OR branch = 'master')
        # — which excludes feat/* memories stored under YADGAR_CI_BRANCH.
        # Fix: fill current_branch from YADGAR_CI_BRANCH so the clause becomes:
        #   (branch IS NONE OR branch = 'master' OR branch = 'feat/test-branch')
        # — which includes unit-test memories without disabling branch isolation.
        _ci_branch = os.environ.get("YADGAR_CI_BRANCH")
        _effective_branch = current_branch or _ci_branch or None
        # T2 Car E2: compose the backend retriever lazily against the test's
        # live engines (idempotent; the shared root no longer builds it).
        ensure_retrieval_engine()
        return _fanout_recall(
            query=query,
            max_results=max_results,
            min_heat=min_heat,
            directory=directory,
            current_branch=_effective_branch,
            default_branch=default_branch,
            type_filter=type_filter,
            tags=tags,
            profile=profile,
        )

    monkeypatch.setattr(_recall_module, "_forward_to_backend", _bypass_forward)


# ---------------------------------------------------------------------------
# 3b. Restore bypass (_forward_restore → backend run_restore in-process)
# ---------------------------------------------------------------------------


def patch_restore_bypass(monkeypatch: Any) -> None:
    """Patch ``_forward_restore`` → ``backend.restoration.run_restore`` (in-process).

    T2 Car B: the restore MCP tool, the /hooks/post-compact handler, and the
    CLI restore subcommand are thin forwarders to the backend POST /restore.
    Unit tests have no backend HTTP server — run the backend restore body
    directly against the same ``_st`` engine stack the test fixture wired
    (``run_restore`` composes CognitiveMap + CheckpointRestore lazily via
    ``ensure_restoration_engines``, so the compute is real, #52).

    CALL-TIME guarded: when YADGAR_EMBED_URL is set the original HTTP forwarder
    is used unchanged (real-backend e2e tests exercise the real contract).

    Patches the source module AND consumers that bound the helper by name
    (tools.misc). http.py + the CLI import it lazily inside the handler body,
    so patching the source module covers them at call time.

    Args:
        monkeypatch: pytest's monkeypatch fixture (function-scoped).
    """
    import yadgar.core.forward as _forward_module
    from yadgar.backend.restoration import run_restore

    _orig_forward_restore = _forward_module._forward_restore

    def _bypass_restore(directory="", timeout_s=120.0):
        if os.environ.get("YADGAR_EMBED_URL"):
            return _orig_forward_restore(directory, timeout_s=timeout_s)
        return run_restore(directory)

    monkeypatch.setattr(_forward_module, "_forward_restore", _bypass_restore)
    for _consumer in ("yadgar.core.server.tools.misc",):
        _mod = sys.modules.get(_consumer)
        if _mod is not None and hasattr(_mod, "_forward_restore"):
            monkeypatch.setattr(_mod, "_forward_restore", _bypass_restore)


# ---------------------------------------------------------------------------
# 3b. Viz bypass (_forward_viz → run_viz_op, in-process)
# ---------------------------------------------------------------------------


def patch_viz_bypass(monkeypatch: Any) -> None:
    """Patch ``_forward_viz`` → ``run_viz_op`` (in-process, no HTTP).

    T2 Car E3: the core /api/graph* handlers are thin forwarders to the backend
    POST /viz route. Unit tests have no backend HTTP server — run the backend
    graph-assembly body directly against the same ``_st`` engine stack the test
    fixture wired.

    CALL-TIME guarded: when YADGAR_EMBED_URL is set the original HTTP forwarder
    is used unchanged. http.py imports the helper lazily inside each handler
    body, so patching the source module covers all consumers at call time.

    Args:
        monkeypatch: pytest's monkeypatch fixture (function-scoped).
    """
    import yadgar.core.forward as _forward_module
    from yadgar.backend.viz_exec import run_viz_op

    _orig_forward_viz = _forward_module._forward_viz

    def _bypass_viz(op, payload, timeout_s=60.0):
        if os.environ.get("YADGAR_EMBED_URL"):
            return _orig_forward_viz(op, payload, timeout_s=timeout_s)
        return run_viz_op(op, payload)

    monkeypatch.setattr(_forward_module, "_forward_viz", _bypass_viz)


# ---------------------------------------------------------------------------
# 4. Consolidate bypass (orchestrator._forward_to_backend → in-process cycle)
# ---------------------------------------------------------------------------


def patch_consolidate_bypass(monkeypatch: Any) -> None:
    """Patch orchestrator's ``_forward_to_backend`` → ``run_consolidation_cycle``.

    CALL-TIME guarded: when YADGAR_EMBED_URL is set the original HTTP forwarder
    is used unchanged.

    R3 harness gap: ``run_consolidation_cycle`` → ``_get_scheduler`` →
    ``_ensure_recall_engines()`` short-circuits on the module-level
    ``_recall_engines_ready`` flag.  In the single-process harness a prior test's
    shutdown()/teardown can null ``_st._storage`` WITHOUT resetting the flag,
    breaking the invariant.  Re-arm the lazy init when storage is dead so the
    next ``_ensure_recall_engines()`` rebuilds engines against the live storage.

    Also drops the stale backend scheduler singleton on teardown so subsequent
    modules rebuild it against their own live storage.

    Args:
        monkeypatch: pytest's monkeypatch fixture (function-scoped).
    """
    import yadgar._shared.runtime.state as _st
    from yadgar.backend.consolidation import service as _consol_service
    from yadgar.backend.embed_service import embed_service as _embed_service
    from yadgar.core.consolidation import orchestrator as _orch

    _orig_forward = _orch._forward_to_backend

    def _bypass_consolidate(mode, timeout_s=1800.0):
        if os.environ.get("YADGAR_EMBED_URL"):
            return _orig_forward(mode, timeout_s=timeout_s)
        if _st._storage is None:
            # Prior test's shutdown() nulled storage WITHOUT resetting the flag
            # — re-arm so the next _ensure_recall_engines() rebuilds against
            # the live engine stack (mirrors e2e conftest comment).
            _embed_service._recall_engines_ready = False
            _consol_service._scheduler = None
        elif not _embed_service._recall_engines_ready:
            # Unit test: _st._storage is live (set by init_engines) but the
            # backend's lazy-init guard hasn't fired yet. Arm it here so
            # _ensure_recall_engines() returns immediately without re-running
            # _init_engines() (which would overwrite _st._storage). The unit
            # test's live storage IS the engine the scheduler should use.
            _embed_service._recall_engines_ready = True
            _consol_service._scheduler = None  # force rebuild against live storage
        return _consol_service.run_consolidation_cycle(mode)

    monkeypatch.setattr(_orch, "_forward_to_backend", _bypass_consolidate)
    # Caller tears down via monkeypatch.undo() — no extra cleanup needed here.
    # Caller must call teardown_consolidate_bypass() in its finally block to drop
    # the stale scheduler singleton after each test module.


def teardown_consolidate_bypass() -> None:
    """Drop the memoized backend scheduler singleton after each test module.

    Call in the finally block of any fixture that used patch_consolidate_bypass.
    The singleton is not reset by server.shutdown(), so without this drop the
    next module would inherit a scheduler bound to a dead storage.
    """
    try:
        from yadgar.backend.consolidation import service as _consol_service

        _consol_service._scheduler = None
        # The scheduler builder also exposes its inner engines as _st globals
        # (_consolidation/_sleep/_cls). Clear them with the singleton, or the
        # NEXT module's engine-presence assertions (e.g. the slim-engine gate)
        # see stale core-only engines from this module's scheduler.
        import yadgar._shared.runtime.state as _st

        _st._consolidation = None
        _st._sleep = None
        _st._cls = None
    except Exception:  # noqa: BLE001
        pass
