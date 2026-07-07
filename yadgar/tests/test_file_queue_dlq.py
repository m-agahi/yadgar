"""Tests for DLQ, retry backoff, and error classification in QueueDrainer."""

import json
import os
import time
from unittest.mock import MagicMock, patch

from yadgar.core.file_queue import DrainerConfig, FileQueue, QueueDrainer, _Attempt, _classify_error

# ── _classify_error ───────────────────────────────────────────────────────────


class TestClassifyError:
    def test_400_is_permanent(self):
        assert (
            _classify_error("Client error '400 Bad Request' for url 'http://x/sql'") == "permanent"
        )

    def test_403_is_permanent(self):
        assert _classify_error("Client error '403 Forbidden'") == "permanent"

    def test_404_is_permanent(self):
        assert _classify_error("404 Not Found") == "permanent"

    def test_500_is_transient(self):
        assert _classify_error("Server error '500 Internal Server Error'") == "transient"

    def test_503_is_transient(self):
        assert _classify_error("503 Service Unavailable") == "transient"

    def test_connection_error_is_transient(self):
        assert _classify_error("Connection refused") == "transient"

    def test_timeout_is_transient(self):
        assert _classify_error("Request timed out") == "transient"

    def test_unknown_is_transient(self):
        assert _classify_error("Something went wrong") == "transient"


# ── FileQueue DLQ dir ─────────────────────────────────────────────────────────


class TestFileQueueDLQ:
    def test_dlq_dir_created_on_init(self, tmp_path):
        fq = FileQueue(tmp_path)
        assert fq.dlq_dir.exists()
        assert fq.dlq_dir.is_dir()

    def test_cleanup_dlq_removes_expired_pair(self, tmp_path):
        fq = FileQueue(tmp_path)
        old = fq.dlq_dir / "0001_abc.json"
        sidecar = fq.dlq_dir / "0001_abc.json.error.json"
        old.write_text(json.dumps({"op": "test"}))
        sidecar.write_text(json.dumps({"op_type": "test"}))
        old_mtime = time.time() - (100 * 86400)
        os.utime(old, (old_mtime, old_mtime))

        deleted = fq.cleanup_dlq(max_age_days=90)

        assert deleted == 1
        assert not old.exists()
        assert not sidecar.exists()

    def test_cleanup_dlq_preserves_recent(self, tmp_path):
        fq = FileQueue(tmp_path)
        recent = fq.dlq_dir / "0002_xyz.json"
        recent.write_text(json.dumps({"op": "test"}))

        deleted = fq.cleanup_dlq(max_age_days=90)
        assert deleted == 0
        assert recent.exists()

    def test_cleanup_dlq_preserves_events_log(self, tmp_path):
        fq = FileQueue(tmp_path)
        log = fq.dlq_dir / ".events.log"
        log.write_text('{"event":"dlq_move"}\n')
        old_mtime = time.time() - (200 * 86400)
        os.utime(log, (old_mtime, old_mtime))

        fq.cleanup_dlq(max_age_days=90)
        assert log.exists()


# ── QueueDrainer retry / DLQ ─────────────────────────────────────────────────


_DRAINER_CONFIG_FIELDS = {
    "max_permanent_attempts",
    "max_transient_attempts",
    "backoff_base_s",
    "backoff_max_s",
    "dlq_retention_days",
}


def _make_drainer(tmp_path, **kwargs) -> tuple[FileQueue, QueueDrainer]:
    fq = FileQueue(tmp_path)
    config_kwargs = {k: v for k, v in kwargs.items() if k in _DRAINER_CONFIG_FIELDS}
    drainer = QueueDrainer(
        fq,
        MagicMock(),
        drain_interval=0.01,
        config=DrainerConfig(**config_kwargs) if config_kwargs else None,
    )
    return fq, drainer


def _advance_all_backoffs(drainer: QueueDrainer) -> None:
    """Zero out all backoff timers so the next drain pass processes all files."""
    for a in drainer._attempts.values():
        a.next_retry_at = 0.0


class TestPermanentFailureDLQ:
    def test_moves_to_dlq_after_max_permanent_attempts(self, tmp_path):
        fq, drainer = _make_drainer(tmp_path, max_permanent_attempts=3, backoff_base_s=0.0)
        fq.enqueue("memorize", {"content": "test", "context": "/tmp", "branch": "master"})

        err = Exception("Client error '400 Bad Request'")
        with patch.object(drainer, "_apply", side_effect=err):
            drainer._drain_once()  # attempt 1
            _advance_all_backoffs(drainer)
            drainer._drain_once()  # attempt 2
            _advance_all_backoffs(drainer)
            drainer._drain_once()  # attempt 3 → DLQ

        assert list(fq.pending()) == []
        dlq_items = [f for f in fq.dlq_dir.glob("*.json") if not f.name.endswith(".error.json")]
        assert len(dlq_items) == 1

    def test_sidecar_written_on_dlq_move(self, tmp_path):
        fq, drainer = _make_drainer(tmp_path, max_permanent_attempts=1, backoff_base_s=0.0)
        fq.enqueue(
            "wiki_add",
            {
                "wiki_schema_version": 2,
                "slug": "t",
                "title": "T",
                "content": "C",
                "category": "reference",
                "branch": "master",
                "directory_context": "/test/sandbox",
            },
        )

        err = Exception("Client error '400 Bad Request' some detail")
        with patch.object(drainer, "_apply", side_effect=err):
            drainer._drain_once()

        dlq_items = [f for f in fq.dlq_dir.glob("*.json") if not f.name.endswith(".error.json")]
        assert len(dlq_items) == 1
        sidecar = fq.dlq_dir / (dlq_items[0].name + ".error.json")
        assert sidecar.exists()
        meta = json.loads(sidecar.read_text())
        assert meta["op_type"] == "wiki_add"
        assert meta["classification"] == "permanent"
        assert meta["attempts"] == 1
        assert "400" in meta["last_error"]
        assert meta["moved_to_dlq_at"]

    def test_events_log_appended(self, tmp_path):
        fq, drainer = _make_drainer(tmp_path, max_permanent_attempts=1, backoff_base_s=0.0)
        fq.enqueue("memorize", {"content": "t", "context": "/tmp", "branch": "master"})

        with patch.object(
            drainer, "_apply", side_effect=Exception("Client error '400 Bad Request'")
        ):
            drainer._drain_once()

        log = fq.dlq_dir / ".events.log"
        assert log.exists()
        event = json.loads(log.read_text().strip())
        assert event["event"] == "dlq_move"
        assert event["op_type"] == "memorize"


