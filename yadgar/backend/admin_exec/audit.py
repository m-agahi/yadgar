"""Backend execution bodies for the anchor-audit write ops (R3 Car 3d / R5).

The anchor-audit MCP tool ``audit_anchors`` (and the consolidation
``_run_anchor_audit_pass``) build their action lists from ``storage._q`` SELECTs
that STAY in ``yadgar.core.server.tools.audit`` — reads are allowed in core, and
``project_brief``'s signals path shares one of those readers
(``_fetch_cross_project_candidates``). Only the WRITE half forwards here:

  - ``audit_apply_mutations`` — applies the forget_expired / merge DELETEs the
    core dry-run built, logs each to action_log, and bumps the directory's
    structural epoch (a delete is a structural write, mirroring
    ``memory.forget``).
  - ``write_audit_sentinel`` — the latest-wins ``_audit_anchors`` sentinel
    CREATE/DELETE written by the consolidation audit pass. Matches the prior
    core behaviour: NO epoch bump (sentinel is a system marker, not user data).

Each op is an undecorated ``(payload: dict) -> dict`` function; storage is
fetched via the shared lifecycle getter (the /admin route builds the slim engine
set first).
"""

from __future__ import annotations

import json
import logging

from yadgar._shared.observability.observe import observe
from yadgar._shared.runtime.lifecycle import _get_storage

logger = logging.getLogger(__name__)


@observe(tier="stage", metric="backend.admin.audit._log_audit_action")
def _log_audit_action(storage, directory: str, action: str, payload: dict) -> None:
    """Log an audit mutation to action_log using the existing insert_action_log API."""
    try:
        summary = json.dumps({"source": "audit_anchors", "action": action, **payload})[:500]
        storage.insert_action_log(
            tool_name="audit_anchors",
            tool_input_summary=summary,
            directory=directory,
            session_id="audit",
            timestamp=storage._now_iso(),
        )
    except Exception:  # BLE001-KEEP: audit-trail write: insert_action_log reaches storage, which raises with no common base, and a lost audit row must not abort the audit action it records
        logger.debug("_log_audit_action failed (non-fatal)", exc_info=True)


@observe(tier="stage", metric="backend.admin.audit._apply_forget_expired")
def _apply_forget_expired(storage, resolved: str, action_entry: dict) -> dict | None:
    """Apply a single forget_expired action. Returns applied entry or None on skip/error."""
    mid = action_entry["id"]
    try:
        row = storage._q(
            "SELECT id FROM memory WHERE id = type::record('memory', $id) LIMIT 1",
            {"id": mid},
        )
        if not row:
            return None
        storage.delete_memory(mid)
        _log_audit_action(
            storage,
            resolved,
            "forget_expired",
            {"memory_id": mid, "expired_at": action_entry.get("expired_at", "")},
        )
        return {"action": "forget_expired", "id": mid, "status": "deleted"}
    except Exception:  # BLE001-KEEP: per-action isolation in the anchor-audit sweep: delete_memory plus the audit write reach storage with no common base, and one failed forget must not abandon the remaining actions
        logger.debug("forget_expired failed for id=%s", mid, exc_info=True)
        return None


@observe(tier="stage", metric="backend.admin.audit._apply_merge")
def _apply_merge(storage, resolved: str, action_entry: dict) -> dict | None:
    """Apply a single merge action. Returns applied entry or None on skip/error."""
    forget_id = action_entry.get("forget_id")
    keep_id = action_entry.get("keep_id")
    if forget_id is None:
        return None
    try:
        row = storage._q(
            "SELECT id FROM memory WHERE id = type::record('memory', $id) LIMIT 1",
            {"id": forget_id},
        )
        if not row:
            return None
        storage.delete_memory(forget_id)
        _log_audit_action(
            storage,
            resolved,
            "merge",
            {
                "kept_id": keep_id,
                "forgotten_id": forget_id,
                "similarity": action_entry.get("similarity"),
            },
        )
        return {
            "action": "merge",
            "kept_id": keep_id,
            "forgotten_id": forget_id,
            "status": "merged",
        }
    except Exception:  # BLE001-KEEP: per-action isolation for the merge action; same untypeable storage surface as _apply_forget_expired above
        logger.debug("merge failed for forget_id=%s", forget_id, exc_info=True)
        return None


