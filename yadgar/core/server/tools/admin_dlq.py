"""DLQ (dead-letter queue) MCP tools: dlq_inspect, dlq_requeue, dlq_dismiss."""

from __future__ import annotations

import logging

import yadgar._shared.runtime.state as _st
from yadgar._shared.observability.observe import observe

# R2a Car D2: _get_file_queue moved to yadgar.core.lifecycle (core → core).
from yadgar.core.lifecycle import _get_file_queue
from yadgar.core.server._app import _tool

logger = logging.getLogger(__name__)

# v5.42.0: failure_reason taxonomy — failure_reason values treated as "rejections"
# (i.e. similarity gate, policy rejected). "permanent_error" is the legacy/default.
# v5.42.5: added "missing_directory" — queue entry lacks directory_context.
# ("repo_wiki_schema_invalid" — the repo_wiki identity-gate rejection reason —
# was removed with repo_wiki's decommission, #33/ADR-0162; the gate that
# produced it no longer exists. The v5.42.3 branch-context rejection was
# removed with branch scoping itself, ADR-0215.)
#
# Task 229: the ``AdminRefusal`` reasons the drainer now stamps belong here and
# not in "failures". ``dlq_requeue``'s taxonomy check is the ONLY thing that
# stops an operator requeueing a job the drainer will refuse again — a refusal
# reason left outside this set mints a requeue loop. The unblock for each is an
# operator action (``wiki_set_mutability``, or re-running the generator) followed
# by ``dlq_requeue(force=True)``, which is exactly the flow this set gates.
# THE COST OF THE TYPE CHECK: a new ``AdminRefusal`` subclass whose ``reason``
# is not added here silently lands in the wrong bucket. Literals rather than an
# import from ``_shared.storage.mutability_gate`` — this module is a core tool
# surface and the reason strings are a wire contract, not an implementation
# detail to be coupled to.
_REJECTION_TAXONOMY: frozenset[str] = frozenset(
    {
        "duplicate_detected",
        "policy_rejected",
        "missing_directory",  # v5.42.5: queue entry lacks directory_context
        "wiki_page_locked",  # task 229: WikiImmutableError, mutability="locked"
        "wiki_page_derived",  # task 229: WikiImmutableError, mutability="derived"
        "wiki_page_immutable",  # task 229: WikiImmutableError fallback reason
        "gate_unavailable",  # PR #65 review finding #10: fail-CLOSED drainer
        # rejection (task 312) — was emitted as a
        # rejection ``reason`` but missing here, so a
        # gate_unavailable row landed in the DLQ but
        # the admin ``--reason`` filter mis-classified
        # it as a failure, not a rejection.
    }
)

# Map of filter values to predicate functions on failure_reason.
# "all" and None: return everything (default behavior).
# "rejections": only rejection taxonomy entries.
# "failures": only permanent_error / missing failure_reason entries.
_VALID_FILTERS = frozenset({"all", "rejections", "failures"})


@observe(tier="hot", metric="tools.admin_dlq._matches_filter")
def _matches_filter(failure_reason: str | None, filter_: str | None) -> bool:
    """Return True if an entry with given failure_reason matches the filter."""
    if not filter_ or filter_ == "all":
        return True
    effective_reason = failure_reason or "permanent_error"
    if filter_ == "rejections":
        return effective_reason in _REJECTION_TAXONOMY
    if filter_ == "failures":
        return effective_reason not in _REJECTION_TAXONOMY
    return True


