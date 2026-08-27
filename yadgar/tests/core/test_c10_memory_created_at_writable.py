"""C10 — task #318: update_memory_fields(created_at=...) must actually write the field.

Pre-C10: ``created_at`` was absent from ``_MEMORY_UPDATABLE_FIELDS`` so any
``update_memory_fields(memory_id, created_at=past)`` call SILENTLY DROPPED the
key. ``test_memory_behavior.py:49`` ages rows by setting ``created_at`` this
way, and the existing aging tests pass by coincidence: they assert on
``compression_level`` / ``content`` (which DO survive) but never assert that
``created_at`` itself was rewritten. The decay path's ``_set_memory_age``
helper looks like it works, but every backdate since the field was added has
been a no-op for ``created_at``.

This test asserts the post-C10 contract: ``created_at`` is in
``_MEMORY_UPDATABLE_FIELDS``, the helper writes it, and a subsequent
``get_memory`` reads back the value the caller supplied.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from yadgar.core import server
from yadgar.tests.core.conftest import memorize_scoped


@pytest.fixture(autouse=True)
def _engines(tmp_path):
    server.init_engines(
        db_path=str(tmp_path / "c10_created_at.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


class TestMemoryCreatedAtWritable:
    """``created_at`` must be in ``_MEMORY_UPDATABLE_FIELDS`` and must persist."""

    def test_created_at_in_updatable_fields(self):
        from yadgar._shared.storage.client import _MEMORY_UPDATABLE_FIELDS

        assert "created_at" in _MEMORY_UPDATABLE_FIELDS, (
            "created_at missing from _MEMORY_UPDATABLE_FIELDS — "
            "update_memory_fields(created_at=...) silently no-ops. "
            "This is exactly task #318: the test_memory_behavior aging helper "
            "has been backdating last_accessed but created_at has stayed at "
            "the row's actual creation time, so all decay tests are running "
            "against the wrong baseline."
        )

    def test_update_memory_fields_writes_created_at(self):
        """End-to-end: a backdate call must round-trip through get_memory()."""
        result = memorize_scoped(
            "A fact whose age we want to control.",
            "/home/user/c10",
            ["test"],
        )
        mid = result["id"]
        storage = server._get_storage()

        past = (datetime.now(UTC) - timedelta(days=30)).isoformat()
        storage.update_memory_fields(mid, created_at=past)

        row = storage.get_memory(mid)
        assert row is not None
        # The stored created_at must be the backdated value, not the original.
        stored = row.get("created_at")
        assert stored is not None, (
            "created_at is NULL after update_memory_fields — the write was dropped "
            "by the frozenset filter at storage/memory.py:1224."
        )
        # Compare on parsed timestamps so ISO formatting drift does not break the assertion.
        stored_dt = datetime.fromisoformat(stored.replace("Z", "+00:00"))
        past_dt = datetime.fromisoformat(past.replace("Z", "+00:00"))
        assert abs((stored_dt - past_dt).total_seconds()) < 2.0, (
            f"created_at was not updated: stored={stored_dt!r} expected~={past_dt!r}"
        )

    def test_set_memory_age_helper_round_trips(self):
        """The _set_memory_age helper used by test_memory_behavior must work
        for BOTH last_accessed AND created_at (task #318). Pre-C10 only the
        former round-tripped.
        """
        # Import the helper from the file it lives in (not re-exported).
        from yadgar.tests.core.test_memory_behavior import _set_memory_age

        result = memorize_scoped(
            "Fact for the helper round-trip test.",
            "/home/user/c10",
            ["test"],
        )
        mid = result["id"]
        storage = server._get_storage()

        hours_ago = 7 * 24  # one week
        _set_memory_age(mid, hours_ago=hours_ago)

        row = storage.get_memory(mid)
        assert row is not None
        stored_created = row.get("created_at")
        assert stored_created is not None
        stored_dt = datetime.fromisoformat(stored_created.replace("Z", "+00:00"))
        now = datetime.now(UTC)
        delta_h = (now - stored_dt).total_seconds() / 3600
        # Allow a 1h slop window so the test is stable across CI jitter.
        assert abs(delta_h - hours_ago) < 1.0, (
            f"_set_memory_age did not round-trip created_at: expected~{hours_ago}h ago, "
            f"got {delta_h:.2f}h ago"
        )
