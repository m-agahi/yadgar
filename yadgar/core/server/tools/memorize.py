"""memorize MCP tool registration."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from yadgar._shared.config import get_settings
from yadgar._shared.observability.observe import observe
from yadgar._shared.secrets import gate_or_reject  # noqa: F401 — required by I26 secret-gate check
from yadgar.core.file_queue import (
    is_draining,  # noqa: F401 — re-exported for backward compat (tests)
)
from yadgar.core.server._app import _tool

from ._memorize_phases import (
    MemorizeContext,
    phase_contradiction,
    phase_embed,
    phase_post_write,
    phase_resolve_branch,
    phase_store,
    phase_validate,
)

logger = logging.getLogger(__name__)

settings = get_settings()

_VALID_TIERS = frozenset({"semantic_immortal", "conditional", "ephemeral"})


@observe(tier="hot", metric="tools.memorize._compute_valid_until")
def _compute_valid_until(
    tier: str | None,
    valid_until: str | None,
    ttl_days: int | None,
    settings,
) -> str | None:
    """Compute the valid_until ISO-8601 UTC string from tier/valid_until/ttl_days.

    Resolution order:
      1. valid_until provided → validate timezone + return as-is.
      2. ttl_days provided → now + ttl_days.
      3. tier=semantic_immortal → None (no expiry).
      4. tier=conditional → now + ANCHOR_CONDITIONAL_TTL_DAYS.
      5. tier=ephemeral → now + ANCHOR_EPHEMERAL_TTL_DAYS.
      6. tier=None → None (non-anchor memory, no expiry logic).
    """
    if valid_until is not None:
        try:
            dt = datetime.fromisoformat(valid_until)
        except ValueError as exc:
            raise ValueError(f"invalid valid_until format: {valid_until!r}") from exc
        if dt.tzinfo is None:
            raise ValueError("valid_until must be timezone-aware UTC (naive datetime rejected)")
        return valid_until
    if ttl_days is not None:
        return (datetime.now(UTC) + timedelta(days=int(ttl_days))).isoformat()
    if tier == "semantic_immortal":
        return None
    if tier == "conditional":
        days = int(getattr(settings, "ANCHOR_CONDITIONAL_TTL_DAYS", 90))
        return (datetime.now(UTC) + timedelta(days=days)).isoformat()
    if tier == "ephemeral":
        days = int(getattr(settings, "ANCHOR_EPHEMERAL_TTL_DAYS", 14))
        return (datetime.now(UTC) + timedelta(days=days)).isoformat()
    return None


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

    result = phase_validate(ctx, settings)
    if result is not None:
        return result

    result = phase_resolve_branch(ctx)
    if result is not None:
        return result

    # Sync path (drain replay or enqueue fallback)
    result = phase_embed(ctx, settings)
    if result is not None:
        return result

    result = phase_contradiction(ctx)
    if result is not None:
        return result

    phase_store(ctx)

    return phase_post_write(ctx, settings)
