"""v5.49.4 Phase B — container-side sd_notify lifespan wire-up tests.

Verifies that `yadgar.server.lifecycle.init_engines()` emits READY=1 and
`shutdown()` emits STOPPING=1 via the sd_notify helper.

All tests mock the heavy engine-init path so no SurrealDB connection is required.
Pattern: patch.multiple() replaces all heavy constructors; monkeypatch replaces
sd_notify.ready / sd_notify.stopping to MagicMock; assert called once.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

# Attribute names in yadgar.server.lifecycle to replace for no-DB runs.
# Keys are bare attribute names (patch.multiple targets the module, keys are attrs).
_LIFECYCLE_PATCHES = {
    "StorageEngine": MagicMock(),
    "EmbeddingEngine": MagicMock(),
    "ActionLogger": MagicMock(),
    "MemoryThermodynamics": MagicMock(),
    "KnowledgeGraph": MagicMock(),
    "CognitiveMap": MagicMock(),
    "Retriever": MagicMock(),
    "MemoryCurator": MagicMock(),
    "ConsolidationScheduler": MagicMock(),
    "StalenessDetector": MagicMock(),
    "ProspectiveMemoryEngine": MagicMock(),
    "NarrativeEngine": MagicMock(),
    "WriteGate": MagicMock(),
    "EngramAllocator": MagicMock(),
    "RulesEngine": MagicMock(),
    "CausalDiscovery": MagicMock(),
    "MetaCognition": MagicMock(),
    "CheckpointRestore": MagicMock(),
    "WikiStore": MagicMock(),
    "_load_default_rules": MagicMock(),
    "_run_wiki_embedding_backfill": MagicMock(),
    "_get_file_queue": MagicMock(),
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
    """init_engines() must call sd_notify.ready() once after engines are set up."""

    def test_lifespan_startup_emits_ready(self):
        """sd_notify.ready() called exactly once when init_engines() succeeds."""
        ready_mock = MagicMock(return_value=True)

        with patch.multiple("yadgar._shared.runtime.lifecycle", **_LIFECYCLE_PATCHES):
            with patch("yadgar.core.sd_notify.ready", ready_mock):
                from yadgar._shared.runtime import lifecycle

                _reset_state()
                lifecycle.init_engines(db_path=":memory:")
                _reset_state()

        ready_mock.assert_called_once()


class TestLifecycleShutdownEmitsStopping:
    """shutdown() must call sd_notify.stopping() once on first invocation."""

    def test_lifespan_shutdown_emits_stopping(self):
        """sd_notify.stopping() called exactly once when shutdown() is invoked."""
        stopping_mock = MagicMock(return_value=True)

        _reset_state()

        with patch("yadgar.core.sd_notify.stopping", stopping_mock):
            from yadgar._shared.runtime import lifecycle

            lifecycle.shutdown()

        stopping_mock.assert_called_once()

        _reset_state()


class TestLifecycleNoSocketSilentNoop:
    """No exception when NOTIFY_SOCKET is unset — sd_notify is already a no-op."""

    def test_lifespan_no_socket_silent_noop(self, monkeypatch):
        """init_engines() + shutdown() complete without exception when NOTIFY_SOCKET unset."""
        monkeypatch.delenv("NOTIFY_SOCKET", raising=False)

        with patch.multiple("yadgar._shared.runtime.lifecycle", **_LIFECYCLE_PATCHES):
            from yadgar._shared.runtime import lifecycle

            _reset_state()
            # Must not raise even without NOTIFY_SOCKET
            lifecycle.init_engines(db_path=":memory:")
            lifecycle.shutdown()

        _reset_state()
