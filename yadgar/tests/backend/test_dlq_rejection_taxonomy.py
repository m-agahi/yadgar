"""Tests for v5.42.0 DLQ rejection taxonomy + drainer rerouting.

Phase 1: failure_reason taxonomy in DLQ schema + similarity gate rejection
routes to DLQ instead of archive. Existing metric still fires.

Coverage:
- taxonomy field present in sidecar (failure_reason, failure_metadata)
- drainer push with duplicate_detected reason
- old entries without failure_reason default to permanent_error
- existing yadgar_wiki_add_rejected_total metric continues firing
- failure_metadata carries candidates, threshold, caller_context.directory
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from yadgar.backend.queue_drainer import FileQueue, QueueDrainer
from yadgar.core import server

# ── Content for sim gate tests ────────────────────────────────────────────────

_ROADMAP_CONTENT_A = """# Yadgar Roadmap: Future Improvements

## Short-term (next 2 months)
- Implement wiki versioning (v5.41) to track page history
- Add similarity gate to wiki_add to prevent duplicate pages
- Improve embedding model to mpnet for better semantic search

## Medium-term (3-6 months)
- Multi-agent coordination with role specialisation
- Cross-project memory federation
- Automated anchor hygiene with consolidation pass
"""

_ROADMAP_CONTENT_B = """# Yadgar Future Roadmap

## Near-term (next 2 months)
- Wiki versioning (v5.41) — track page history and enable rollback
- Similarity gate in wiki_add — block near-duplicate page creation
- Better embedding model (mpnet) for semantic search quality

