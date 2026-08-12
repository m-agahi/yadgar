"""Action log and file hash helpers.

_QueueMixin provides:
  - upsert_file_hash / get_file_hash / get_filepath_by_hash
  - insert_action_log / get_unprocessed_actions / mark_actions_processed
  - prune_processed_action_log
"""

import logging

from yadgar._shared.observability.observe import observe

_log = logging.getLogger(__name__)


class _QueueMixin:
    """Action log and file hash CRUD — mixed into StorageEngine."""

    # ------------------------------------------------------------------ File Hashes

    @observe(tier="stage")
    def upsert_file_hash(self, filepath: str, hash_value: str):
        """§6 Q15: read-modify-write wrapped per-call to avoid duplicate rows under concurrency."""
        now = self._now_iso()
        rows = self._q(
            "SELECT id FROM file_hash WHERE filepath = $fp LIMIT 1",
            {"fp": filepath},
        )
        if rows:
            fid = self._extract_id(rows[0]["id"])
            self._q(
                "BEGIN TRANSACTION;\n"
                "UPDATE type::record('file_hash', $id) SET hash = $hash, last_checked = $now;\n"
                "COMMIT TRANSACTION",
                {"id": fid, "hash": hash_value, "now": now},
            )
        else:
            fid = self._next_id("file_hash")
            self._q(
                "BEGIN TRANSACTION;\n"
                "CREATE type::record('file_hash', $id) SET "
                "filepath = $fp, hash = $hash, last_checked = $now;\n"
                "COMMIT TRANSACTION",
                {"id": fid, "fp": filepath, "hash": hash_value, "now": now},
            )

    def get_file_hash(self, filepath: str) -> str | None:
        rows = self._q(
            "SELECT hash FROM file_hash WHERE filepath = $fp LIMIT 1",
            {"fp": filepath},
        )
        return rows[0]["hash"] if rows else None

    def get_filepath_by_hash(self, hash_value: str) -> str | None:
        rows = self._q(
            "SELECT filepath FROM file_hash WHERE hash = $hash LIMIT 1",
            {"hash": hash_value},
        )
        return rows[0]["filepath"] if rows else None

    # ------------------------------------------------------------------ Action Log

    def insert_action_log(
        self,
        tool_name: str,
        tool_input_summary: str,
        directory: str,
        session_id: str,
        timestamp: str,
        project_id: str = "",
    ):
        """Insert one action_log row.

        C4 (0047 PR#40 §5): ``project_id`` carries the value the PRODUCING
        session minted host-side. The consumer is
        ``consolidation.cleanup._group_rows_by_window``, which groups on it —
        so a row that arrives without one is skipped and counted there rather
        than being bucketed under a guess. Empty string is the honest "the
        producer did not stamp one" marker; nothing here derives a value.

        C11: ``action_log`` is SCHEMALESS, so this write needs no migration to
        land — but migration 033 must still add
        ``DEFINE FIELD project_id ON TABLE action_log TYPE option<string>``
        plus an index, so the column is declared and queryable rather than
        merely present on rows written after this car.
        """
        aid = self._next_id("action_log")
        self._q(
            "CREATE type::record('action_log', $id) SET "
            "tool_name = $tool_name, tool_input_summary = $tis, "
            "directory = $directory, session_id = $sid, "
            "project_id = $project_id, "
            "timestamp = $ts, processed = false",
            {
                "id": aid,
                "tool_name": tool_name,
                "tis": tool_input_summary,
                "directory": directory,
                "sid": session_id,
                "project_id": project_id,
                "ts": timestamp,
            },
        )

    def get_unprocessed_actions(self, limit: int = 200) -> list[dict]:
        rows = self._q(
            "SELECT * FROM action_log WHERE processed = false ORDER BY timestamp ASC LIMIT $lim",
            {"lim": limit},
        )
        return self._rows_to_dicts(rows)

    @observe(tier="stage")
    def mark_actions_processed(self, ids: list[int]):
        if not ids:
            return
        for aid in ids:
            self._q(f"UPDATE action_log:{aid} SET processed = true")

    @observe(tier="stage")
    def prune_processed_action_log(self, older_than_days: int = 7) -> int:
        """Delete processed action_log rows older than ``older_than_days``.

        Unprocessed rows are never pruned — they haven't been turned into
        memories yet.  Returns the number of rows deleted.
        """
        from datetime import UTC, datetime, timedelta

        cutoff = (datetime.now(UTC) - timedelta(days=older_than_days)).isoformat()
        # SurrealDB does not expose a row-count from DELETE, so count first
        rows = self._q(
            "SELECT count() AS c FROM action_log "
            "WHERE processed = true AND timestamp < $cutoff GROUP ALL",
            {"cutoff": cutoff},
        )
        n = int(rows[0]["c"]) if rows and rows[0].get("c") else 0
        self._q(
            "DELETE FROM action_log WHERE processed = true AND timestamp < $cutoff",
            {"cutoff": cutoff},
        )
        return n
