"""Prune passes for the memify self-improvement cycle."""

import json
import logging
from datetime import UTC, datetime, timedelta

from yadgar.cls_store import _is_degenerate_auto_abstracted
from yadgar.config import Settings
from yadgar.storage import StorageEngine

logger = logging.getLogger(__name__)


def _parse_tags(mem: dict) -> list:
    tags = mem.get("tags", [])
    if isinstance(tags, str):
        tags = json.loads(tags)
    return tags


def _prune_action_stream_cold(
    candidates: list,
    stats: dict,
    storage: StorageEngine,
    settings: Settings,
) -> None:
    """Pass 1: delete cold, unaccessed, low-confidence action-stream summaries."""
    for mem in candidates:
        tags = _parse_tags(mem)
        if "_action_stream" not in tags:
            continue
        if mem.get("is_protected"):
            continue
        if (
            (mem.get("heat") or 0.0) < 0.01
            and (mem.get("confidence") or 1.0) < 0.3
            and (mem.get("access_count") or 0) == 0
        ):
            storage.delete_memory(mem["id"])
            stats["pruned"] += 1


def _prune_auto_generated_old(
    candidates: list,
    stats: dict,
    storage: StorageEngine,
    settings: Settings,
) -> None:
    """Pass 2: delete cold, unaccessed, old auto-generated memories."""
    max_age_days = settings.AUTO_GENERATED_MEMORY_MAX_AGE_DAYS
    cold_threshold = settings.COLD_THRESHOLD
    age_cutoff = (datetime.now(UTC) - timedelta(days=max_age_days)).isoformat()

    for mem in candidates:
        tags = _parse_tags(mem)
        if "auto-generated" not in tags:
            continue
        if mem.get("is_protected"):
            continue
        if (mem.get("heat") or 0.0) >= cold_threshold:
            continue
        if (mem.get("access_count") or 0) != 0:
            continue
        created_at = mem.get("created_at") or ""
        if created_at > age_cutoff:
            continue  # too recent — spare it
        storage.delete_memory(mem["id"])
        stats["pruned"] += 1


def _prune_auto_abstracted_old(
    candidates: list,
    stats: dict,
    storage: StorageEngine,
    settings: Settings,
) -> None:
    """Pass 3: delete unaccessed, old CLS-promoted auto-abstracted semantics.

    Action-stream noise and file co-occurrence patterns start at heat=0.8
    and decay ~0.9995/hour — reaching COLD_THRESHOLD takes 300+ days, so
    the 30-day age cap would never fire with a heat gate.  Rely on age +
    access_count only; no heat check.
    """
    auto_abstracted_max_age = settings.AUTO_ABSTRACTED_MEMORY_MAX_AGE_DAYS
    if auto_abstracted_max_age <= 0:
        return
    aa_age_cutoff = (datetime.now(UTC) - timedelta(days=auto_abstracted_max_age)).isoformat()

    for mem in candidates:
        tags = _parse_tags(mem)
        if "auto-abstracted" not in tags:
            continue
        if mem.get("is_protected"):
            continue
        if (mem.get("access_count") or 0) != 0:
            continue
        created_at = mem.get("created_at") or ""
        if created_at > aa_age_cutoff:
            continue  # too recent — spare it
        storage.delete_memory(mem["id"])
        stats["pruned"] += 1


def _prune_dream_insights(
    candidates: list,
    stats: dict,
    storage: StorageEngine,
    settings: Settings,
) -> None:
    """Pass 4: hard cap dream insights by age regardless of heat or access_count.

    Dream insights start at heat=0.5 and decay to ~0.1 within days, but
    COLD_THRESHOLD=0.02 means they linger for weeks.  Hard cap by age
    regardless of heat or access_count — one accidental recall must not
    let a dream insight escape the age cap forever.
    """
    dream_max_age = settings.DREAM_INSIGHT_MAX_AGE_DAYS
    if dream_max_age <= 0:
        return
    dream_age_cutoff = (datetime.now(UTC) - timedelta(days=dream_max_age)).isoformat()

    for mem in candidates:
        tags = _parse_tags(mem)
        if "dream" not in tags or "auto-generated" not in tags:
            continue
        if mem.get("is_protected"):
            continue
        created_at = mem.get("created_at") or ""
        if created_at > dream_age_cutoff:
            continue  # too recent — spare it
        storage.delete_memory(mem["id"])
        stats["pruned"] += 1


