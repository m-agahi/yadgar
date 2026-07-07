"""Tests for the dlq_inspect / dlq_requeue MCP tools."""

import json

import pytest

from yadgar.core import server


@pytest.fixture(autouse=True)
def _engines(tmp_path, monkeypatch):
    """Init engines and route the file queue to an isolated YADGAR_DATA_DIR."""
    monkeypatch.setenv("YADGAR_DATA_DIR", str(tmp_path / "yadgar"))
    server.init_engines(db_path=str(tmp_path / "dlq_tools.db"))
    yield
    server.shutdown()


def _seed_dlq_entry(filename: str = "0001_seed.json") -> tuple[str, dict]:
    """Drop a fake DLQ entry on disk and return (filename, sidecar_meta)."""
    fq = server._get_file_queue()
    main = fq.dlq_dir / filename
    main.write_text(json.dumps({"op": "memorize", "payload": {"content": "x"}}))
    meta = {
        "op_type": "memorize",
        "first_failed_at": "2026-05-08T07:00:00+00:00",
        "last_failed_at": "2026-05-08T07:01:00+00:00",
        "attempts": 3,
        "classification": "permanent",
        "last_error": "Client error '400 Bad Request'",
        "moved_to_dlq_at": "2026-05-08T07:01:00+00:00",
    }
    (fq.dlq_dir / (filename + ".error.json")).write_text(json.dumps(meta))
    return filename, meta


class TestDlqInspect:
    def test_empty_dlq_returns_empty_list(self):
        assert server.dlq_inspect() == []

    def test_lists_seeded_entry(self):
        fname, meta = _seed_dlq_entry()
        out = server.dlq_inspect()
        assert len(out) == 1
        e = out[0]
        assert e["file"] == fname
        assert e["op_type"] == "memorize"
        assert e["attempts"] == 3
        assert e["classification"] == "permanent"
        assert "400" in e["last_error"]
        assert e["file_size"] is not None and e["file_size"] > 0

    def test_corrupt_sidecar_does_not_break_inspect(self):
        fq = server._get_file_queue()
        main = fq.dlq_dir / "0002_corrupt.json"
        main.write_text("{}")
        (fq.dlq_dir / "0002_corrupt.json.error.json").write_text("{not json")
        out = server.dlq_inspect()
        assert len(out) == 1
        assert out[0]["op_type"] == "unknown"


class TestDlqRequeue:
    def test_requeue_moves_back_to_queue(self):
        fq = server._get_file_queue()
        fname, _ = _seed_dlq_entry()
        result = server.dlq_requeue(fname)
        assert result["requeued"] is True
        assert (fq.queue_dir / fname).exists()
        assert not (fq.dlq_dir / fname).exists()
        assert not (fq.dlq_dir / (fname + ".error.json")).exists()

    def test_requeue_resets_attempt_tracker(self):
        fname, _ = _seed_dlq_entry()
        # Pre-seed a stale tracker entry
        from yadgar.core.file_queue import _Attempt

        if server._queue_drainer is not None:
            server._queue_drainer._attempts[fname] = _Attempt(count=99, next_retry_at=1e18)
        server.dlq_requeue(fname)
        if server._queue_drainer is not None:
            assert fname not in server._queue_drainer._attempts

    def test_requeue_blocks_path_traversal(self):
        for bad in ["../etc/passwd", "foo/bar.json", "..\\windows\\sys", ".bashrc", ".events.log"]:
            r = server.dlq_requeue(bad)
            assert r["requeued"] is False, f"path traversal allowed: {bad!r} → {r}"

    def test_requeue_missing_file_returns_error(self):
        r = server.dlq_requeue("nonexistent.json")
        assert r["requeued"] is False
        assert "not found" in r["error"].lower()

    def test_requeue_collision_with_existing_queue_file(self):
        fq = server._get_file_queue()
        fname, _ = _seed_dlq_entry("0003_collide.json")
        # Plant a same-named file already in queue
        (fq.queue_dir / fname).write_text("{}")
        r = server.dlq_requeue(fname)
        assert r["requeued"] is False
        assert "queue" in r["error"].lower()
        # DLQ file must still be there (no destructive partial move)
        assert (fq.dlq_dir / fname).exists()


class TestBuildDlqAlertText:
    def test_empty_dlq_returns_empty_string(self):
        assert server._build_dlq_alert_text() == ""

    def test_non_empty_dlq_includes_op_type(self):
        _seed_dlq_entry()
        text = server._build_dlq_alert_text()
        assert "memorize" in text
        assert "DLQ Alert" in text
        assert "dlq_requeue" in text  # actionable hint