@_tool()
def dlq_inspect(filter: str | None = None) -> list[dict]:  # noqa: A002 — shadowing built-in intentional
    """List items stuck in the dead-letter queue (failed writes that exhausted retries).

    Returns entries with op_type, attempts, classification, last_error, moved_to_dlq_at,
    file_size, and failure_reason. Each entry has a filename you can pass to dlq_requeue()
    or dlq_dismiss().

    filter: narrow results by failure_reason. The two buckets are defined by
    _REJECTION_TAXONOMY (authoritative — read it rather than this prose, which
    has gone stale before):
      None / "all"    — all entries (default, current behavior)
      "rejections"    — the system DECIDED to refuse: a similarity duplicate
                        (duplicate_detected), a policy rejection
                        (policy_rejected, missing_directory), or a wiki
                        mutability refusal (wiki_page_locked /
                        wiki_page_derived / wiki_page_immutable). Requeueing
                        one unchanged reproduces the refusal.
      "failures"      — everything else, i.e. the write FAILED rather than being
                        refused: permanent_error (or absent, the legacy shape),
                        transient_error (task 229 — retries exhausted against a
                        transient fault), and any reason not in the taxonomy.

    KNOWN GAP: slug_exists and missing_project_id are stamped by the drainer but
    are in neither set, so they land in "failures" and stay requeueable. That
    predates task 229 and is not corrected here.

    These operations will NOT be retried automatically. Fix the root cause first, then
    call dlq_requeue(filename) to send them back through the queue. For rejection entries
    (duplicate_detected), use wiki_add(force=True) or delete the existing page, then retry.
    For a mutability refusal, call wiki_set_mutability, then dlq_requeue(force=True).
    Or call dlq_dismiss(filename) to acknowledge and drop the entry.
    """
    import json as _json

    if filter is not None and filter not in _VALID_FILTERS:
        return [
            {"error": f"Invalid filter value: {filter!r}. Must be one of: {sorted(_VALID_FILTERS)}"}
        ]

    fq = _get_file_queue()
    if not fq.dlq_dir.exists():
        return []
    results = []
    for sidecar in sorted(fq.dlq_dir.glob("*.json.error.json")):
        try:
            meta = _json.loads(sidecar.read_text())
        except Exception:
            meta = {}
        fname = sidecar.name[: -len(".error.json")]
        # v5.42.0: read failure_reason for filter + surface in output
        failure_reason: str = meta.get("failure_reason") or "permanent_error"
        if not _matches_filter(failure_reason, filter):
            continue
        main_file = fq.dlq_dir / fname
        try:
            file_size = main_file.stat().st_size if main_file.exists() else None
        except OSError:
            file_size = None
        entry = {
            # BOTH keys, deliberately. ``dlq_requeue``/``dlq_dismiss`` take
            # ``filename=`` and this docstring has always promised
            # ``filename``, but the entry only ever carried ``file`` — so a
            # caller following the documented contract read ``None`` off every
            # row and concluded the DLQ could not be cleared at all (2026-08-15,
            # against a 1,104-entry backlog). ``file`` is kept because existing
            # readers use it; the pair is cheap next to that failure mode.
            "file": fname,
            "filename": fname,
            "op_type": meta.get("op_type", "unknown"),
            "attempts": meta.get("attempts"),
            "classification": meta.get("classification"),
            "last_error": (meta.get("last_error") or "")[:200],
            "first_failed_at": meta.get("first_failed_at"),
            "moved_to_dlq_at": meta.get("moved_to_dlq_at"),
            "file_size": file_size,
            "failure_reason": failure_reason,
        }
        # v5.42.0: surface failure_metadata for rejection entries (candidates etc.)
        if meta.get("failure_metadata"):
            entry["failure_metadata"] = meta["failure_metadata"]
        results.append(entry)
    return results


# v5.42.0: rejection error message — surfaced when dlq_requeue blocks a rejection entry.
_REQUEUE_REJECTION_ERROR = (
    "rejection entry — cannot auto-requeue. "
    "Options: (1) use wiki_add(force=True) to bypass the similarity gate, "
    "(2) delete the existing duplicate via wiki_delete, then retry, "
    "(3) call dlq_dismiss(filename) to acknowledge and drop this entry."
)


