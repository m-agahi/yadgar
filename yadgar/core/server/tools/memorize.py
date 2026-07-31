"""memorize MCP tool registration."""

from __future__ import annotations

import logging
import os

from yadgar._shared.config import get_settings
from yadgar._shared.observability.observe import observe
from yadgar._shared.security.secrets import (
    gate_or_reject,  # noqa: F401 — required by I26 secret-gate check
)
from yadgar._shared.server_helpers import normalize_write_context
from yadgar._shared.write_exec import (
    MemorizeContext,
    phase_validate,
)
from yadgar.core.forward import _forward_admin

# R2a Car D2: _get_file_queue lives in yadgar.core.lifecycle (core → core).
from yadgar.core.lifecycle import _get_file_queue
from yadgar.core.server._app import _tool

logger = logging.getLogger(__name__)

settings = get_settings()

_VALID_TIERS = frozenset({"semantic_immortal", "conditional", "ephemeral"})


@_tool(always_load=True)
def memorize(  # noqa: PLR0913 — MCP tool with frozen 10-arg signature
    content: str,
    context: str,
    tags: list[str],
    is_protected: bool = False,
    provenance_agent: str | None = None,
    tier: str | None = None,
    valid_until: str | None = None,
    ttl_days: int | None = None,
    reason: str = "",
    branch_hint: str | None = None,
    wait: bool = False,
) -> dict:
    """Store a new memory with embedding.

    context MUST be the actual working directory path (e.g., '/home/user/projects/myapp'),
    NOT a description. project_brief() filters by directory path match —
    descriptive strings will make memories unfindable by project.

    Persistence options:
    - is_protected=True: memory is exempt from heat decay and will never be aged out.
      Use this for facts that must persist indefinitely (credentials locations, key
      decisions, permanent constraints). Equivalent to calling anchor() but inline.
    - Alternatively, include "_anchor" in tags for the same effect.
    - Without either flag, memories decay naturally based on heat and last-access time.

    tier: anchor tier — "semantic_immortal" | "conditional" | "ephemeral".
      Setting tier auto-sets is_protected=True.
      Defaults: conditional → 90d TTL; ephemeral → 14d TTL; semantic_immortal → no expiry.

    valid_until: ISO-8601 UTC datetime string. Explicit expiry. Mutually exclusive with ttl_days.

    ttl_days: Shorthand for valid_until = now() + ttl_days. Mutually exclusive with valid_until.

    provenance_agent: identifies the agent or subagent type that stored this memory.
      Defaults to "default". Must be ASCII alphanumeric/hyphen/underscore, ≤64 chars.
      Used for provenance tracking across multi-agent workflows.

    reason: human-readable justification for why this memory is protected.
      Only meaningful when is_protected=True. Adds 'anchor:<reason>' tag.
      Required when tier='semantic_immortal' and ANCHOR_SEMANTIC_IMMORTAL_REQUIRES_REASON=True.

    branch_hint: host-supplied branch name. Used when daemon-side _detect_branch() fails
      (e.g., daemon runs in a container without access to the host .git directory).
      Resolution order: _detect_branch(context) → branch_hint → hard-reject (v5.42.3).
      SessionStart hooks should always pass branch_hint=<current-branch>.

    wait: read-your-writes surface (mirrors wiki_add's wait semantics).
      wait=False (default): enqueue and return {stored, queued, queue_id}
        immediately — the drainer commits asynchronously.
      wait=True: enqueue, nudge the drainer, and block until the write drains
        or WIKI_WRITE_WAIT_TIMEOUT_SECONDS elapses, returning:
          {"stored": True, "committed": True, "queued": False, ...} — committed
          {"stored": False, "reason": "wait_timeout", "queued": True, ...} — still queued
          {"stored": False, "reason": "rejected", "queued": False, ...} — DLQ'd
    """
    # secret-gate: skip — gate_or_reject() called in phase_validate() (see _memorize_phases/_phase_validate.py)
    ctx = MemorizeContext(
        content=content,
        context=context,
        tags=list(tags),
        is_protected=is_protected,
        provenance_agent=provenance_agent,
        tier=tier,
        valid_until=valid_until,
        ttl_days=ttl_days,
        reason=reason,
        branch_hint=branch_hint,
    )

    # Validate + compute valid_until / provenance (still on the request thread:
    # secret gate + policy live here). The sync write pipeline (embed →
    # contradiction → store → post_write) runs ONLY in the backend drainer.
    result = phase_validate(ctx, settings)
    if result is not None:
        return result

    # Resolve the branch to capture at enqueue time, then enqueue and return.
    branch, branch_err = _resolve_memorize_branch(ctx)
    if branch_err is not None:
        return branch_err
    ctx.resolved_branch = branch

    # T2 fold-in (Q1 orphaned-memories fix): collapse worktree contexts to the
    # canonical repo root so rows stay visible to canonical-repo recall. Covers
    # the SubagentStop footer path too (it calls this same tool).
    ctx.context, ctx.resolved_branch = normalize_write_context(ctx.context, ctx.resolved_branch)

    return _enqueue(ctx, wait=wait)