@observe(tier="boundary", metric="backend.admin.audit_apply_mutations")
def audit_apply_mutations(payload: dict) -> dict:
    """Apply the (already-built, core-side) anchor-audit mutations. Storage-write half.

    payload: {"resolved": str, "actions": [<action dict>, ...]}
    The ``actions`` list is the dry-run output the core ``audit_anchors`` shell
    built via reads; here we execute the forget_expired / merge DELETEs, skip
    entries flagged ``skipped``/``promote``/unknown, and log each applied one.

    Returns {"applied": [<applied entry>, ...]}.

    A delete is a STRUCTURAL write — after applying (if any rows were forgotten)
    bump the resolved directory's epoch so cached project_brief for that dir busts.
    Cross-process via the shared queue volume (Car 2). Guarded: never breaks the op.
    """
    resolved = payload["resolved"]
    actions = payload.get("actions") or []
    storage = _get_storage()

    _APPLY = {
        "forget_expired": _apply_forget_expired,
        "merge": _apply_merge,
    }
    applied: list[dict] = []
    for action_entry in actions:
        if action_entry.get("skipped"):
            continue
        act = action_entry.get("action")
        fn = _APPLY.get(act)
        if fn is None:
            continue  # promote and unknown actions not applied
        result = fn(storage, resolved, action_entry)
        if result is not None:
            applied.append(result)

    if applied:
        try:
            from yadgar._shared.server_helpers import _bump_epoch_for_context  # noqa: PLC0415

            _bump_epoch_for_context(resolved)
        except Exception:  # noqa: BLE001 - instrumentation must never break the op
            pass

    return {"applied": applied}


@observe(tier="boundary", metric="backend.admin.write_audit_sentinel")
def write_audit_sentinel(payload: dict) -> dict:
    """Write the latest-wins _audit_anchors sentinel memory for a directory. Storage-write half.

    payload: {"directory": str, "audit_result": {actions, scanned, coverage, _truncated}}
    Deletes any existing _audit_anchors sentinel for the directory, then creates
    a fresh one. Matches prior core behaviour: NO epoch bump (system marker).

    ``coverage`` (task 408) is carried through VERBATIM from the ``audit_anchors``
    result — it is not recomputed here. Task 391 built that block in
    ``core/server/tools/_audit_coverage.py`` because the scan selector
    (``'_anchor' INSIDE tags AND directory_context = $dir``) is narrower than
    "this project's protected rows", so a bare ``scanned`` overstates coverage.
    ``_run_anchor_audit_pass`` already forwards the whole result across this
    boundary; this serialiser simply dropped the key, so the UNATTENDED nightly
    sentinel kept recording the exact unqualified number 391 exists to qualify.

    Returns {"written": bool}.
    """
    directory = payload["directory"]
    audit_result = payload.get("audit_result") or {}
    storage = _get_storage()
    try:
        # An audit_result with no coverage says so, rather than omitting the key:
        # a reader could otherwise not tell "not computed" from "pre-391 build".
        # ``is None``, NOT ``or``: an empty-but-present coverage block is recorded
        # verbatim. Claiming "absent" for a block that was in fact handed over
        # would be a false statement in the one field added to prevent those.
        coverage = audit_result.get("coverage")
        if coverage is None:
            coverage = {"error": "coverage absent from audit result"}
        sentinel_content = json.dumps(
            {
                "actions": audit_result.get("actions", []),
                "scanned": audit_result.get("scanned", 0),
                "coverage": coverage,
                "_truncated": audit_result.get("_truncated", False),
                "audited_at": storage._now_iso(),
            }
        )
        now = storage._now_iso()
        storage._q(
            "DELETE FROM memory WHERE directory_context = $dir AND '_audit_anchors' INSIDE tags",
            {"dir": directory},
        )
        sid = storage._next_id("memory")
        storage._q(
            "CREATE type::record('memory', $id) SET "
            "content = $content, directory_context = $dir, "
            "tags = $tags, heat = 0.0, is_protected = false, "
            "created_at = $now, last_accessed = $now, access_count = 0",
            {
                "id": sid,
                "content": sentinel_content,
                "dir": directory,
                "tags": ["_audit_anchors", "_system"],
                "now": now,
            },
        )
        return {"written": True}
    except Exception:  # BLE001-KEEP: sentinel write via storage._q with no common base; the caller reads {written: False} as a soft outcome, so a failed sentinel must not fail the sweep
        logger.debug("write_audit_sentinel: failed for dir=%s", directory, exc_info=True)
        return {"written": False}