@_tool(power=True)
def dlq_requeue(filename: str, force: bool = False) -> dict:
    """Move a DLQ item back to the queue so it will be retried on the next drain pass.

    Call after fixing the root cause of the failure. The item's retry counter is reset.

    v5.42.0: entries with failure_reason in the rejection taxonomy (duplicate_detected,
    policy_rejected) CANNOT be requeued — drainer would reject them again. Use:
    - wiki_add(force=True, ...) to bypass the similarity gate
    - wiki_delete the existing duplicate first, then retry
    - dlq_dismiss(filename) to acknowledge and drop the entry

    Note: force=True bypasses the taxonomy gate ONLY. The drainer still re-validates
    the payload content — a payload that still fails validation after force=True is
    rejected again.

    filename: exact filename from dlq_inspect() (e.g. "0001778139482800_<uuid>.json")
    force: if True, bypass taxonomy blocking (after the operator has fixed the payload)
    """
    import json as _json

    # §4: Reject null bytes, Unicode separators, path traversal chars.
    # U+202F NARROW NO-BREAK SPACE, U+200B ZERO WIDTH SPACE,
    # U+2028 LINE SEPARATOR, U+2029 PARAGRAPH SEPARATOR
    _FORBIDDEN_IN_FILENAME = ("/", "\\", "\x00", "\u202f", "\u200b", "\u2028", "\u2029")
    if filename.startswith(".") or any(c in filename for c in _FORBIDDEN_IN_FILENAME):
        return {"requeued": False, "error": "Invalid filename — must be a plain filename"}
    fq = _get_file_queue()
    src = fq.dlq_dir / filename
    if not src.exists():
        return {"requeued": False, "error": f"Not found in DLQ: {filename}"}

    # v5.42.0: block requeue for rejection taxonomy entries unless force=True.
    sidecar = fq.dlq_dir / (filename + ".error.json")
    if sidecar.exists() and not force:
        try:
            meta = _json.loads(sidecar.read_text())
            failure_reason = meta.get("failure_reason") or "permanent_error"
            if failure_reason in _REJECTION_TAXONOMY:
                return {"requeued": False, "error": _REQUEUE_REJECTION_ERROR}
        except Exception:
            pass  # If sidecar unreadable, fall through to allow requeue

    dest = fq.queue_dir / filename
    if dest.exists():
        return {"requeued": False, "error": f"Already exists in queue: {filename}"}
    try:
        src.rename(dest)
    except OSError as exc:
        return {"requeued": False, "error": str(exc)}
    # Remove sidecar
    sidecar.unlink(missing_ok=True)
    # Reset in-memory retry tracker
    if _st._queue_drainer is not None:
        _st._queue_drainer.reset_attempt(filename)
    return {
        "requeued": True,
        "file": filename,
        "message": "Item will be retried on next drain pass",
    }


@_tool(power=True)
def dlq_dismiss(filename: str) -> dict:
    """Remove a DLQ entry without retry — acknowledge and drop it.

    Use when you've reviewed a rejection (duplicate_detected) and decided it's not
    worth retrying (e.g., the content is genuinely duplicate and should be discarded).

    Removes both the queue file and its .error.json sidecar atomically.

    I26 note: dlq_dismiss accepts no user-supplied content — no secret scan needed.
    Power-gated (requires power tool approval per I26 protocol).

    filename: exact filename from dlq_inspect() (e.g. "0001778139482800_<uuid>.json")
    """
    _FORBIDDEN_IN_FILENAME = ("/", "\\", "\x00", " ", " ", "​", " ", " ")
    if filename.startswith(".") or any(c in filename for c in _FORBIDDEN_IN_FILENAME):
        return {"dismissed": False, "error": "Invalid filename — must be a plain filename"}
    fq = _get_file_queue()
    src = fq.dlq_dir / filename
    if not src.exists():
        return {"dismissed": False, "error": f"Not found in DLQ: {filename}"}
    try:
        src.unlink()
    except OSError as exc:
        return {"dismissed": False, "error": str(exc)}
    # Remove sidecar (best-effort)
    (fq.dlq_dir / (filename + ".error.json")).unlink(missing_ok=True)
    logger.info("dlq_dismiss: removed %s from DLQ", filename)
    return {
        "dismissed": True,
        "file": filename,
        "message": "Entry removed from DLQ",
    }
