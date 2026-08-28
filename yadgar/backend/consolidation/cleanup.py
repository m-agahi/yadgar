"""Cleanup mixin — action log processing and table retention prunes."""

import json
import logging
from datetime import UTC, datetime
from typing import Any

import yadgar._shared.paths as _paths
from yadgar._shared.observability.observe import observe
from yadgar._shared.storage._project_id_writer import (
    _NON_IDENTIFYING_PROJECT_IDS,
    observe_project_id_skip,
)
from yadgar.backend.consolidation.cold_retention import _cold_memory_retention_report

logger = logging.getLogger("yadgar.consolidation")


def _observe_action_batch(n: int) -> None:
    """Record action batch size metric. Silently no-ops if metrics unavailable."""
    try:
        from yadgar._shared.observability.metrics import yadgar_action_batch_size  # noqa: PLC0415

        yadgar_action_batch_size.observe(n)
    except ImportError:
        pass


def _observe_archive_purge(result: dict) -> None:
    """Emit Prometheus counters for a completed archive purge. Non-fatal."""
    try:
        from yadgar._shared.observability.metrics import (  # noqa: PLC0415
            yadgar_archive_purged_total,
            yadgar_archive_retention_skipped_total,
        )

        yadgar_archive_purged_total.inc(result["purged"])
        yadgar_archive_retention_skipped_total.labels(reason="protected").inc(
            result["skipped_protected"]
        )
        yadgar_archive_retention_skipped_total.labels(reason="anchor").inc(result["skipped_anchor"])
        yadgar_archive_retention_skipped_total.labels(reason="recent").inc(result["skipped_recent"])
    except (ImportError, KeyError):  # fmt: skip
        pass


def _quarantine_action_group(action_ids: list, reason: str, directory: str) -> None:
    """Append a quarantine entry for a poison-pill action-log group.

    Best-effort: any I/O error is swallowed so the quarantine write never
    re-poisons the consolidation cycle.

    File: ~/.local/state/yadgar/quarantine/action_log_poison.jsonl
    Format: one JSON object per line — {timestamp, action_ids, reason, directory}
    """
    try:
        quarantine_dir = _paths.QUARANTINE_DIR
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
    except (OSError, TypeError, ValueError):  # fmt: skip
        logger.debug("quarantine write failed (non-fatal)", exc_info=True)


def _bucket_for_timestamp(timestamp: str) -> str:
    """Return a 30-minute bucket string for *timestamp*, or 'unknown' on parse error."""
    try:
        dt = datetime.fromisoformat(timestamp)
        return dt.strftime("%Y-%m-%d-%H") + f"-{dt.minute // 30}"
    except (ValueError, TypeError):  # fmt: skip
        return "unknown"


@observe(tier="stage", metric="consolidation.group_rows_by_window")
def _group_rows_by_window(rows: list) -> tuple[dict[str, list], list]:
    """Group action-log rows by (project_id, 30-min window) key.

    C4 (0047 PR#40 §5). Two defects died here together:

      * the key was ``row["directory"]``, so ONE project checked out twice
        (a worktree, a second clone) split into two unrelated summaries;
      * a row with no directory fell into a literal ``"unknown"`` bucket —
        a phantom project whose summaries are attributed to nothing.

    The key is now the row's ``project_id``, stamped at enqueue time by the
    session that produced the action (``core/cli/capture.py`` and the
    ``/hooks/auto-capture`` endpoint). Nothing is derived here: ADR-0227
    forbids the container from minting one, and a bucket named by a guess
    is the phantom namespace the registry check exists to prevent.

    A row whose ``project_id`` is absent or a sentinel is NOT bucketed. It
    is returned in the second element so the caller can count it and — this
    part is load-bearing — still mark it processed. ``get_unprocessed_actions``
    selects ``WHERE processed = false ORDER BY timestamp ASC LIMIT 200``, so
    an un-marked skip would sit at the head of that window forever and no
    new action would ever be summarised again.

    Returns:
        ``(groups, skipped_ids)``. Each group value is a list of compact
        dicts: ``{id, tool, summary, directory, project_id}``.
    """
    groups: dict[str, list] = {}
    skipped_ids: list = []
    for row in rows:
        project_id = row.get("project_id")
        if not isinstance(project_id, str) or project_id in _NON_IDENTIFYING_PROJECT_IDS:
            skipped_ids.append(row.get("id"))
            continue
        bucket = _bucket_for_timestamp(row.get("timestamp", ""))
        key = f"{project_id}|{bucket}"
        groups.setdefault(key, []).append(
            {
                "id": row.get("id"),
                "tool": row.get("tool_name", ""),
                "summary": row.get("tool_input_summary", ""),
                # The row's own directory is still the memory's
                # directory_context; it is no longer the grouping key.
                "directory": row.get("directory") or "",
                "project_id": project_id,
            }
        )
    return groups, skipped_ids


