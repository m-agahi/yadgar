"""DLQ mixin for QueueDrainer — move-to-DLQ, validation, defaults."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def _json_default(obj):
    """JSON serializer for objects not serializable by default json."""
    if hasattr(obj, "tolist"):
        return obj.tolist()
    return str(obj)


class _DLQMixin:
    """Dead-letter queue operations for QueueDrainer."""

    def _move_to_dlq(self, path: Path, attempt, op_type: str) -> None:
        """Atomically move a queue file to DLQ, write a .error.json sidecar, append events log."""
        now_ts = datetime.now(UTC).isoformat()
        first_failed = (
            datetime.fromtimestamp(attempt.first_failed_at, UTC).isoformat()
            if attempt.first_failed_at
            else now_ts
        )
        meta = {
            "op_type": op_type,
            "first_failed_at": first_failed,
            "last_failed_at": now_ts,
            "attempts": attempt.count,
            "classification": attempt.classification,
            "last_error": attempt.last_error,
            "moved_to_dlq_at": now_ts,
        }

        dlq_path = self._queue.dlq_dir / path.name
        try:
            path.rename(dlq_path)
        except OSError as exc:
            logger.error("Failed to move %s to DLQ: %s", path.name, exc)
            return

        # Write error sidecar atomically
        sidecar = self._queue.dlq_dir / (path.name + ".error.json")
        tmp = self._queue.dlq_dir / (path.name + ".error.json.tmp")
        try:
            tmp.write_text(
                json.dumps(meta, ensure_ascii=False, default=_json_default), encoding="utf-8"
            )
            tmp.rename(sidecar)
        except OSError as exc:
            logger.warning("Failed to write DLQ sidecar for %s: %s", path.name, exc)

        # Append to audit events log (never pruned by cleanup_dlq)
        events_log = self._queue.dlq_dir / ".events.log"
        event = {"event": "dlq_move", "ts": now_ts, "file": path.name, **meta}
        try:
            with open(events_log, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False, default=_json_default) + "\n")
        except OSError as exc:
            logger.warning("Failed to append DLQ event log: %s", exc)

        logger.error(
            "MOVED TO DLQ: %s (%d attempts, %s) — %s",
            path.name,
            attempt.count,
            attempt.classification,
            attempt.last_error[:200],
        )

    # ── §26 Option Z ─────────────────────────────────────────────────────────

    _MIN_WIKI_SCHEMA_VERSION: int = 2
    _WIKI_REQUIRED_FIELDS: tuple[str, ...] = ("slug", "title", "content", "category")

    def _validate_wiki_add(self, record: dict) -> str | None:
        """Validate a wiki_add queue record (§26 Option Z).

        Returns a rejection reason string if the record should go to DLQ,
        or None if it passes all checks.
        """
        p = record.get("payload", {})

        # 1. Schema-version gate
        schema_ver = p.get("wiki_schema_version")
        if schema_ver is None or int(schema_ver) < self._MIN_WIKI_SCHEMA_VERSION:
            return (
                f"schema_version_too_old: got {schema_ver!r}, "
                f"require >= {self._MIN_WIKI_SCHEMA_VERSION}"
            )

        # 2. Required fields
        for field in self._WIKI_REQUIRED_FIELDS:
            if not p.get(field):
                return f"missing_required_field: {field}"

        # 3. Degenerate content filter (v4.9 guard)
        try:
            from yadgar.cls_store import _is_degenerate_auto_abstracted

            if _is_degenerate_auto_abstracted(p.get("content", "")):
                return "degenerate_content"
        except Exception as _e:
            logger.debug("_validate_wiki_add: degenerate check failed: %s", _e)

        return None

    def _fill_wiki_add_defaults(self, payload: dict) -> dict:
        """Fill fields that the export-yadgar skill cannot know (§26 Option Z).

        - branch: set to 'master' if absent (Stage 10 will source from git).
        - confidence: set to 'medium' if absent.
        """
        if "branch" not in payload or payload.get("branch") is None:
            payload["branch"] = "master"
        if not payload.get("confidence"):
            payload["confidence"] = "medium"
        return payload

    # ── end §26 ──────────────────────────────────────────────────────────────
