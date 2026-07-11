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

    return _enqueue(ctx)


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
def _enqueue(ctx: MemorizeContext) -> dict:
    """Enqueue a memorize job. Returns the queued result."""
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
    job_id = _get_file_queue().enqueue("memorize", payload)
    return {"stored": True, "queued": True, "queue_id": job_id}
