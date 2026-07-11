"""Tests for Hippocampal Replay restoration engine.

T2 Car B: mirrors yadgar/backend/restoration/checkpoint_restore.py (moved from
_shared behind the backend POST /restore forward).
"""

import sys

import pytest

from yadgar._shared.config import Settings
from yadgar._shared.embeddings import EmbeddingEngine
from yadgar._shared.restoration.contract import CheckpointContext
from yadgar._shared.storage import StorageEngine
from yadgar.backend.restoration.checkpoint_restore import CheckpointRestore


@pytest.fixture
def temp_db(tmp_path):
    # surrealkv needs a directory path (not an existing file)
    yield str(tmp_path / "test.db")


@pytest.fixture
def engines(temp_db):
    settings = Settings(DB_PATH=temp_db)
    storage = StorageEngine(temp_db)
    embeddings = EmbeddingEngine()
    replay = CheckpointRestore(
        storage=storage,
        embeddings=embeddings,
        settings=settings,
    )
    yield storage, embeddings, replay
    storage.close()


class TestCheckpoints:
    def test_create_checkpoint(self, engines):
        storage, embeddings, replay = engines
        ctx = CheckpointContext(
            current_task="Implementing feature X",
            files_being_edited=["src/main.py", "src/utils.py"],
            key_decisions=["Use async for IO"],
            next_steps=["Write tests"],
        )
        result = replay.create_checkpoint("/test/project", ctx)
        assert result["status"] == "created"
        assert result["checkpoint_id"] > 0

    def test_checkpoint_supersedes(self, engines):
        storage, embeddings, replay = engines
        replay.create_checkpoint("/test", CheckpointContext(current_task="Task 1"))
        replay.create_checkpoint("/test", CheckpointContext(current_task="Task 2"))

        active = storage.get_active_checkpoint()
        assert active is not None
        assert active["current_task"] == "Task 2"

        # Only one active checkpoint
        rows = storage._q("SELECT * FROM checkpoint WHERE is_active = true")
        assert len(rows) == 1

    def test_epoch_tracking(self, engines):
        storage, embeddings, replay = engines
        assert storage.get_current_epoch() == 0

        replay.create_checkpoint("/test", CheckpointContext(current_task="T1"))
        assert storage.get_current_epoch() == 0

        new_epoch = storage.increment_epoch()
        assert new_epoch == 1


class TestAnchor:
    def test_anchor_memory(self, engines):
        storage, embeddings, replay = engines
        mid = replay.anchor_memory(
            content="Always use PostgreSQL not SQLite",
            context="/test/project",
            tags=["database", "decision"],
            reason="Architecture decision",
        )
        assert mid > 0

        mem = storage.get_memory(mid)
        assert mem["is_protected"] == 1
        assert mem["importance"] == 1.0
        assert "_anchor" in mem["tags"]

    def test_anchor_heat(self, engines):
        storage, embeddings, replay = engines
        mid = replay.anchor_memory(
            content="Critical fact",
            context="/test",
            tags=[],
        )
        mem = storage.get_memory(mid)
        assert mem["heat"] == 1.0


class TestPreCompactDrain:
    def test_drain_creates_epoch(self, engines):
        storage, embeddings, replay = engines
        result = replay.pre_compact_drain("/test")
        assert result["status"] == "drained"
        assert result["epoch"] == 1

    def test_drain_auto_checkpoint(self, engines):
        storage, embeddings, replay = engines
        result = replay.pre_compact_drain("/test")
        assert result["auto_checkpoint_created"] is True

        active = storage.get_active_checkpoint()
        assert active is not None

    def test_drain_preserves_existing_checkpoint(self, engines):
        storage, embeddings, replay = engines
        replay.create_checkpoint("/test", CheckpointContext(current_task="My task"))
        result = replay.pre_compact_drain("/test")

        # Should update existing, not create new auto
        assert result["auto_checkpoint_created"] is False


