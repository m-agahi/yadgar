"""PR-0 regression guard: daemon 30-min auto-consolidation trigger is removed.

After v5.7.0 PR-0, consolidation only runs when explicitly invoked (MCP tool,
cron, manual). The background _daemon_loop — which auto-triggered consolidation
on idle detection with a 30-min cooldown — must not exist on ConsolidationScheduler.
"""

from yadgar.backend.consolidation import ConsolidationScheduler


def test_daemon_loop_method_removed():
    """_daemon_loop must not be a method of ConsolidationScheduler (PR-0)."""
    assert not hasattr(ConsolidationScheduler, "_daemon_loop"), (
        "_daemon_loop still exists on ConsolidationScheduler; PR-0 removes the daemon auto-trigger"
    )


def test_start_method_removed():
    """start() — which spawned the _daemon_loop thread — must be gone (PR-0)."""
    assert not hasattr(ConsolidationScheduler, "start"), (
        "start() still exists on ConsolidationScheduler; PR-0 removes the daemon thread startup"
    )


def test_stop_method_removed():
    """stop() — which joined the _daemon_loop thread — must be gone (PR-0)."""
    assert not hasattr(ConsolidationScheduler, "stop"), (
        "stop() still exists on ConsolidationScheduler; PR-0 removes the daemon thread shutdown"
    )


def test_force_consolidate_still_callable(tmp_path):
    """force_consolidate() must still work — MCP consolidate_now depends on it."""
    from yadgar._shared.config import Settings
    from yadgar._shared.embeddings import EmbeddingEngine
    from yadgar._shared.storage import StorageEngine

    storage = StorageEngine(str(tmp_path / "pr0_test.db"))
    emb = EmbeddingEngine()
    emb._unavailable = True
    settings = Settings(DB_PATH=str(tmp_path / "pr0_test.db"))
    sched = ConsolidationScheduler(storage, emb, settings)

    result = sched.force_consolidate()
    assert isinstance(result, dict), "force_consolidate() must return a stats dict"
    assert "memories_added" in result
    storage.close()