class TestTransientFailureSurvives:
    def test_transient_survives_permanent_threshold(self, tmp_path):
        fq, drainer = _make_drainer(
            tmp_path,
            max_permanent_attempts=3,
            max_transient_attempts=10,
            backoff_base_s=0.0,
        )
        fq.enqueue("memorize", {"content": "t", "context": "/tmp", "branch": "master"})

        err = Exception("Server error '503 Service Unavailable'")
        for _ in range(4):
            _advance_all_backoffs(drainer)
            with patch.object(drainer, "_apply", side_effect=err):
                drainer._drain_once()

        # 4 transient failures < max_transient=10 → still in queue
        assert len(list(fq.pending())) == 1
        assert not any(f for f in fq.dlq_dir.glob("*.json") if not f.name.endswith(".error.json"))


class TestBackoffBehavior:
    def test_file_skipped_within_backoff_window(self, tmp_path):
        fq, drainer = _make_drainer(tmp_path, backoff_base_s=3600.0)
        fq.enqueue("memorize", {"content": "t", "context": "/tmp", "branch": "master"})

        err = Exception("Client error '400 Bad Request'")
        with patch.object(drainer, "_apply", side_effect=err):
            drainer._drain_once()  # attempt 1 → next_retry_at = now + 3600

        # Second pass should NOT call _apply again (still in backoff)
        with patch.object(drainer, "_apply") as mock_apply:
            drainer._drain_once()
            mock_apply.assert_not_called()

    def test_backoff_doubles_each_attempt(self, tmp_path):
        fq, drainer = _make_drainer(
            tmp_path, max_permanent_attempts=10, backoff_base_s=30.0, backoff_max_s=3600.0
        )
        fq.enqueue("memorize", {"content": "t", "context": "/tmp", "branch": "master"})

        err = Exception("Client error '400 Bad Request'")
        now = time.time()

        _advance_all_backoffs(drainer)
        with patch.object(drainer, "_apply", side_effect=err):
            with patch("yadgar.core.file_queue.time") as mock_time:
                mock_time.time.return_value = now
                drainer._drain_once()

        fname = list(fq.pending())[0].name
        attempt = drainer._attempts[fname]
        assert attempt.count == 1
        # next_retry_at should be ~30s from now (30 * 2^0 = 30)
        assert abs(attempt.next_retry_at - (now + 30.0)) < 1.0

    def test_backoff_capped_at_max(self, tmp_path):
        fq, drainer = _make_drainer(
            tmp_path, max_permanent_attempts=20, backoff_base_s=30.0, backoff_max_s=100.0
        )
        fq.enqueue("memorize", {"content": "t", "context": "/tmp", "branch": "master"})

        fname = list(fq.pending())[0].name
        # Simulate 10 previous failures
        drainer._attempts[fname] = _Attempt(count=10)

        err = Exception("Client error '400 Bad Request'")
        _advance_all_backoffs(drainer)
        with patch.object(drainer, "_apply", side_effect=err):
            drainer._drain_once()

        attempt = drainer._attempts[fname]
        # next_retry_at should be capped at max (100s)
        assert attempt.next_retry_at <= time.time() + 101.0
        assert attempt.next_retry_at >= time.time() + 99.0


class TestSuccessAndReset:
    def test_success_clears_tracker(self, tmp_path):
        fq, drainer = _make_drainer(tmp_path)
        fq.enqueue("memorize", {"content": "t", "context": "/tmp", "branch": "master"})
        fname = list(fq.pending())[0].name
        drainer._attempts[fname] = _Attempt(count=2, last_error="old error")

        with patch.object(drainer, "_apply"):
            drainer._drain_once()

        assert fname not in drainer._attempts
        assert list(fq.pending()) == []

    def test_reset_attempt_clears_entry(self, tmp_path):
        _, drainer = _make_drainer(tmp_path)
        drainer._attempts["foo.json"] = _Attempt(count=5)
        drainer.reset_attempt("foo.json")
        assert "foo.json" not in drainer._attempts

    def test_reset_attempt_noop_for_unknown(self, tmp_path):
        _, drainer = _make_drainer(tmp_path)
        drainer.reset_attempt("nonexistent.json")  # should not raise
