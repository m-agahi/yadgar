"""Cold-memory retention DRY-RUN report — visibility gate for immortal cold user memories.

Problem: 46% of the store (1536/3332 memories) are cold (heat<COLD_THRESHOLD) but
IMMORTAL — heat decay only sets heat→0; _memify_prune only targets tagged system
memories (_action_stream / auto-generated / auto-abstracted / dream).  Cold USER-created
untagged memories have no retention gate whatsoever.  This is the #44 data-loss risk:
we need visibility BEFORE we wire real deletes.

This module ships VISIBILITY FIRST:
  - COLD_MEMORY_PURGE_ENABLED=False (default) → report only, DELETE NOTHING
  - COLD_MEMORY_PURGE_DRY_RUN=True  (default) → report only, DELETE NOTHING
  - Real delete only fires when ENABLED=True AND DRY_RUN=False — both gates OFF

Candidate gate (conservative — "safe to consider" set):
  heat < COLD_THRESHOLD                (truly cold)
  AND age (now - created_at) > COLD_MEMORY_RETENTION_DAYS
  AND access_count == 0                (never recalled)
  AND NOT is_protected
  AND NOT _anchor in tags              (respect anchors)
"""

import json
import logging
from datetime import UTC, datetime, timedelta

from yadgar._shared.config import Settings
from yadgar._shared.observability.observe import observe
from yadgar._shared.storage import StorageEngine

logger = logging.getLogger("yadgar.consolidation")


def _parse_tags(mem: dict) -> list:
    """Return tags as list; handles JSON-string storage format."""
    tags = mem.get("tags", [])
    if isinstance(tags, str):
        tags = json.loads(tags)
    return tags


def _is_candidate(mem: dict, age_cutoff: str, cold_threshold: float) -> bool:
    """Return True if mem satisfies all retention candidate conditions."""
    if (mem.get("heat") or 0.0) >= cold_threshold:
        return False
    created_at = mem.get("created_at") or ""
    if created_at > age_cutoff:  # too recent — spare
        return False
    if (mem.get("access_count") or 0) != 0:
        return False
    if mem.get("is_protected"):
        return False
    tags = _parse_tags(mem)
    if "_anchor" in tags:
        return False
    return True


@observe(tier="stage", metric="consolidation.cold_memory_retention_report")
def _cold_memory_retention_report(
    storage: StorageEngine,
    settings: Settings,
) -> dict:
    """Identify cold immortal user memories and report candidates.

    Default behaviour (PURGE_ENABLED=False OR DRY_RUN=True):
      - Log structured report with candidate count + content previews
      - Emit yadgar_cold_purge_candidates gauge
      - DELETE NOTHING (deleted=0 always)

    Gated delete (PURGE_ENABLED=True AND DRY_RUN=False):
      - All of the above PLUS calls storage.delete_memory for each candidate
      - Guard is explicit: BOTH flags must be set; any other combo = report only

    Returns:
        {"candidates": N, "deleted": M}
        M is always 0 unless both gates are explicitly armed.
    """
    stats = {"candidates": 0, "deleted": 0}

    retention_days = settings.COLD_MEMORY_RETENTION_DAYS
    cold_threshold = settings.COLD_THRESHOLD
    purge_enabled = settings.COLD_MEMORY_PURGE_ENABLED
    dry_run = settings.COLD_MEMORY_PURGE_DRY_RUN

    age_cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()

    candidates = storage.get_memories_by_heat(min_heat=0.0, limit=10000)

    cold_candidates = [mem for mem in candidates if _is_candidate(mem, age_cutoff, cold_threshold)]
    stats["candidates"] = len(cold_candidates)

    # Emit metric gauge (always — both dry-run and gated-delete modes)
    _emit_cold_purge_candidates_metric(stats["candidates"])

    if not cold_candidates:
        return stats

    # Structured report — always logged regardless of purge mode
    previews = [
        {"id": m["id"], "preview": (m.get("content") or "")[:80]} for m in cold_candidates[:5]
    ]
    mode = "DRY_RUN" if (not purge_enabled or dry_run) else "PURGE"
    logger.info(
        "cold_retention [%s]: %d candidate memories (heat<%.3f, age>%dd, access_count=0, "
        "not protected/anchored) — samples: %s",
        mode,
        stats["candidates"],
        cold_threshold,
        retention_days,
        previews,
    )

    # Real delete — BOTH gates must be explicitly armed
    if purge_enabled and not dry_run:
        for mem in cold_candidates:
            storage.delete_memory(mem["id"])
            stats["deleted"] += 1
        logger.info(
            "cold_retention [PURGE]: deleted %d cold user memories",
            stats["deleted"],
        )

    return stats


def _emit_cold_purge_candidates_metric(count: int) -> None:
    """Set yadgar_cold_purge_candidates gauge. Non-fatal."""
    try:
        from yadgar._shared.observability.metrics import (
            yadgar_cold_purge_candidates,  # noqa: PLC0415
        )

        yadgar_cold_purge_candidates.set(count)
    except ImportError:
        pass
