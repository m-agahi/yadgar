"""Tests for §12 resource hygiene fixes.

Covers:
- init_engram_slots int cast (float r from SurrealDB)
- shutdown idempotency (_shutdown_done flag)
- _get_file_queue drainer init — _file_queue not assigned if start() raises
"""

import pytest


class TestInitEngramIntCast:
    """init_engram_slots: existing slot indices from DB may be floats — cast to int."""

    @pytest.fixture
    def storage(self, tmp_path):
        from yadgar._shared.storage import StorageEngine

        engine = StorageEngine(str(tmp_path / "test.db"))
        yield engine
        engine.close()

    def test_float_slot_indices_not_reinserted(self, storage):
        """Float slot indices (e.g. 0.0, 1.0) must be treated as existing slots."""
        # Insert slots 0-4 normally
        storage.init_engram_slots(5)

        # Patch _q to return floats (simulating SurrealDB returning 0.0 instead of 0)
        original_q = storage._q

        def patched_q(surql, params=None):
            if "SELECT VALUE slot_index FROM engram_slot" in surql:
                # Return floats instead of ints — the bug
                return [0.0, 1.0, 2.0, 3.0, 4.0]
            return original_q(surql, params)

        storage._q = patched_q

        # Capture INSERT calls
        inserts = []
        original_q2 = original_q

        def patched_q2(surql, params=None):
            if "INSERT INTO engram_slot" in surql:
                inserts.append(surql)
            return original_q2(surql, params)

        # Reset _q to capture inserts while still returning floats for SELECT
        def combined_q(surql, params=None):
            if "SELECT VALUE slot_index FROM engram_slot" in surql:
                return [0.0, 1.0, 2.0, 3.0, 4.0]
            if "INSERT INTO engram_slot" in surql:
                inserts.append(surql)
                return original_q(surql, params)
            return original_q(surql, params)

        storage._q = combined_q
        storage.init_engram_slots(5)

        # With correct int cast, no inserts should happen (all 5 slots exist)
        assert inserts == [], f"Re-inserted slots despite floats matching ints: {inserts}"

    def test_init_engram_slots_idempotent(self, storage):
        """Calling init_engram_slots twice must not duplicate slots."""
        storage.init_engram_slots(10)
        storage.init_engram_slots(10)
        rows = storage._q("SELECT VALUE slot_index FROM engram_slot")
        # Should have exactly 10 slots, not 20
        assert len(rows) == 10


class TestShutdownIdempotency:
    """shutdown() must be safe to call twice (Q16)."""

    def test_double_shutdown_no_exception(self, tmp_path):
        """Calling shutdown() twice must not raise."""
        import yadgar.core.server as srv

        # Initialize a minimal engine set
        srv.init_engines(db_path=str(tmp_path / "test.db"), start_daemons=False)

        # First shutdown
        srv.shutdown()
        # Second shutdown — must not raise
        srv.shutdown()

    def test_shutdown_idempotent_globals_none(self, tmp_path):
        """After shutdown(), all engine globals must be None."""
        import yadgar.core.server as srv

        srv.init_engines(db_path=str(tmp_path / "test.db"), start_daemons=False)
        srv.shutdown()

        assert srv._storage is None
        assert srv._embeddings is None
        assert srv._consolidation is None

    def test_second_shutdown_is_noop(self, tmp_path):
        """Second shutdown does nothing (all globals already None)."""
        import yadgar.core.server as srv

        srv.init_engines(db_path=str(tmp_path / "test.db"), start_daemons=False)
        srv.shutdown()
        # After shutdown, all are None — second call must not error on None.close()
        srv.shutdown()
        assert srv._storage is None


class TestFileQueueDrainerInit:
    """R3: core _get_file_queue owns ONLY the enqueue side — never the drainer.

    Pre-R3 this guarded '_file_queue must NOT be assigned if drainer.start()
    raises'. R3 Car 1 removed the core → drainer construction edge entirely:
    the QueueDrainer is a backend concern. The replacing invariant is that
    core queue init is fully decoupled from drainer lifecycle — a poisoned
    QueueDrainer.start() must not affect _get_file_queue() at all.
    """

    def test_failed_start_leaves_file_queue_none(self, tmp_path, monkeypatch):
        """QueueDrainer.start() raising must be irrelevant to core queue init."""
        import yadgar.core.server as srv

        # Ensure _file_queue is None to start
        srv._file_queue = None
        srv._queue_drainer = None

        from yadgar.backend.queue_drainer import QueueDrainer

        def failing_start(self):
            raise RuntimeError("Thread start failed")

        monkeypatch.setattr(QueueDrainer, "start", failing_start)

        # Temporarily set DATA_DIR to tmp_path so FileQueue init works

        monkeypatch.setenv("YADGAR_DATA_DIR", str(tmp_path / "data"))

        # R3: no drainer is constructed on the enqueue path — no raise.
        fq = srv._get_file_queue()

        # The queue is built and usable; the drainer slot stays None (backend
        # lifecycle owns it) — the poisoned start() was never reached.
        assert fq is not None, "_get_file_queue must build the enqueue-side FileQueue"
        assert srv._file_queue is not None
        assert srv._queue_drainer is None, (
            "core _get_file_queue must not construct/start a QueueDrainer (R3 Car 1)"
        )

        # Cleanup
        srv._file_queue = None
        srv._queue_drainer = None
