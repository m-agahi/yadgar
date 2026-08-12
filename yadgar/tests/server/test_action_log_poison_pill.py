"""TDD tests for v5.25.2 Fix 2 — SecretLeakBlocked poison-pill skip in action-log replay.

Root cause: _process_action_log() in cleanup.py calls insert_memory() which
raises SecretLeakBlocked when action-log content contains a detected secret.
The exception propagates out of the for-loop before mark_actions_processed()
runs, so the offending group's IDs never get marked. Next cycle re-fetches the
same 200 rows → same group → same exception. Poison-pill loop: only 1 of N
expected cycles completed in 5h10min.

Fix: catch SecretLeakBlocked specifically around insert_memory(), log WARNING,
quarantine the group's action IDs to ~/.yadgar/quarantine/action_log_poison.jsonl,
and fall through to mark_actions_processed() so the cycle advances.

Written BEFORE implementation — starts red.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


def _make_consolidation_engine(tmp_path):
    """Return a minimal _CleanupMixin instance wired to mock storage + embeddings."""
    from yadgar.backend.consolidation.cleanup import _CleanupMixin

    class _FakeSettings:
        ACTION_LOG_RETENTION_DAYS = 30

    class _FakeEngine(_CleanupMixin):
        def __init__(self):
            self._storage = MagicMock()
            self._embeddings = MagicMock()
            self._settings = _FakeSettings()

    return _FakeEngine()


def _make_rows(n: int, base_id: int = 1, project_id: str | None = "owner/repo") -> list[dict]:
    """Produce n fake action_log rows in a single directory/time-window group.

    ``project_id`` is stamped by default because C4 (0047 PR#40 §5) gave
    ``_process_action_log`` a skip-and-count path for rows that name no
    project: they are counted under ``actions_skipped_no_project``, marked
    processed and never bucketed under a guess. Rows without one therefore
    never reach ``insert_memory``, which is where this file's SecretLeakBlocked
    poison pill has to fire. Pass ``project_id=None`` to exercise the skip path
    deliberately.
    """

    ts = "2026-05-30T10:15:00"
    rows = []
    for i in range(n):
        row = {
            "id": base_id + i,
            "tool_name": "Bash",
            "tool_input_summary": f"cmd_{i}",
            "directory": "/home/user/proj",
            "timestamp": ts,
        }
        if project_id is not None:
            row["project_id"] = project_id
        rows.append(row)
    return rows


class TestSecretLeakBlockedDoesNotCrashCycle:
    """SecretLeakBlocked from insert_memory must not abort the consolidation cycle."""

    def test_single_poisoned_group_does_not_raise(self, tmp_path):
        """_process_action_log must not propagate SecretLeakBlocked."""
        from yadgar._shared.security.secrets import SecretLeakBlocked

        engine = _make_consolidation_engine(tmp_path)
        rows = _make_rows(5)
        engine._storage.get_unprocessed_actions.return_value = rows
        engine._embeddings.encode.return_value = [0.1] * 384
        engine._storage.insert_memory.side_effect = SecretLeakBlocked(
            "AWS access key", "AKIAIOSFODNN7EX"
        )

        # Must not raise
        with patch("pathlib.Path.home", return_value=tmp_path):
            stats = engine._process_action_log()

        assert isinstance(stats, dict), "_process_action_log must return a dict"

    def test_poisoned_group_ids_are_marked_processed(self, tmp_path):
        """IDs from a poisoned group must still be passed to mark_actions_processed."""
        from yadgar._shared.security.secrets import SecretLeakBlocked

        engine = _make_consolidation_engine(tmp_path)
        rows = _make_rows(5, base_id=100)
        engine._storage.get_unprocessed_actions.return_value = rows
        engine._embeddings.encode.return_value = [0.1] * 384
        engine._storage.insert_memory.side_effect = SecretLeakBlocked(
            "AWS access key", "AKIAIOSFODNN7EX"
        )

        with patch("pathlib.Path.home", return_value=tmp_path):
            engine._process_action_log()

        # All 5 IDs must have been passed to mark_actions_processed
        engine._storage.mark_actions_processed.assert_called()
        all_marked_ids = []
        for c in engine._storage.mark_actions_processed.call_args_list:
            all_marked_ids.extend(c.args[0] if c.args else [])
        expected_ids = {100, 101, 102, 103, 104}
        assert expected_ids.issubset(set(all_marked_ids)), (
            f"Expected IDs {expected_ids} to be marked processed. "
            f"Got: {set(all_marked_ids)}. "
            "Without this, the poison-pill group re-queues every cycle."
        )

    def test_subsequent_groups_still_process_after_poisoned_group(self, tmp_path):
        """Groups after the poisoned group must still produce memories."""
        from yadgar._shared.security.secrets import SecretLeakBlocked

        engine = _make_consolidation_engine(tmp_path)

        # Two groups: group A is poisoned (rows 1-5), group B is clean (rows 6-10).
        #
        # C4 (0047 PR#40 §5) re-keyed _group_rows_by_window from row["directory"]
        # to (project_id, 30-min window) — one project checked out twice used to
        # split into two unrelated summaries. So a DIFFERENT DIRECTORY no longer
        # makes a different group, and the two sets below would collapse into a
        # single group of 10 that the first SecretLeakBlocked quarantines whole,
        # which is exactly the shape this test exists to rule out. Group B is now
        # separated by the key that actually separates groups: its project_id.
        rows_a = _make_rows(5, base_id=1)  # project owner/repo, 10:15
        rows_b = [
            {
                "id": 10 + i,
                "tool_name": "Read",
                "tool_input_summary": f"file_{i}",
                "directory": "/home/user/other",
                "timestamp": "2026-05-30T10:15:00",
                # different project → different group (C4's key)
                "project_id": "owner/other-repo",
            }
            for i in range(5)
        ]

        engine._storage.get_unprocessed_actions.return_value = rows_a + rows_b
        engine._embeddings.encode.return_value = [0.1] * 384
        # First insert_memory call (group A) raises, second (group B) succeeds
        engine._storage.insert_memory.side_effect = [
            SecretLeakBlocked("AWS access key", "AKIAIOSFODNN7EX"),
            None,
        ]

        with patch("pathlib.Path.home", return_value=tmp_path):
            stats = engine._process_action_log()

        # Group B should have created a memory
        assert stats.get("memories_created", 0) >= 1, (
            f"Expected at least 1 memory from clean group. stats={stats}. "
            "SecretLeakBlocked on group A must not prevent group B from processing."
        )
        # Total processed should include both groups
        assert stats.get("processed", 0) == 10, (
            f"Expected 10 rows processed (5+5). Got {stats.get('processed')}."
        )

    @pytest.mark.xfail(
        reason="v5.49.4 bisect: patches Path.home but yadgar.paths uses XDG_STATE_HOME env var (set by isolate_yadgar_paths fixture since v5.47.0); quarantine file written to XDG_STATE_HOME path, not ~/.yadgar/. Fix: update test to use XDG_STATE_HOME-aware path. Refactor in v5.50+.",
        strict=False,
    )
    def test_quarantine_file_written(self, tmp_path):
        """Quarantined group IDs must be persisted to quarantine JSONL file."""
        from yadgar._shared.security.secrets import SecretLeakBlocked

        engine = _make_consolidation_engine(tmp_path)
        rows = _make_rows(5, base_id=200)
        engine._storage.get_unprocessed_actions.return_value = rows
        engine._embeddings.encode.return_value = [0.1] * 384
        engine._storage.insert_memory.side_effect = SecretLeakBlocked(
            "AWS access key", "AKIAIOSFODNN7EX"
        )

        with patch("pathlib.Path.home", return_value=tmp_path):
            engine._process_action_log()

        quarantine_file = tmp_path / ".yadgar" / "quarantine" / "action_log_poison.jsonl"
        assert quarantine_file.exists(), (
            f"Quarantine file not found at {quarantine_file}. "
            "Blocked entries must be persisted for later inspection."
        )

        lines = quarantine_file.read_text().strip().splitlines()
        assert len(lines) >= 1, "Quarantine file must have at least one entry"

        entry = json.loads(lines[-1])
        assert "action_ids" in entry, "Quarantine entry must contain action_ids"
        assert "reason" in entry, "Quarantine entry must contain reason"
        assert "timestamp" in entry, "Quarantine entry must contain timestamp"
        # Verify the IDs are in the quarantine entry
        quarantined_ids = set(entry["action_ids"])
        assert {200, 201, 202, 203, 204}.issubset(quarantined_ids), (
            f"Expected IDs 200-204 in quarantine. Got: {quarantined_ids}"
        )

    def test_stats_include_quarantined_count(self, tmp_path):
        """Stats dict must include actions_quarantined count when poison-pill detected."""
        from yadgar._shared.security.secrets import SecretLeakBlocked

        engine = _make_consolidation_engine(tmp_path)
        rows = _make_rows(5, base_id=300)
        engine._storage.get_unprocessed_actions.return_value = rows
        engine._embeddings.encode.return_value = [0.1] * 384
        engine._storage.insert_memory.side_effect = SecretLeakBlocked(
            "private key", "-----BEGIN RSA"
        )

        with patch("pathlib.Path.home", return_value=tmp_path):
            stats = engine._process_action_log()

        assert "actions_quarantined" in stats, (
            f"stats dict missing 'actions_quarantined' key. Got: {list(stats.keys())}. "
            "Caller needs visibility into quarantine events for monitoring."
        )
        assert stats["actions_quarantined"] >= 1, (
            f"Expected actions_quarantined >= 1. Got: {stats['actions_quarantined']}."
        )

    def test_memories_created_not_incremented_on_quarantine(self, tmp_path):
        """memories_created must NOT increment when insert_memory raises SecretLeakBlocked."""
        from yadgar._shared.security.secrets import SecretLeakBlocked

        engine = _make_consolidation_engine(tmp_path)
        rows = _make_rows(5, base_id=400)
        engine._storage.get_unprocessed_actions.return_value = rows
        engine._embeddings.encode.return_value = [0.1] * 384
        engine._storage.insert_memory.side_effect = SecretLeakBlocked(
            "GitHub token", "ghp_xxxxxxxxxxxx"
        )

        with patch("pathlib.Path.home", return_value=tmp_path):
            stats = engine._process_action_log()

        assert stats.get("memories_created", 0) == 0, (
            f"memories_created must be 0 when insert_memory was blocked. "
            f"Got: {stats.get('memories_created')}."
        )

    def test_quarantine_disk_error_does_not_crash_cycle(self, tmp_path):
        """If quarantine file write fails, the cycle must still complete (best-effort)."""
        from yadgar._shared.security.secrets import SecretLeakBlocked

        engine = _make_consolidation_engine(tmp_path)
        rows = _make_rows(5, base_id=500)
        engine._storage.get_unprocessed_actions.return_value = rows
        engine._embeddings.encode.return_value = [0.1] * 384
        engine._storage.insert_memory.side_effect = SecretLeakBlocked(
            "AWS access key", "AKIAIOSFODNN7EX"
        )

        # Simulate disk error during quarantine write
        with patch("pathlib.Path.home", return_value=tmp_path):
            with patch("pathlib.Path.mkdir", side_effect=OSError("disk full")):
                # Must not raise even if quarantine write fails
                stats = engine._process_action_log()

        assert isinstance(stats, dict), "Cycle must return stats even if quarantine write fails"