def _prune_action_stream_aged(
    candidates: list,
    stats: dict,
    storage: StorageEngine,
    settings: Settings,
) -> None:
    """Pass 5: prune _action_stream episodics beyond ACTION_STREAM_MAX_AGE_DAYS.

    Action log summaries (tool-call batches) are tagged _action_stream and
    created at heat=0.4.  Pass 1 prunes at heat<0.01 — that takes ~300 days.
    One access (access_count=1) blocks Pass 1 forever, so these pile up
    indefinitely.  Prune unconditionally after ACTION_STREAM_MAX_AGE_DAYS
    unless accessed (access_count > 0 means something found them useful).
    """
    action_stream_max_age = settings.ACTION_STREAM_MAX_AGE_DAYS
    if action_stream_max_age <= 0:
        return
    as_age_cutoff = (datetime.now(UTC) - timedelta(days=action_stream_max_age)).isoformat()

    for mem in candidates:
        tags = _parse_tags(mem)
        if "_action_stream" not in tags:
            continue
        if mem.get("is_protected"):
            continue
        if (mem.get("access_count") or 0) > 0:
            continue  # was accessed — something found it useful
        created_at = mem.get("created_at") or ""
        if created_at > as_age_cutoff:
            continue  # too recent — spare it
        storage.delete_memory(mem["id"])
        stats["pruned"] += 1


def _prune_degenerate_auto_abstracted(
    candidates: list,
    stats: dict,
    storage: StorageEngine,
    settings: Settings,
) -> None:
    """Pass 6: delete degenerate auto-abstracted semantics unconditionally.

    CLS consolidation_cycle can emit "Recurring pattern across N observations:
    frequently modified together" when clusters are built from _memify_derive
    placeholders.  These memories have no subject and are pure noise.
    Delete unconditionally (regardless of age / heat) — they were never
    meaningful.  Protected memories and accessed memories are respected.
    """
    for mem in candidates:
        tags = _parse_tags(mem)
        if "auto-abstracted" not in tags:
            continue
        if mem.get("is_protected"):
            continue
        if (mem.get("access_count") or 0) > 0:
            continue  # user accessed it; treat as legitimate
        content = mem.get("content") or ""
        if _is_degenerate_auto_abstracted(content):
            logger.info(
                "Pruning degenerate auto-abstracted memory %d: %r",
                mem["id"],
                content[:80],
            )
            storage.delete_memory(mem["id"])
            stats["pruned"] += 1


def _memify_prune(
    storage: StorageEngine,
    settings: Settings,
    stats: dict,
) -> None:
    """Delete cold, unaccessed, stale auto-generated memories.

    Pass 1 (action-stream): summaries tagged _action_stream that are cold
    (heat<0.01), low-confidence (<0.3), and never accessed.

    Pass 2 (auto-generated): memories tagged "auto-generated" (derived facts,
    dream insights, CLS semantic promotions) that are cold (heat<COLD_THRESHOLD),
    never accessed (access_count==0 or NONE), older than
    AUTO_GENERATED_MEMORY_MAX_AGE_DAYS, and not protected.

    User-created memories are never touched by either pass.
    """
    candidates = storage.get_memories_by_heat(min_heat=0.0, limit=10000)

    _prune_action_stream_cold(candidates, stats, storage, settings)

    if settings.AUTO_GENERATED_MEMORY_MAX_AGE_DAYS <= 0:
        return  # disabled — gates passes 2-6

    _prune_auto_generated_old(candidates, stats, storage, settings)
    _prune_auto_abstracted_old(candidates, stats, storage, settings)
    _prune_dream_insights(candidates, stats, storage, settings)
    _prune_action_stream_aged(candidates, stats, storage, settings)
    _prune_degenerate_auto_abstracted(candidates, stats, storage, settings)
