"""Operational/bookkeeping table CRUD.

_OpsMixin provides:
  - Consolidation log insert
  - Memory stats (aggregate queries)
  - Engram slot init/query/update/assign
  - Checkpoint insert/query/update/epoch helpers
  - prune_old_rows generic pruner
"""

import logging
import re as _re

from yadgar.tracing import trace_span

_log = logging.getLogger(__name__)

# S1a (H-5): allowlist for extra_where clauses in prune_old_rows.
# Permits only simple "column = literal_value" fragments where:
#   - column: alphanumeric + underscore
#   - operator: =, !=, <, <=, >, >= (no keyword operators that could be injected)
#   - literal: boolean keywords (true/false/none/null), quoted strings, or numbers.
# Semicolons, comments (--), and parentheses are rejected outright.
# Callers (consolidation/cleanup.py) only ever pass "is_active = false" style clauses.
_EXTRA_WHERE_PATTERN = _re.compile(
    r"^[a-zA-Z_][a-zA-Z0-9_]*\s*(?:=|!=|<=?|>=?)\s*(?:true|false|none|null|-?\d+(?:\.\d+)?|'[^']*')$",
    _re.IGNORECASE,
)


class _OpsMixin:
    """Operational tables (consolidation_log, stats, engram_slot, checkpoint, prune) —
    mixed into StorageEngine."""

    # ------------------------------------------------------------------ Consolidation Log

    @trace_span("storage.ops.insert_consolidation_log")
    def insert_consolidation_log(self, log: dict) -> int:
        cid = self._next_id("consolidation_log")
        self._q(
            "CREATE type::record('consolidation_log', $id) SET "
            "timestamp = $timestamp, memories_added = $added, "
            "memories_updated = $updated, memories_archived = $archived, "
            "memories_deleted = $deleted, duration_ms = $duration_ms",
            {
                "id": cid,
                "timestamp": log.get("timestamp", self._now_iso()),
                "added": log.get("memories_added", 0),
                "updated": log.get("memories_updated", 0),
                "archived": log.get("memories_archived", 0),
                "deleted": log.get("memories_deleted", 0),
                "duration_ms": log.get("duration_ms", 0),
            },
        )
        return cid

    # ------------------------------------------------------------------ Stats

    @trace_span("storage.ops.get_memory_stats")
    def get_memory_stats(self) -> dict:
        total_rows = self._q("SELECT count() AS c FROM memory GROUP ALL")
        total = int(total_rows[0]["c"]) if total_rows else 0

        active_rows = self._q(
            "SELECT count() AS c FROM memory WHERE is_stale = false AND heat >= 0.05 GROUP ALL"
        )
        active = int(active_rows[0]["c"]) if active_rows else 0

        archived_rows = self._q("SELECT count() AS c FROM memory WHERE heat < 0.05 GROUP ALL")
        archived = int(archived_rows[0]["c"]) if archived_rows else 0

        stale_rows = self._q("SELECT count() AS c FROM memory WHERE is_stale = true GROUP ALL")
        stale = int(stale_rows[0]["c"]) if stale_rows else 0

        heat_rows = self._q("SELECT math::mean(heat) AS avg FROM memory GROUP ALL")
        avg_heat = (
            float(heat_rows[0]["avg"]) if heat_rows and heat_rows[0].get("avg") is not None else 0.0
        )

        log_rows = self._q("SELECT * FROM consolidation_log ORDER BY timestamp DESC LIMIT 1")
        last_consolidation = log_rows[0]["timestamp"] if log_rows else None

        return {
            "total_memories": total,
            "active_count": active,
            "archived_count": archived,
            "stale_count": stale,
            "avg_heat": avg_heat,
            "last_consolidation": last_consolidation,
        }

    # ------------------------------------------------------------------ prune_old_rows

    def prune_old_rows(
        self,
        table: str,
        older_than_days: int,
        age_field: str = "created_at",
        extra_where: str | None = None,
    ) -> int:
        """Delete rows from `table` whose `age_field` is older than `older_than_days`.

        `extra_where` may contain additional AND conditions (e.g. ``is_active = false``).
        Only the listed tables may be pruned — rejects unknown tables to prevent SQL injection.

        Returns the approximate number of rows deleted (counted before the DELETE).
        """
        from datetime import UTC, datetime, timedelta

        _allowed_tables = frozenset(
            {
                "narrative_entry",
                "astrocyte_process",
                "memory_cluster",
                "derived_belief",
                "prospective_memory",
            }
        )
        if table not in _allowed_tables:
            raise ValueError(f"prune_old_rows: table '{table}' is not in the allowed set")
        if older_than_days <= 0:
            return 0

        cutoff = (datetime.now(UTC) - timedelta(days=older_than_days)).isoformat()
        where = f"{age_field} < $cutoff"
        if extra_where:
            # S1a (H-5): validate extra_where against a strict allowlist before interpolation.
            # Permits only "column op literal" forms (see _EXTRA_WHERE_PATTERN above).
            # Semicolons, comments, subqueries, and compound expressions are rejected.
            if not _EXTRA_WHERE_PATTERN.match(extra_where.strip()):
                raise ValueError(
                    f"prune_old_rows: extra_where {extra_where!r} is not an allowed clause. "
                    "Only simple 'column op literal' expressions are permitted."
                )
            where = f"{where} AND {extra_where}"
        count_rows = self._q(
            f"SELECT count() AS c FROM {table} WHERE {where} GROUP ALL",
            {"cutoff": cutoff},
        )
        n = int(count_rows[0]["c"]) if count_rows and count_rows[0].get("c") else 0
        self._q(f"DELETE FROM {table} WHERE {where}", {"cutoff": cutoff})
        return n

    # ------------------------------------------------------------------ Archive Retention

    def purge_expired_archives(
        self,
        dry_run: bool = False,
        retention_days_override: int | None = None,
    ) -> dict:
        """Purge memory_archive rows older than retention threshold.

        retention_days_override: if provided, use this value instead of
            MEMORY_ARCHIVE_RETENTION_DAYS for this call only.

        Returns dict:
          {
            "candidates": int,            # total matched (before circuit-breaker cap)
            "purged": int,                # actual rows deleted (0 if dry_run)
            "skipped_protected": int,
            "skipped_anchor": int,
            "skipped_recent": int,
            "circuit_breaker_hit": bool,
            "candidate_ids": list[int],   # first 10 candidate IDs (for MCP sample)
          }
        """
        from datetime import UTC, datetime, timedelta

        from yadgar.config import get_settings

        cfg = get_settings()
        retention_days: int = (
            retention_days_override
            if retention_days_override is not None
            else cfg.MEMORY_ARCHIVE_RETENTION_DAYS
        )
        circuit_breaker: int = cfg.MEMORY_ARCHIVE_RETENTION_CIRCUIT_BREAKER
        thrash_guard_days: int = cfg.MEMORY_ARCHIVE_RETENTION_THRASH_GUARD_DAYS

        _zero: dict = {
            "candidates": 0,
            "purged": 0,
            "skipped_protected": 0,
            "skipped_anchor": 0,
            "skipped_recent": 0,
            "circuit_breaker_hit": False,
        }

        if retention_days == 0:
            return _zero

        now = datetime.now(UTC)
        archived_cutoff = (now - timedelta(days=retention_days)).isoformat()
        thrash_cutoff = (now - timedelta(days=thrash_guard_days)).isoformat()

        # Eligible rows: old enough, not protected, not anchor-tagged,
        # not recently created, and not in active migration grace.
        # Uses INSIDE/NOTINSIDE — the operator convention for SurrealDB v2 embedded mode.
        eligible_rows = self._q(
            "SELECT meta::id(id) AS rid FROM memory_archive "
            "WHERE archived_at < $archived_cutoff "
            "AND is_protected != true "
            "AND '_anchor' NOTINSIDE tags "
            "AND 'anchor' NOTINSIDE tags "
            "AND created_at < $thrash_cutoff "
            "AND (migration_grace != true OR valid_until < $now_iso) "
            "LIMIT $limit",
            {
                "archived_cutoff": archived_cutoff,
                "thrash_cutoff": thrash_cutoff,
                "now_iso": now.isoformat(),
                "limit": circuit_breaker + 1,
            },
        )

        candidates = len(eligible_rows)
        circuit_breaker_hit = candidates > circuit_breaker
        if circuit_breaker_hit:
            _log.critical(
                "purge_expired_archives: circuit-breaker hit — %d candidates exceed limit %d; "
                "capping purge to %d rows",
                candidates,
                circuit_breaker,
                circuit_breaker,
            )
            eligible_rows = eligible_rows[:circuit_breaker]
            candidates = circuit_breaker

        # Count skip categories over the same archived_at window (before exclusions).
        skipped_protected = self._count_archive_skip_protected(archived_cutoff)
        skipped_anchor = self._count_archive_skip_anchor(archived_cutoff)
        skipped_recent = self._count_archive_skip_recent(archived_cutoff, thrash_cutoff)

        all_ids = [int(r["rid"]) for r in eligible_rows]
        candidate_ids = all_ids[:10]

        purged = 0
        if not dry_run and all_ids:
            for aid in all_ids:
                self._q(
                    "DELETE type::record('memory_archive', $id)",
                    {"id": aid},
                )
            purged = len(all_ids)

        return {
            "candidates": candidates,
            "purged": purged,
            "skipped_protected": skipped_protected,
            "skipped_anchor": skipped_anchor,
            "skipped_recent": skipped_recent,
            "circuit_breaker_hit": circuit_breaker_hit,
            "candidate_ids": candidate_ids,
        }

    def _count_archive_skip_protected(self, archived_cutoff: str) -> int:
        """Count protected archives older than cutoff."""
        rows = self._q(
            "SELECT count() AS c FROM memory_archive "
            "WHERE archived_at < $cutoff AND is_protected = true GROUP ALL",
            {"cutoff": archived_cutoff},
        )
        return int(rows[0]["c"]) if rows and rows[0].get("c") else 0

    def _count_archive_skip_anchor(self, archived_cutoff: str) -> int:
        """Count anchor-tagged archives older than cutoff."""
        rows = self._q(
            "SELECT count() AS c FROM memory_archive "
            "WHERE archived_at < $cutoff "
            "AND ('_anchor' INSIDE tags OR 'anchor' INSIDE tags) GROUP ALL",
            {"cutoff": archived_cutoff},
        )
        return int(rows[0]["c"]) if rows and rows[0].get("c") else 0

    def _count_archive_skip_recent(self, archived_cutoff: str, thrash_cutoff: str) -> int:
        """Count archives older than archived_cutoff but recently created (thrash-guard)."""
        rows = self._q(
            "SELECT count() AS c FROM memory_archive "
            "WHERE archived_at < $cutoff AND created_at >= $thrash GROUP ALL",
            {"cutoff": archived_cutoff, "thrash": thrash_cutoff},
        )
        return int(rows[0]["c"]) if rows and rows[0].get("c") else 0

    # ------------------------------------------------------------------ Engram Slots

    def init_engram_slots(self, num_slots: int):
        """Ensure all slot indices exist in the engram_slot table."""
        now = self._now_iso()
        rows = self._q("SELECT VALUE slot_index FROM engram_slot")
        # Cast to int — SurrealDB may return floats (e.g. 0.0) instead of ints
        existing = {int(r) for r in rows if r is not None}
        missing = [i for i in range(num_slots) if i not in existing]
        if not missing:
            return
        records = [{"slot_index": i, "excitability": 0.0, "last_activated": now} for i in missing]
        _CHUNK = 500
        for start in range(0, len(records), _CHUNK):
            chunk = records[start : start + _CHUNK]
            # S1b (H-4): use bind parameter ($data) instead of raw json.dumps interpolation.
            # The LET-preamble mechanism in _q serialises the value safely with ensure_ascii=False,
            # preventing any dollar-token or quote-escape in surrounding context from corrupting
            # the INSERT statement.
            self._q("INSERT INTO engram_slot $data", {"data": chunk})

    def get_engram_slot(self, slot_index: int) -> dict | None:
        rows = self._q(
            "SELECT * FROM engram_slot WHERE slot_index = $si LIMIT 1",
            {"si": slot_index},
        )
        # Engram slots use SurrealDB auto-generated string IDs; skip _row_to_dict
        # which would try to coerce the ID to int and fail.
        return dict(rows[0]) if rows else None

    def get_all_engram_slots(self) -> list[dict]:
        rows = self._q("SELECT * FROM engram_slot ORDER BY slot_index")
        # Engram slots use SurrealDB auto-generated string IDs; skip _row_to_dict
        # which would try to coerce the ID to int and fail.
        return [dict(r) for r in rows]

    def update_engram_slot(self, slot_index: int, excitability: float, last_activated: str):
        self._q(
            "UPDATE engram_slot SET excitability = $exc, last_activated = $la "
            "WHERE slot_index = $si",
            {"si": slot_index, "exc": excitability, "la": last_activated},
        )

    def assign_memory_slot(self, memory_id: int, slot_index: int):
        now = self._now_iso()
        self._q(
            "UPDATE type::record('memory', $id) SET "
            "slot_index = $si, excitability = 1.0, last_excitability_update = $now",
            {"id": memory_id, "si": slot_index, "now": now},
        )

    def get_memories_in_slot(self, slot_index: int) -> list[dict]:
        rows = self._q(
            "SELECT * FROM memory WHERE slot_index = $si AND heat > 0 ORDER BY created_at",
            {"si": slot_index},
        )
        return self._rows_to_dicts(rows)

    def get_slot_occupancy(self) -> dict:
        """Return {slot_index: count} for all occupied slots."""
        rows = self._q(
            "SELECT slot_index, count() AS cnt FROM memory "
            "WHERE slot_index IS NOT NONE GROUP BY slot_index"
        )
        result = {}
        for row in rows:
            si = row.get("slot_index")
            cnt = int(row.get("cnt", 0))
            if si is not None:
                result[si] = cnt
        return result

    def get_memory_ids_in_slot(self, slot_index: int, limit: int = 100) -> list[int]:
        """Return memory IDs assigned to a given engram slot, up to limit."""
        rows = self._q(
            "SELECT VALUE meta::id(id) FROM memory WHERE slot_index = $slot LIMIT $lim",
            {"slot": slot_index, "lim": limit},
        )
        return [int(r) for r in (rows or [])]

    # ------------------------------------------------------------------ Checkpoints

    def insert_checkpoint(self, data: dict) -> int:
        """Replace any existing checkpoint for this directory.

        Old per-directory checkpoints are HARD-DELETED. Other directories untouched.
        is_active=true is kept on every row for backward compat with callers that
        still filter on it; get_active_checkpoint() now uses directory_context.
        """
        now = self._now_iso()
        cid = self._next_id("checkpoint")
        directory = data.get("directory_context", "")
        resume_hint = data.get("resume_hint", "") or f'restore(directory="{directory}")'
        # Hard-delete existing rows for this directory, then create new one.
        self._q(
            "BEGIN TRANSACTION;\n"
            "DELETE FROM checkpoint WHERE directory_context = $dir;\n"
            "CREATE type::record('checkpoint', $id) SET "
            "session_id = $session_id, directory_context = $dir, "
            "current_task = $task, files_being_edited = $files, "
            "key_decisions = $decisions, open_questions = $questions, "
            "next_steps = $steps, active_errors = $errors, "
            "custom_context = $custom, epoch = $epoch, "
            "resume_hint = $hint, "
            "created_at = $now, is_active = true;\n"
            "COMMIT TRANSACTION",
            {
                "id": cid,
                "session_id": data.get("session_id", "default"),
                "dir": directory,
                "task": data.get("current_task", ""),
                "files": data.get("files_being_edited", []),
                "decisions": data.get("key_decisions", []),
                "questions": data.get("open_questions", []),
                "steps": data.get("next_steps", []),
                "errors": data.get("active_errors", []),
                "custom": data.get("custom_context", ""),
                "epoch": data.get("epoch", 0),
                "hint": resume_hint,
                "now": now,
            },
        )
        return cid

    def get_active_checkpoint(self, directory: str = "") -> dict | None:
        """Latest checkpoint for this directory. Empty directory = global most-recent."""
        if directory:
            rows = self._q(
                "SELECT * FROM checkpoint WHERE directory_context = $dir "
                "ORDER BY created_at DESC LIMIT 1",
                {"dir": directory},
            )
        else:
            rows = self._q("SELECT * FROM checkpoint ORDER BY created_at DESC LIMIT 1")
        if not rows:
            return None
        return self._row_to_dict(rows[0])

    def get_current_epoch(self) -> int:
        """Get the current compaction epoch number."""
        rows = self._q("SELECT math::max(epoch) AS max_epoch FROM checkpoint GROUP ALL")
        if rows and rows[0].get("max_epoch") is not None:
            return int(rows[0]["max_epoch"])
        return 0

    def increment_epoch(self) -> int:
        """Increment and return the new epoch number."""
        current = self.get_current_epoch()
        return current + 1

    def update_checkpoint_epoch(self, checkpoint_id: int, epoch: int):
        """Update the epoch field on an existing checkpoint."""
        self._q(
            "UPDATE type::record('checkpoint', $id) SET epoch = $epoch",
            {"id": checkpoint_id, "epoch": epoch},
        )


