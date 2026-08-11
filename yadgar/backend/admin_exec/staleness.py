"""Backend execution bodies for the staleness flag-compute ops (T2 Car E1).

Census verdict #8 (layer-boundary train): the heat-decay half of staleness
detection is stateless-over-DB compute and runs backend-side. The host-FS half
(watchdog events, file hashing, directory walks) stays in the core
``StalenessDetector``, which forwards here via ``_forward_admin``:

- ``staleness_file_changed`` — one watchdog event: compare stored hash, flag
  affected memories (heat/2 + is_stale), upsert the new hash.
- ``staleness_scan`` — batch form for directory scans, with global memory-id
  dedup across the walk (a memory matched by several changed files is halved
  exactly once).
- ``staleness_flag_memory`` — single-memory flag: the write half of
  ``validate_memory`` (the host file-hash comparison runs core-side).

Each op is an undecorated ``(payload: dict) -> dict`` function.
"""

from __future__ import annotations

import logging
from pathlib import Path

from yadgar._shared.observability.observe import observe
from yadgar._shared.runtime.lifecycle import _get_storage

logger = logging.getLogger(__name__)


@observe(tier="stage", metric="backend.admin.staleness_flag_for_file")
def _flag_memories_for_file(
    storage, filepath: str, old_hash: str, flagged_memory_ids: set[int]
) -> int:
    """Flag memories affected by one changed file. Returns newly-flagged count.

    Matches by stored file hash PLUS parent-directory context (same two arms
    the core detector historically used). ``flagged_memory_ids`` is the
    caller-owned global dedup set — memories already flagged in this call
    batch are skipped so heat is halved exactly once per batch.
    """
    memories = storage.get_memories_by_file_hash(old_hash)
    # C10g (0047 PR#40 §5) — DELIBERATE CARVE-OUT, do not "fix" this into a
    # project_id. ``parent_dir`` is a CHANGED FILE's parent directory, never a
    # project identity, so there is nothing here to re-key onto.
    # ``get_memories_for_directory`` is now project_id-keyed (its column holds
    # ``owner/repo`` since C10f moved memorize's stamp), so this arm degrades to
    # a no-match: no memory is flagged stale by directory any more. The
    # ``file_hash`` arm above is unaffected and still does the real work — it is
    # the precise arm, and it is what every staleness test exercises. Restoring
    # a directory arm means giving this function a real project identity from
    # its caller, which is C11's shape (the plan's "semantic split"), not a
    # rename this car could make.
    parent_dir = str(Path(filepath).parent)
    dir_memories = storage.get_memories_for_directory(parent_dir, min_heat=0.0)

    flagged = 0
    for m in memories + dir_memories:
        if m["id"] in flagged_memory_ids:
            continue
        flagged_memory_ids.add(m["id"])
        storage.update_memory_heat(m["id"], m["heat"] / 2.0)
        storage.update_memory_staleness(m["id"], True)
        flagged += 1
    return flagged


@observe(tier="boundary", metric="backend.admin.staleness_file_changed")
def staleness_file_changed(payload: dict) -> dict:
    """Handle one file-change event: flag stale memories + upsert the hash.

    payload: {"filepath": str, "new_hash": str}
    Returns {"changed": bool, "memories_flagged": int}.
    """
    filepath = payload["filepath"]
    new_hash = payload.get("new_hash", "")

    storage = _get_storage()
    old_hash = storage.get_file_hash(filepath)

    flagged_ids: set[int] = set()
    changed = old_hash is not None and old_hash != new_hash
    if changed:
        _flag_memories_for_file(storage, filepath, old_hash, flagged_ids)

    storage.upsert_file_hash(filepath, new_hash)
    return {"changed": changed, "memories_flagged": len(flagged_ids)}


@observe(tier="boundary", metric="backend.admin.staleness_scan")
def staleness_scan(payload: dict) -> dict:
    """Batch flag compute for a directory scan (host walk runs core-side).

    payload: {"files": [{"path": str, "hash": str}, ...]}
    Returns {"files_changed": int, "memories_flagged": int} — flagged count is
    globally deduplicated across the batch.
    """
    files = payload.get("files") or []

    storage = _get_storage()
    files_changed = 0
    flagged_memory_ids: set[int] = set()

    for entry in files:
        filepath = entry["path"]
        new_hash = entry.get("hash", "")
        old_hash = storage.get_file_hash(filepath)

        if old_hash is not None and old_hash != new_hash:
            files_changed += 1
            _flag_memories_for_file(storage, filepath, old_hash, flagged_memory_ids)

        storage.upsert_file_hash(filepath, new_hash)

    return {"files_changed": files_changed, "memories_flagged": len(flagged_memory_ids)}


@observe(tier="boundary", metric="backend.admin.staleness_flag_memory")
def staleness_flag_memory(payload: dict) -> dict:
    """Flag a single memory stale (heat/2 + is_stale) — validate_memory write half.

    payload: {"memory_id": int}
    Returns {"memory_id": int, "flagged": bool} (flagged=False when not found).
    """
    memory_id = int(payload["memory_id"])
    storage = _get_storage()
    memory = storage.get_memory(memory_id)
    if memory is None:
        return {"memory_id": memory_id, "flagged": False}

    storage.update_memory_heat(memory_id, memory["heat"] / 2.0)
    storage.update_memory_staleness(memory_id, True)
    return {"memory_id": memory_id, "flagged": True}