@observe(tier="hot", metric="tools.memorize._resolve_memorize_branch")
def _resolve_memorize_branch(ctx: MemorizeContext) -> tuple[str | None, dict | None]:
    """Resolve branch at the MCP boundary for the enqueue payload.

    Resolution order: _detect_branch(context) → branch_hint → YADGAR_CI_BRANCH.
    Returns (branch, None) on success, (None, error_dict) when branch is absent
    (hard-reject — v5.42.3).
    """
    branch = None
    try:
        import yadgar.core.server as _srv  # noqa: PLC0415

        branch = _srv._detect_branch(ctx.context)
    except Exception:
        pass  # non-fatal — fall through to branch_hint

    if not branch and ctx.branch_hint:
        branch = ctx.branch_hint

    if not branch:
        branch = os.environ.get("YADGAR_CI_BRANCH") or None

    if not branch:
        return None, {
            "error": "missing_branch",
            "stored": False,
            "message": (
                "Branch context required. Supply branch_hint=<current-branch-name> or ensure "
                "the working directory is a git repo accessible to the yadgar daemon."
            ),
            "field": "branch_hint",
            "op_type": "memorize",
        }
    return branch, None


@observe(tier="stage")
def _enqueue(ctx: MemorizeContext, wait: bool = False) -> dict:
    """Enqueue a memorize job. Returns the queued result.

    wait=True routes through _memorize_wait_path for read-your-writes (mirrors
    wiki_add). wait=False returns the async {stored, queued, queue_id} shape.
    """
    payload: dict = {
        "content": ctx.content,
        "context": ctx.context,
        "tags": list(ctx.tags),
        "is_protected": ctx.is_protected,
        "branch": ctx.resolved_branch,
        "provenance_agent": ctx.provenance_agent_resolved,
    }
    if ctx.tier is not None:
        payload["tier"] = ctx.tier
    if ctx.computed_valid_until is not None:
        payload["valid_until"] = ctx.computed_valid_until
    # reason is required for semantic_immortal tier; include in payload so
    # run_memorize_replay can re-validate on the drainer side (R3 write-path).
    if ctx.reason:
        payload["reason"] = ctx.reason

    if wait:
        return _memorize_wait_path(payload)

    job_id = _get_file_queue().enqueue("memorize", payload)
    return {"stored": True, "queued": True, "queue_id": job_id}


@observe(tier="stage", metric="tools.memorize._memorize_wait_path")
def _memorize_wait_path(payload: dict) -> dict:
    """Handle memorize(wait=True): enqueue then poll for the terminal file.

    Mirrors wiki._wiki_add_wait_path exactly: enqueue, nudge the background
    drainer (drain_now) so the caller doesn't wait a full drain interval, then
    poll the shared archive/dlq dirs for the job's terminal state
    (FileQueue.wait_for_job). Reuses the same wait/drain plumbing wiki_add uses —
    no new machinery.

    Returns:
      {"stored": True, "committed": True, "queued": False, "queue_id": ...} — archived
      {"stored": False, "reason": "wait_timeout", "queued": True, ...} — drainer timeout
      {"stored": False, "reason": "rejected", "queued": False, ...} — DLQ'd
    """
    import yadgar._shared.runtime.state as _st  # noqa: PLC0415

    fq = _get_file_queue()
    job_id = fq.enqueue("memorize", payload)

    # Nudge the drainer to flush promptly, matching wiki_add's wait path. Task #29
    # cold-drain fix: the live drainer runs ONLY in the backend after the ADR-0078
    # split (in-core ``_st._queue_drainer`` is None → the in-process nudge is a
    # silent no-op in production). POST a cross-process ``drain_now`` nudge first
    # (synchronous, durable); keep the in-process nudge for single-process runs +
    # existing tests. Best-effort: a failed POST (backend down / older backend) is
    # swallowed and we fall through to the passive poll (mixed-version safe).
    try:
        _forward_admin("drain_now", {})
    except Exception as exc:  # noqa: BLE001 — non-fatal; passive poll still converges
        logger.warning("memorize wait: backend drain_now nudge failed (non-fatal): %s", exc)
    _drainer = _st._queue_drainer
    if _drainer is not None:
        try:
            _drainer.drain_now()
        except Exception as exc:  # noqa: BLE001
            logger.warning("memorize wait: drain_now() failed (non-fatal): %s", exc)

    # Reuse wiki_add's wait-timeout knob (WIKI_WRITE_WAIT_TIMEOUT_SECONDS) rather
    # than inventing a memorize-specific one — the shared file-queue wait budget.
    # (Sibling car #26 owns this knob's default; do not change it here.)
    try:
        timeout = getattr(settings, "WIKI_WRITE_WAIT_TIMEOUT_SECONDS", 5.0)
    except Exception:  # noqa: BLE001
        timeout = 5.0

    outcome = fq.wait_for_job(job_id, timeout=timeout)

    if outcome["status"] == "timeout":
        # Car 3 (contract clarity): wait_timeout is convergence-pending, not a
        # failure. Signal it explicitly (converging=True, committed=False) while
        # keeping stored/reason/queued unchanged for back-compat.
        return {
            "stored": False,
            "committed": False,
            "converging": True,
            "reason": "wait_timeout",
            "queued": True,
            "queue_id": job_id,
            "hint": "Write still queued — will commit on next drain or hit DLQ on repeated failure.",
        }

    if outcome["status"] == "rejected":
        rejection = outcome.get("result")
        if rejection is not None:
            return rejection
        return {
            "stored": False,
            "reason": "rejected",
            "queued": False,
            "queue_id": job_id,
        }

    return {
        "stored": True,
        "committed": True,
        "queued": False,
        "queue_id": job_id,
    }
