"""Tests for the dlq_inspect / dlq_requeue MCP tools."""

import json

import pytest

from yadgar.core import server


@pytest.fixture(autouse=True)
def _engines(tmp_path):
    """Init engines against an isolated DB.

    R3 note: do NOT override YADGAR_DATA_DIR here. The autouse
    ``_isolate_file_queue`` fixture already routes the queue to a per-test
    dir, and ``_unit_backend_harness`` (which fires for this module) builds
    the FileQueue under THAT path before this fixture runs — a second
    override here would desync ``fq.dlq_dir`` from the env-derived path
    ``_build_dlq_alert_text`` reads.
    """
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
        from yadgar.backend.queue_drainer import _Attempt

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


class TestInspectNamesTheKeyItsSiblingsTake:
    """``dlq_inspect`` must emit the key ``dlq_dismiss``/``dlq_requeue`` accept.

    Found 2026-08-15 while trying to clear a 1,104-entry backlog: the entries
    carry ``file``, but the docstring says "Each entry has a filename you can
    pass to dlq_requeue()" and both sibling tools take ``filename=``. Reading
    the documented key off a real result yields ``None`` for every row, which
    reads as "the DLQ is unmanageable" rather than "look under a different
    key" — the backlog cannot be scripted away without knowing the source.
    """

    def test_entry_exposes_filename(self):
        from yadgar.core.server.tools.admin_dlq import dlq_inspect

        fname, _ = _seed_dlq_entry("0007_namekey.json")
        entry = next(e for e in dlq_inspect() if e.get("file") == fname)
        assert entry.get("filename") == fname, (
            "dlq_inspect promises `filename` and its siblings take `filename=`, "
            "but the entry does not carry that key"
        )

    def test_filename_round_trips_into_dismiss(self):
        """The documented key must actually work as the sibling's argument."""
        from yadgar.core.server.tools.admin_dlq import dlq_dismiss, dlq_inspect

        _seed_dlq_entry("0008_roundtrip.json")
        entry = next(e for e in dlq_inspect() if e.get("op_type") == "memorize")
        result = dlq_dismiss(entry["filename"])
        assert result.get("dismissed") or result.get("ok"), result

    def test_legacy_file_key_is_kept(self):
        """``file`` stays — anything already reading it must not break."""
        from yadgar.core.server.tools.admin_dlq import dlq_inspect

        fname, _ = _seed_dlq_entry("0009_legacy.json")
        entry = next(e for e in dlq_inspect() if e.get("file") == fname)
        assert entry["file"] == entry["filename"]