# ── Module-level helpers ────────────────────────────────────────────────────


def purge_expired_archives(
    storage,
    dry_run: bool = False,
    retention_days_override: int | None = None,
) -> dict:
    """Purge memory_archive rows older than retention threshold.

    Thin wrapper around storage.purge_expired_archives() for callers that
    prefer the standalone-function calling convention.

    Returns dict:
      {
        "candidates": int,            # total matched (before circuit-breaker cap)
        "purged": int,                # actual rows deleted (0 if dry_run)
        "skipped_protected": int,
        "skipped_anchor": int,
        "skipped_recent": int,
        "circuit_breaker_hit": bool,
        "candidate_ids": list[int],   # first 10 candidate IDs
      }
    """
    return storage.purge_expired_archives(
        dry_run=dry_run,
        retention_days_override=retention_days_override,
    )


def vacuum_checkpoints(storage, *, dry_run: bool = True) -> dict:
    """Collapse stale checkpoints: keep latest per directory_context, delete rest.

    Idempotent. Call with dry_run=False after v5.6.5 deploy to clean up rows
    accumulated under the old global-deactivate scheme.

    Returns:
        {
            "stale_count": int,   # rows that would be / were deleted
            "deleted": int,       # 0 if dry_run=True
            "survivors": int,     # rows remaining after vacuum
            "dry_run": bool,
        }
    """
    # Fetch all checkpoint rows (id + directory_context + created_at).
    all_rows = storage._q("SELECT id, directory_context, created_at FROM checkpoint")
    if not all_rows:
        return {"stale_count": 0, "deleted": 0, "survivors": 0, "dry_run": dry_run}

    # Group by directory_context; find winner (latest created_at) per group.
    from collections import defaultdict

    groups: dict[str, list[dict]] = defaultdict(list)
    for row in all_rows:
        groups[row.get("directory_context", "")].append(row)

    stale_int_ids: list[int] = []
    for _dir, rows in groups.items():
        # Sort descending by created_at string (ISO-8601 sorts lexicographically).
        rows.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        # Winner stays; everything else is stale.
        for stale in rows[1:]:
            raw_id = stale["id"]
            # Normalise to int (raw row ids may be RecordID or "checkpoint:N" strings)
            stale_int_ids.append(storage._extract_id(raw_id))

    stale_count = len(stale_int_ids)
    deleted = 0

    if not dry_run:
        for iid in stale_int_ids:
            storage._q(
                "DELETE type::record('checkpoint', $id)",
                {"id": iid},
            )
            deleted += 1

    survivors_count = len(all_rows) - deleted
    return {
        "stale_count": stale_count,
        "deleted": deleted,
        "survivors": survivors_count,
        "dry_run": dry_run,
    }
