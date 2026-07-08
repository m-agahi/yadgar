"""v5.49.4 Phase B — container-side sd_notify lifespan wire-up tests.

Verifies that the CORE composition root emits READY=1 on full-path init and that
the CORE shutdown wrapper emits STOPPING=1.

R2a Car D2: the READY=1 emit (``_emit_sd_ready``) and STOPPING=1 emit moved OUT of
``yadgar._shared.runtime.lifecycle`` (they imported ``yadgar.core.sd_notify`` — a
``_shared → core`` edge) into ``yadgar.core.lifecycle``. READY=1 is now driven by
``yadgar.core.bootstrap.core_init_engines`` on the FULL path (the backend /recall
slim path never signals READY). STOPPING=1 is fired by ``core.lifecycle.shutdown``
via a callback injected into the shared teardown at its exact original position.

All tests mock the heavy engine-init path so no SurrealDB connection is required.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

# Shared engine constructors live in _shared.runtime.lifecycle; the 9 CORE-only
# engine constructors live in yadgar.core.bootstrap (imported into its namespace).
_SHARED_LIFECYCLE_PATCHES = {
    "StorageEngine": MagicMock(),
    "EmbeddingEngine": MagicMock(),
    "ActionLogger": MagicMock(),
    "MemoryThermodynamics": MagicMock(),
    "KnowledgeGraph": MagicMock(),
    "CognitiveMap": MagicMock(),
    "Retriever": MagicMock(),
    "EngramAllocator": MagicMock(),
    "RulesEngine": MagicMock(),
    "MetaCognition": MagicMock(),
    "CheckpointRestore": MagicMock(),
    "WikiStore": MagicMock(),
    "_load_default_rules": MagicMock(),
    "_run_wiki_embedding_backfill": MagicMock(),
}

# R3 Car 1 F: the consolidation compute engines (MemoryCurator,
# ConsolidationScheduler, ProspectiveMemoryEngine, NarrativeEngine, WriteGate,
# CausalDiscovery) moved to the BACKEND — core.bootstrap no longer imports or
# instantiates them. StalenessDetector is the sole surviving core-only engine.
_BOOTSTRAP_PATCHES = {
    "StalenessDetector": MagicMock(),
}

# _get_file_queue + _emit_sd_ready live in yadgar.core.lifecycle now; the ready
# test lets _emit_sd_ready run for real (it is what calls sd_notify.ready) but
# stubs the file-queue drainer start so no real drainer thread spins up.
_CORE_LIFECYCLE_PATCHES = {
    "_init_file_queue": MagicMock(),
}


def _reset_state():
    """Reset lifecycle _state after each test to avoid cross-test pollution."""
    import yadgar._shared.runtime.state as _st

    _st._shutdown_done = False
    _st._storage = None
    _st._embeddings = None
    _st._buffer = None
    _st._consolidation = None
    _st._staleness = None


class TestLifecycleStartupEmitsReady:
    """core_init_engines() must call sd_notify.ready() once after engines are set up."""

    def test_lifespan_startup_emits_ready(self):
        """sd_notify.ready() called exactly once when core_init_engines() succeeds."""
        ready_mock = MagicMock(return_value=True)

        with (
            patch.multiple("yadgar._shared.runtime.lifecycle", **_SHARED_LIFECYCLE_PATCHES),
            patch.multiple("yadgar.core.bootstrap", **_BOOTSTRAP_PATCHES),
            patch.multiple("yadgar.core.lifecycle", **_CORE_LIFECYCLE_PATCHES),
            patch("yadgar.core.sd_notify.ready", ready_mock),
        ):
            from yadgar.core.bootstrap import core_init_engines

            _reset_state()
            core_init_engines(db_path=":memory:")
            _reset_state()

        ready_mock.assert_called_once()


class TestLifecycleShutdownEmitsStopping:
    """core.lifecycle.shutdown() must call sd_notify.stopping() once on first invocation."""

    def test_lifespan_shutdown_emits_stopping(self):
        """sd_notify.stopping() called exactly once when core shutdown() is invoked."""
        stopping_mock = MagicMock(return_value=True)

        _reset_state()

        with patch("yadgar.core.sd_notify.stopping", stopping_mock):
            from yadgar.core import lifecycle as core_lifecycle

            core_lifecycle.shutdown()

        stopping_mock.assert_called_once()

        _reset_state()


class TestLifecycleNoSocketSilentNoop:
    """No exception when NOTIFY_SOCKET is unset — sd_notify is already a no-op."""

    def test_lifespan_no_socket_silent_noop(self, monkeypatch):
        """core_init_engines() + core shutdown() complete without exception when NOTIFY_SOCKET unset."""
        monkeypatch.delenv("NOTIFY_SOCKET", raising=False)

        with (
            patch.multiple("yadgar._shared.runtime.lifecycle", **_SHARED_LIFECYCLE_PATCHES),
            patch.multiple("yadgar.core.bootstrap", **_BOOTSTRAP_PATCHES),
            patch.multiple("yadgar.core.lifecycle", **_CORE_LIFECYCLE_PATCHES),
        ):
            from yadgar.core import lifecycle as core_lifecycle
            from yadgar.core.bootstrap import core_init_engines

            _reset_state()
            # Must not raise even without NOTIFY_SOCKET
            core_init_engines(db_path=":memory:")
            core_lifecycle.shutdown()

        _reset_state()