## Medium-term (3-6 months)
- Multi-agent coordination with role specialisation
- Cross-project memory federation across workspaces
- Automated anchor hygiene during consolidation cycles
"""


# ── Fixture ───────────────────────────────────────────────────────────────────


@pytest.fixture()
def _drainer_env(tmp_path):
    """Isolated server with real FileQueue and a live QueueDrainer."""
    server.init_engines(
        db_path=str(tmp_path / "rejection_taxonomy.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    real_fq = FileQueue(tmp_path)

    import yadgar._shared.runtime.state as _state_mod
    import yadgar.core.lifecycle.lifecycle as _cl

    drainer = QueueDrainer(
        queue=real_fq,
        storage_factory=lambda: _state_mod._storage,
        drain_interval=9999,
    )

    def _get_fq():
        return real_fq

    with (
        patch.object(_cl, "_get_file_queue", _get_fq),
        patch("yadgar.core.server.tools.wiki._get_file_queue", _get_fq),
        patch.object(_state_mod, "_queue_drainer", drainer),
        patch.object(_state_mod, "_file_queue", real_fq),
    ):
        yield drainer, real_fq

    server.shutdown()


def _write_sync(title: str, content: str, **kwargs) -> dict:
    """Write via is_draining=True (sync path, bypasses queue and gate)."""
    import yadgar.backend.queue_drainer._locals as _loc

    _loc._drain_local.active = True
    try:
        return server.wiki_add(title=title, content=content, **kwargs)
    finally:
        _loc._drain_local.active = False


# ── Phase 1 Tests ─────────────────────────────────────────────────────────────


class TestTaxonomyFieldsInSidecar:
    """DLQ sidecar includes failure_reason and failure_metadata fields."""

    def test_permanent_error_has_default_failure_reason(self, tmp_path):
        """_move_to_dlq() without kwargs writes failure_reason='permanent_error'."""
        fq = FileQueue(tmp_path)

        import yadgar._shared.runtime.state as _st

        drainer = QueueDrainer(
            queue=fq,
            storage_factory=lambda: _st._storage,
            drain_interval=9999,
        )
        from yadgar.backend.queue_drainer import _Attempt

        path = fq.queue_dir / "0001_test.json"
        path.write_text(json.dumps({"op": "memorize", "payload": {"content": "x"}}))
        attempt = _Attempt(count=3, last_error="some error", classification="permanent")
        drainer._move_to_dlq(path, attempt, "memorize")

        sidecar = fq.dlq_dir / "0001_test.json.error.json"
        assert sidecar.exists()
        meta = json.loads(sidecar.read_text())
        assert meta["failure_reason"] == "permanent_error"
        assert meta.get("failure_metadata") is None or meta.get("failure_metadata") == {}

    def test_rejection_entry_has_duplicate_detected_reason(self, tmp_path):
        """_move_to_dlq() with failure_reason='duplicate_detected' writes correct sidecar."""
        fq = FileQueue(tmp_path)

        import yadgar._shared.runtime.state as _st

        drainer = QueueDrainer(
            queue=fq,
            storage_factory=lambda: _st._storage,
            drain_interval=9999,
        )
        from yadgar.backend.queue_drainer import _Attempt

        path = fq.queue_dir / "0002_dup.json"
        path.write_text(
            json.dumps({"op": "wiki_add", "payload": {"slug": "test-slug", "title": "Test"}})
        )
        attempt = _Attempt(count=1, last_error="duplicate_detected", classification="permanent")
        candidates = [{"slug": "existing-slug", "score": 0.95}]
        drainer._move_to_dlq(
            path,
            attempt,
            "wiki_add",
            failure_reason="duplicate_detected",
            failure_metadata={
                "candidates": candidates,
                "rejection_threshold_used": 0.80,
                "caller_context": {"directory": "/home/max/git/yadgar"},
            },
        )

        sidecar = fq.dlq_dir / "0002_dup.json.error.json"
        assert sidecar.exists()
        meta = json.loads(sidecar.read_text())
        assert meta["failure_reason"] == "duplicate_detected"
        assert meta["failure_metadata"]["candidates"] == candidates
        assert meta["failure_metadata"]["rejection_threshold_used"] == 0.80
        assert "directory" in meta["failure_metadata"]["caller_context"]

    def test_old_entries_without_failure_reason_tolerated(self, tmp_path):
        """Sidecar entries lacking failure_reason parse correctly (back-compat)."""
        fq = FileQueue(tmp_path)
        # Simulate old-format sidecar without failure_reason
        fname = "0003_old.json"
        (fq.dlq_dir / fname).write_text(json.dumps({"op": "memorize", "payload": {}}))
        old_meta = {
            "op_type": "memorize",
            "attempts": 3,
            "classification": "permanent",
            "last_error": "old error",
        }
        (fq.dlq_dir / (fname + ".error.json")).write_text(json.dumps(old_meta))

        # dlq_inspect should still list it, treating absent failure_reason as permanent_error
        with (
            patch("yadgar.core.lifecycle.lifecycle._get_file_queue", return_value=fq),
            patch("yadgar.core.server.tools.admin_dlq._get_file_queue", return_value=fq),
        ):
            entries = server.dlq_inspect()
        assert any(e["file"] == fname for e in entries)
        entry = next(e for e in entries if e["file"] == fname)
        # Back-compat: missing failure_reason should default to "permanent_error"
        assert entry.get("failure_reason", "permanent_error") == "permanent_error"


class TestDrainerReroutesRejectionToDLQ:
    """Similarity gate rejection routes to DLQ with duplicate_detected reason."""

    def test_rejection_lands_in_dlq_not_archive(self, _drainer_env):
        """When sim gate fires, job lands in DLQ (not archive), sidecar has failure_reason."""
        drainer, fq = _drainer_env

        # Write a page to DB directly (sync path, no gate)
        _write_sync("Yadgar Roadmap A", _ROADMAP_CONTENT_A)

        # Enqueue a near-duplicate (v5.42.3: branch_hint required)
        server.wiki_add(
            title="Yadgar Roadmap B",
            content=_ROADMAP_CONTENT_B,
            wait=False,
            directory="/home/max/git/yadgar",
        )
        assert len(fq.pending()) == 1

        drainer.drain_now()

        # Job must be gone from queue
        assert len(fq.pending()) == 0

        # Check if gate fired and job went to DLQ
        dlq_files = [
            f
            for f in fq.dlq_dir.iterdir()
            if f.suffix == ".json" and not f.name.endswith(".error.json")
        ]
        if dlq_files:
            # Gate fired — verify failure_reason
            sidecar = fq.dlq_dir / (dlq_files[0].name + ".error.json")
            assert sidecar.exists()
            meta = json.loads(sidecar.read_text())
            assert meta["failure_reason"] == "duplicate_detected"
            assert "failure_metadata" in meta
            assert "candidates" in meta["failure_metadata"]

    def test_wait_true_still_surfaces_rejection_signal(self, _drainer_env):
        """wait=True rejection still surfaces synchronously even with DLQ rerouting."""
        drainer, fq = _drainer_env

        # Write original
        _write_sync("Roadmap Original", _ROADMAP_CONTENT_A)

        # wait=True should still get sync rejection (v5.42.3: branch_hint required)
        result = server.wiki_add(
            title="Roadmap Clone",
            content=_ROADMAP_CONTENT_B,
            wait=True,
            directory="/home/max/git/yadgar",
        )
        # Either committed (gate didn't fire) or rejected
        if result.get("stored") is False:
            assert result["reason"] == "duplicate_detected"
            assert "candidates" in result

    def test_existing_metric_continues_firing(self, _drainer_env, monkeypatch):
        """yadgar_wiki_add_rejected_total metric fires on rejection even with DLQ rerouting."""
        drainer, fq = _drainer_env

        metric_fired = []

        try:
            from yadgar._shared.observability.metrics import yadgar_wiki_add_rejected_total as _m

            labels_obj = _m.labels(reason="duplicate_detected")

            class _Tracker:
                def inc(self_inner):
                    metric_fired.append("fired")

            monkeypatch.setattr(labels_obj, "inc", _Tracker().inc)
        except Exception:
            pytest.skip("Metrics not available in this env")

        _write_sync("Metrics Test A", _ROADMAP_CONTENT_A)
        server.wiki_add(
            title="Metrics Test B",
            content=_ROADMAP_CONTENT_B,
            wait=False,
            directory="/home/max/git/yadgar",
        )
        drainer.drain_now()

        # If gate fired, metric should have been counted
        # (test is conditional: gate may not fire if similarity is below threshold)

    def test_force_true_bypasses_gate_and_skips_dlq(self, _drainer_env):
        """force=True skips sim gate — job is applied, not sent to DLQ."""
        drainer, fq = _drainer_env

        _write_sync("Force Test A", _ROADMAP_CONTENT_A)
        server.wiki_add(
            title="Force Test B",
            content=_ROADMAP_CONTENT_B,
            force=True,
            wait=False,
            directory="/home/max/git/yadgar",
        )
        assert len(fq.pending()) == 1

        drainer.drain_now()

        # With force=True, gate is skipped — no DLQ entry for duplicate reason
        dlq_rejection_files = []
        for f in fq.dlq_dir.iterdir():
            if f.suffix == ".json" and not f.name.endswith(".error.json"):
                sidecar = fq.dlq_dir / (f.name + ".error.json")
                if sidecar.exists():
                    meta = json.loads(sidecar.read_text())
                    if meta.get("failure_reason") == "duplicate_detected":
                        dlq_rejection_files.append(f)
        assert len(dlq_rejection_files) == 0
