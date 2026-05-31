"""Cleanup mixin — action log processing and table retention prunes."""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("yadgar.consolidation")


def _observe_action_batch(n: int) -> None:
    """Record action batch size metric. Silently no-ops if metrics unavailable."""
    try:
        from yadgar.metrics import yadgar_action_batch_size  # noqa: PLC0415

        yadgar_action_batch_size.observe(n)
    except Exception:
        pass


def _quarantine_action_group(action_ids: list, reason: str, directory: str) -> None:
    """Append a quarantine entry for a poison-pill action-log group.

    Best-effort: any I/O error is swallowed so the quarantine write never
    re-poisons the consolidation cycle.

    File: ~/.yadgar/quarantine/action_log_poison.jsonl
    Format: one JSON object per line — {timestamp, action_ids, reason, directory}
    """
    try:
        quarantine_dir = Path.home() / ".yadgar" / "quarantine"
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "action_ids": action_ids,
            "reason": reason,
            "directory": directory,
        }
        quarantine_file = quarantine_dir / "action_log_poison.jsonl"
        with quarantine_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        logger.debug("quarantine write failed (non-fatal)", exc_info=True)


class _CleanupMixin:
    """Action log processing and retention-based table pruning."""

    def _process_action_log(self) -> dict:
        """Process unprocessed action_log entries into summarized memories.

        Groups actions by directory + 30-minute time windows, then creates
        a summary memory for each group. This is the cold path — the hot
        path (PostToolCall hook) just writes to action_log.
        """
        stats = {"processed": 0, "memories_created": 0, "actions_quarantined": 0}

        try:
            rows = self._storage.get_unprocessed_actions(limit=200)
        except Exception:
            return stats

        # PR-E: observe batch size (including zero) into yadgar_action_batch_size
        _observe_action_batch(len(rows))

        if not rows:
            return stats

        # Group by directory + 30-min windows
        groups: dict[str, list] = {}
        for row in rows:
            directory = row.get("directory") or "unknown"
            timestamp = row.get("timestamp", "")
            # Create a window key: directory + 30-min bucket
            try:
                dt = datetime.fromisoformat(timestamp)
                bucket = dt.strftime("%Y-%m-%d-%H") + f"-{dt.minute // 30}"
            except (ValueError, TypeError) as _e:
                bucket = "unknown"
            key = f"{directory}|{bucket}"
            if key not in groups:
                groups[key] = []
            groups[key].append(
                {
                    "id": row.get("id"),
                    "tool": row.get("tool_name", ""),
                    "summary": row.get("tool_input_summary", ""),
                    "directory": directory,
                }
            )

        # Create a summary memory for each group with 3+ actions
        for _key, actions in groups.items():
            directory = actions[0]["directory"]

            # Build action summary
            tool_counts: dict[str, int] = {}
            details = []
            for a in actions:
                tool_counts[a["tool"]] = tool_counts.get(a["tool"], 0) + 1
                if a["summary"] and len(details) < 5:
                    details.append(f"{a['tool']}: {a['summary'][:80]}")

            if len(actions) >= 3:
                tools_str = ", ".join(
                    f"{t}({c})" for t, c in sorted(tool_counts.items(), key=lambda x: -x[1])
                )
                content = f"Session activity [{tools_str}]: {len(actions)} tool calls"
                if details:
                    content += "\n" + "\n".join(f"- {d}" for d in details)

                group_ids = [a["id"] for a in actions]
                stored = self._try_store_action_summary(content, directory, group_ids)
                if stored is None:
                    stats["actions_quarantined"] += len(group_ids)
                else:
                    stats["memories_created"] += stored

            # Mark all as processed
            ids = [a["id"] for a in actions]
            self._storage.mark_actions_processed(ids)
            stats["processed"] += len(actions)

        # Prune old processed rows so action_log doesn't grow without bound.
        try:
            retention = self._settings.ACTION_LOG_RETENTION_DAYS
            self._storage.prune_processed_action_log(older_than_days=retention)
        except Exception:
            logger.debug("action_log prune failed (non-fatal)", exc_info=True)

        return stats

    def _try_store_action_summary(
        self, content: str, directory: str, group_ids: list
    ) -> int | None:
        """Attempt to store one action-log group as a memory.

        Returns 1 on success, None if blocked by SecretLeakBlocked (poison-pill).
        Re-raises any other exception so the caller sees unexpected failures.

        v5.25.2: extracted from _process_action_log to reduce nesting/cyclo.
        """
        from yadgar.secrets import SecretLeakBlocked  # noqa: PLC0415

        embedding = self._embeddings.encode(content)
        try:
            self._storage.insert_memory(
                {
                    "content": content,
                    "embedding": embedding,
                    "tags": ["_action_stream", "_auto"],
                    "directory_context": directory,
                    "heat": 0.4,
                    "confidence": 0.0,
                    "is_stale": False,
                    "file_hash": None,
                    "embedding_model": self._embeddings.get_model_name(),
                }
            )
            return 1
        except SecretLeakBlocked as _slb:
            # Poison-pill: log + quarantine + let caller skip the entry.
            logger.warning(
                "action_log poison-pill: SecretLeakBlocked on group "
                "(directory=%s, action_ids=%s, reason=%s) — "
                "quarantining and skipping. Entry will not be retried.",
                directory,
                group_ids,
                _slb,
            )
            _quarantine_action_group(group_ids, str(_slb), directory)
            return None

    def _prune_old_episodes_safe(self) -> None:
        """Prune old episodes to keep the table bounded. Non-fatal."""
        try:
            retention = self._settings.EPISODE_RETENTION_DAYS
            pruned = self._storage.prune_old_episodes(older_than_days=retention)
            if pruned:
                logger.info("phase: pruned %d old episodes (retention=%dd)", pruned, retention)
        except Exception:
            logger.debug("Episode prune failed (non-fatal)", exc_info=True)

    def _run_retention_tasks(self) -> None:
        """Prune old rows from auxiliary tables. Each task is non-fatal."""
        _retention_tasks = [
            ("narrative_entry", self._settings.NARRATIVE_ENTRY_RETENTION_DAYS, "created_at", None),
            (
                "astrocyte_process",
                self._settings.ASTROCYTE_PROCESS_RETENTION_DAYS,
                "created_at",
                None,
            ),
            ("memory_cluster", self._settings.MEMORY_CLUSTER_RETENTION_DAYS, "created_at", None),
            ("derived_belief", self._settings.DERIVED_BELIEF_RETENTION_DAYS, "created_at", None),
            (
                "prospective_memory",
                self._settings.PROSPECTIVE_MEMORY_RETENTION_DAYS,
                "created_at",
                "is_active = false",  # never delete pending reminders
            ),
        ]
        for _table, _days, _field, _extra in _retention_tasks:
            try:
                pruned = self._storage.prune_old_rows(
                    _table, _days, age_field=_field, extra_where=_extra
                )
                if pruned:
                    logger.info("retention: pruned %d old rows from %s", pruned, _table)
            except Exception:
                logger.debug("retention prune failed for %s (non-fatal)", _table, exc_info=True)