class TestRestore:
    def test_restore_empty(self, engines):
        storage, embeddings, replay = engines
        result = replay.restore("/test")
        assert "formatted" in result
        assert result["anchored_memories"] == 0
        assert result["hot_memories"] == 0

    def test_restore_with_checkpoint(self, engines):
        storage, embeddings, replay = engines
        replay.create_checkpoint(
            "/test",
            CheckpointContext(
                current_task="Building feature X",
                files_being_edited=["main.py"],
            ),
        )
        result = replay.restore("/test")
        assert result["checkpoint"] is not None
        assert "Building feature X" in result["formatted"]

    def test_restore_includes_anchored(self, engines):
        storage, embeddings, replay = engines
        replay.anchor_memory(
            content="Use React not Vue",
            context="/test",
            tags=["framework"],
            reason="Team decision",
        )
        result = replay.restore("/test")
        assert result["anchored_memories"] >= 1
        assert "React" in result["formatted"]

    def test_full_drain_restore_cycle(self, engines):
        storage, embeddings, replay = engines

        # Simulate a session
        replay.create_checkpoint(
            "/test",
            CheckpointContext(
                current_task="Refactoring auth module",
                key_decisions=["Switch to JWT"],
            ),
        )
        replay.anchor_memory(
            content="API key stored in .env",
            context="/test",
            tags=["security"],
        )

        # Simulate compaction
        replay.pre_compact_drain("/test")

        # Restore
        result = replay.restore("/test")
        assert result["checkpoint"] is not None
        assert result["anchored_memories"] >= 1
        assert "Refactoring auth module" in result["formatted"]
        assert "API key" in result["formatted"]


class TestCLISubcommandsForwardOnly:
    """T2 Car B: drain/restore CLI are thin HTTP forwarders to the backend.

    Without YADGAR_EMBED_URL they must fail LOUD (RuntimeError naming the env
    var) — no in-core fallback exists anymore (CheckpointRestore moved to
    yadgar.backend.restoration). The forward behavior itself is unit-covered in
    tests/scripts/test_cli_restore_module.py / test_cli_drain_module.py.
    """

    def _run_cli(self, *args):
        import os
        import subprocess

        env = {k: v for k, v in os.environ.items() if k != "YADGAR_EMBED_URL"}
        return subprocess.run(
            [sys.executable, "-m", "yadgar", *args],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )

    def test_cli_drain_fails_loud_without_backend_url(self):
        result = self._run_cli("drain", "/test/project")
        assert result.returncode != 0
        assert "YADGAR_EMBED_URL" in result.stderr

    def test_cli_restore_fails_loud_without_backend_url(self):
        result = self._run_cli("restore", "/test/project")
        assert result.returncode != 0
        assert "YADGAR_EMBED_URL" in result.stderr


class TestAutoCheckpoint:
    def test_tool_call_tracking(self, engines):
        storage, embeddings, replay = engines
        assert not replay.should_auto_checkpoint()

        for _ in range(50):
            replay.record_tool_call()

        assert replay.should_auto_checkpoint()

    def test_reset_after_checkpoint(self, engines):
        storage, embeddings, replay = engines
        for _ in range(50):
            replay.record_tool_call()
        assert replay.should_auto_checkpoint()

        replay.create_checkpoint("/test", CheckpointContext(current_task="T"))
        assert not replay.should_auto_checkpoint()


class TestRunRestore:
    """run_restore — the POST /restore body (T2 Car B).

    X1 MagicMock-storage safety: every _st slot touched is saved and restored
    in a finally block so mocks never leak into other tests' engine stacks.
    """

    def test_invalidates_map_then_delegates(self):
        """Cross-process staleness fix: the SR matrix is rebuilt per restore
        (transitions are recorded core-side; the backend _dirty flag cannot see
        them), then CheckpointRestore.restore runs with the directory."""
        from unittest.mock import MagicMock

        import yadgar._shared.runtime.state as _st
        from yadgar.backend.restoration import CognitiveMap, run_restore

        saved = (_st._storage, _st._embeddings, _st._cognitive_map, _st._replay)
        cmap = MagicMock(spec=CognitiveMap)
        replay = MagicMock()
        replay.restore.return_value = {"formatted": "# R", "epoch": 1}
        _st._storage = MagicMock()
        _st._embeddings = MagicMock()
        _st._cognitive_map = cmap
        _st._replay = replay
        try:
            result = run_restore("/proj")
        finally:
            _st._storage, _st._embeddings, _st._cognitive_map, _st._replay = saved

        cmap.invalidate.assert_called_once_with()
        replay.restore.assert_called_once_with(directory="/proj")
        assert result == {"formatted": "# R", "epoch": 1}

    def test_raises_when_engines_missing(self):
        """No storage/embeddings → ensure_restoration_engines cannot compose →
        fail loud (the /restore route must not return an empty 200)."""
        import pytest

        import yadgar._shared.runtime.state as _st
        from yadgar.backend.restoration import run_restore

        saved = (_st._storage, _st._embeddings, _st._cognitive_map, _st._replay)
        _st._storage = None
        _st._embeddings = None
        _st._cognitive_map = None
        _st._replay = None
        try:
            with pytest.raises(RuntimeError, match="CheckpointRestore not initialized"):
                run_restore("/proj")
        finally:
            _st._storage, _st._embeddings, _st._cognitive_map, _st._replay = saved
