"""Episode storage mixin — insert, fetch, and prune episode rows."""

import logging
from datetime import UTC, datetime, timedelta

from yadgar._shared.observability.observe import observe

_log = logging.getLogger(__name__)


class _EpisodeMixin:
    """Episode CRUD — mixed into StorageEngine."""

    # ------------------------------------------------------------------ Episodes

    @observe(tier="stage")
    def insert_episode(self, episode: dict) -> int:
        eid = self._next_id("episode")
        self._q(
            "CREATE type::record('episode', $id) SET "
            "session_id = $session_id, timestamp = $timestamp, "
            "directory = $directory, raw_content = $raw_content, "
            "overlap_start = $overlap_start, overlap_end = $overlap_end",
            {
                "id": eid,
                "session_id": episode["session_id"],
                "timestamp": episode.get("timestamp", self._now_iso()),
                "directory": episode["directory"],
                "raw_content": episode["raw_content"],
                "overlap_start": episode.get("overlap_start"),
                "overlap_end": episode.get("overlap_end"),
            },
        )
        return eid

    def get_session_episodes(self, session_id: str) -> list[dict]:
        rows = self._q(
            "SELECT * FROM episode WHERE session_id = $sid ORDER BY id",
            {"sid": session_id},
        )
        return self._rows_to_dicts(rows)

    def get_episodes_since(self, episode_id: int) -> list[dict]:
        rows = self._q(
            "SELECT * FROM episode WHERE id > $eid ORDER BY id",
            {"eid": episode_id},
        )
        return self._rows_to_dicts(rows)

    @observe(tier="stage")
    def get_max_episode_id(self) -> int:
        row = self._q("SELECT val FROM counter:episode")
        if row:
            return int(row[0].get("val", 0))
        return 0

    def get_all_episodes(self) -> list[dict]:
        """Return all episodes ordered by timestamp ascending."""
        rows = self._q("SELECT * FROM episode ORDER BY timestamp ASC")
        return self._rows_to_dicts(rows)

    def get_recent_episodes(self, limit: int) -> list[dict]:
        """Return the most recent ``limit`` episodes, ordered ascending by timestamp.

        Used by _check_temporal_order to avoid scanning the full episode table
        (which can be huge) on every consolidation cycle.
        """
        rows = self._q(
            "SELECT * FROM episode ORDER BY timestamp DESC LIMIT $lim",
            {"lim": limit},
        )
        # Re-order ascending so callers see chronological order
        return list(reversed(self._rows_to_dicts(rows)))

    @observe(tier="stage")
    def prune_old_episodes(self, older_than_days: int = 14) -> int:
        """Delete episode rows older than ``older_than_days``.

        Returns the number of rows deleted (approximate — counted before delete).
        Prevents the episode table from growing without bound and keeps
        _check_temporal_order O(recent episodes) instead of O(all time).
        """
        cutoff = (datetime.now(UTC) - timedelta(days=older_than_days)).isoformat()
        rows = self._q(
            "SELECT count() AS c FROM episode WHERE timestamp < $cutoff GROUP ALL",
            {"cutoff": cutoff},
        )
        n = int(rows[0]["c"]) if rows and rows[0].get("c") else 0
        self._q(
            "DELETE FROM episode WHERE timestamp < $cutoff",
            {"cutoff": cutoff},
        )
        return n

    @observe(tier="stage")
    def get_episode_session_id(self, episode_id: int) -> str | None:
        rows = self._q(f"SELECT session_id FROM episode:{episode_id}")
        return rows[0].get("session_id") if rows else None