def _build_group_content(actions: list) -> str | None:
    """Build a summary string for *actions* if the group has >= 3 entries.

    Returns None when the group is too small to warrant a memory.
    """
    if len(actions) < 3:
        return None
    tool_counts: dict[str, int] = {}
    details: list[str] = []
    for a in actions:
        tool_counts[a["tool"]] = tool_counts.get(a["tool"], 0) + 1
        if a["summary"] and len(details) < 5:
            details.append(f"{a['tool']}: {a['summary'][:80]}")
    tools_str = ", ".join(f"{t}({c})" for t, c in sorted(tool_counts.items(), key=lambda x: -x[1]))
    content = f"Session activity [{tools_str}]: {len(actions)} tool calls"
    if details:
        content += "\n" + "\n".join(f"- {d}" for d in details)
    return content


class _CleanupMixin:
    """Action log processing and retention-based table pruning.

    The three attributes below are supplied by the concrete
    ``ConsolidationEngine`` that mixes this in. They are declared (annotation
    only — no assignment, so nothing is bound at class level) because mypy
    otherwise reports ``has no attribute`` for every use, which made the
    strict-typing ratchet unable to tell a NEW error from the 20 standing ones.
    """

    _storage: Any
    _settings: Any
    _embeddings: Any

    @observe(tier="stage", metric="consolidation.action_log")
    def _process_action_log(self) -> dict:
        """Process unprocessed action_log entries into summarized memories.

        Groups actions by project_id + 30-minute time windows, then creates
        a summary memory for each group. This is the cold path — the hot
        path (PostToolCall hook) just writes to action_log.

        C6: the plan's end state has the nightly cycle iterate the ``project``
        REGISTRY and run once per registered ``project_id``, so a project with
        no rows in this batch is still visited and an unregistered project_id
        on a row is a registry violation rather than merely an unknown key.
        The registry table does not exist yet. What lands here now is the half
        that does not depend on it: the per-project grouping and the
        skip-and-count path for rows the registry would have rejected. When C6
        lands, the loop below is driven by the registry and the skip branch
        gains a second reason ("project_id not registered").
        """
        stats = {
            "processed": 0,
            "memories_created": 0,
            "actions_quarantined": 0,
            "actions_skipped_no_project": 0,
        }

        try:
            rows = self._storage.get_unprocessed_actions(limit=200)
        except Exception:  # BLE001-KEEP: the action-log read that feeds the consolidation cycle: storage raises RuntimeError over HTTP and arbitrary SDK types embedded with no common base, and an unreadable batch returns zeroed stats so the cycle continues
            return stats

        # PR-E: observe batch size (including zero) into yadgar_action_batch_size
        _observe_action_batch(len(rows))

        if not rows:
            return stats

        groups, skipped_ids = _group_rows_by_window(rows)

        # C4: rows that name no project are skipped and counted — never
        # bucketed under a guess — but they are STILL marked processed, or
        # they would occupy the head of the unprocessed window forever and
        # silently stop the summariser (they are the oldest rows in it).
        if skipped_ids:
            observe_project_id_skip("action_log_group", len(skipped_ids))
            logger.warning(
                "action_log: skipped %d row(s) with no usable project_id — the "
                "producing session did not stamp one. Not bucketed (ADR-0227: no "
                "phantom namespace), marked processed so the cycle keeps moving.",
                len(skipped_ids),
            )
            self._storage.mark_actions_processed(skipped_ids)
            stats["actions_skipped_no_project"] += len(skipped_ids)
            stats["processed"] += len(skipped_ids)

        for _key, actions in groups.items():
            directory = actions[0]["directory"]
            project_id = actions[0]["project_id"]
            content = _build_group_content(actions)
            if content is not None:
                group_ids = [a["id"] for a in actions]
                stored = self._try_store_action_summary(content, directory, project_id, group_ids)
                if stored is None:
                    stats["actions_quarantined"] += len(group_ids)
                else:
                    stats["memories_created"] += stored

            ids = [a["id"] for a in actions]
            self._storage.mark_actions_processed(ids)
            stats["processed"] += len(actions)

        self._prune_action_log_safe()

        return stats

    @observe(tier="stage", metric="consolidation.prune_action_log_safe")
    def _prune_action_log_safe(self) -> None:
        """Prune old processed rows so action_log doesn't grow without bound. Non-fatal."""
        try:
            retention = self._settings.ACTION_LOG_RETENTION_DAYS
            self._storage.prune_processed_action_log(older_than_days=retention)
        except Exception:  # BLE001-KEEP: per-task isolation in the nightly retention sweep: the prune runs against storage, which raises with no common base, and an unprunable table must not stop the cycle
            logger.debug("action_log prune failed (non-fatal)", exc_info=True)

    @observe(tier="hot", metric="consolidation.try_store_action_summary")
    def _try_store_action_summary(
        self, content: str, directory: str, project_id: str, group_ids: list
    ) -> int | None:
        """Attempt to store one action-log group as a memory.

        Returns 1 on success, None if blocked by SecretLeakBlocked (poison-pill).
        Re-raises any other exception so the caller sees unexpected failures.

        v5.25.2: extracted from _process_action_log to reduce nesting/cyclo.

        C4 (0047 PR#40 §5): ``project_id`` is now a REQUIRED argument, taken
        from the group's rows. Car L used to call the classifier here and fall
        back to ``'global'``; this process is the backend container, which has
        no git binary and no host project mounts, so that call could only ever
        manufacture ``local/<basename>`` or ``'unresolved'`` (ADR-0227 §1.1).
        A group with no nameable project never reaches this method — the
        caller skips and counts it.
        """
        from yadgar._shared.security.secrets import SecretLeakBlocked  # noqa: PLC0415

        embedding = self._embeddings.encode(content)
        try:
            self._storage.insert_memory(
                {
                    "content": content,
                    "embedding": embedding,
                    "tags": ["_action_stream", "_auto"],
                    "directory_context": directory,
                    "project_id": project_id,
                    "heat": 0.4,
                    "confidence": 0.0,
                    "is_stale": False,
                    "file_hash": None,
                    "embedding_model": self._embeddings.get_model_name(),
                    # C12 (ADR-0226): the ``"branch": None`` key and its "canonical
                    # NULL-branch slot" note are gone. The key was already INERT —
                    # ``_build_memory_insert_clause`` reads ``branch`` from the kwarg,
                    # never from this dict — and both the kwarg and the concept are
                    # retired. The v5.42.3 ``_internal`` carve-out it annotated still
                    # holds: consolidation writes straight to storage, bypassing the
                    # file_queue drainer.
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

    @observe(tier="stage", metric="consolidation.prune_episodes")
    def _prune_old_episodes_safe(self) -> None:
        """Prune old episodes to keep the table bounded. Non-fatal."""
        try:
            retention = self._settings.EPISODE_RETENTION_DAYS
            pruned = self._storage.prune_old_episodes(older_than_days=retention)
            if pruned:
                logger.info("phase: pruned %d old episodes (retention=%dd)", pruned, retention)
        except Exception:  # BLE001-KEEP: per-task isolation; same untypeable storage surface
            logger.debug("Episode prune failed (non-fatal)", exc_info=True)

    @observe(tier="stage", metric="consolidation.run_retention_tasks")
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
            except Exception:  # BLE001-KEEP: per-table isolation inside the retention loop: same untypeable storage surface, and the remaining tables must still be pruned
                logger.debug("retention prune failed for %s (non-fatal)", _table, exc_info=True)

        # cold-memory retention DRY-RUN visibility (#29)
        # Reports immortal cold user-memory candidates. Deletes nothing by default.
        # Real delete requires COLD_MEMORY_PURGE_ENABLED=True AND COLD_MEMORY_PURGE_DRY_RUN=False.
        if self._settings.COLD_MEMORY_RETENTION_DAYS > 0:
            try:
                result = _cold_memory_retention_report(self._storage, self._settings)
                if result["candidates"]:
                    logger.info(
                        "retention: cold_memory report — %d candidates, %d deleted",
                        result["candidates"],
                        result["deleted"],
                    )
            except Exception:  # BLE001-KEEP: per-task isolation for the cold-memory dry-run report; same untypeable storage surface
                logger.debug("retention: cold_memory report failed (non-fatal)", exc_info=True)

        # v5.49.0: archive retention — only when enabled
        if self._settings.MEMORY_ARCHIVE_RETENTION_DAYS > 0:
            try:
                result = self._storage.purge_expired_archives(dry_run=False)
                if result["circuit_breaker_hit"]:
                    logger.warning(
                        "retention: archive purge circuit-breaker hit during nightly cycle "
                        "(purged=%d); see storage-layer CRITICAL for details",
                        result["purged"],
                    )
                if result["purged"]:
                    logger.info(
                        "retention: purged %d expired archives (protected=%d anchor=%d recent=%d)",
                        result["purged"],
                        result["skipped_protected"],
                        result["skipped_anchor"],
                        result["skipped_recent"],
                    )
                _observe_archive_purge(result)
            except Exception:  # BLE001-KEEP: per-task isolation for the archive purge; same untypeable storage surface, and the purge's own circuit breaker already reports its stop condition through the result dict
                logger.debug("retention: archive purge failed (non-fatal)", exc_info=True)
