"""Phase 1 — validate: arg validation, tier/parity, secret gate, write policy, unicode."""

from __future__ import annotations

import logging

import yadgar._shared.runtime.state as _st
from yadgar._shared.observability.observe import observe
from yadgar._shared.secrets import gate_or_reject
from yadgar._shared.tracing import trace_span
from yadgar.core.server._helpers import _has_unpaired_surrogate

from .context import MemorizeContext

logger = logging.getLogger(__name__)

_VALID_TIERS = frozenset({"semantic_immortal", "conditional", "ephemeral"})


@trace_span("memorize.validate")
def phase_validate(ctx: MemorizeContext, settings) -> dict | None:
    """Validate and normalise inputs. Returns rejection dict or None (continue).

    Mutations on ctx:
    - tier may be promoted (is_protected → conditional)
    - is_protected set True when tier is set
    - tags extended with _anchor / anchor:<reason>
    - computed_valid_until set
    - provenance_agent_resolved set
    """
    err = _validate_tier_and_parity(ctx, settings)
    if err:
        return err

    err = _validate_valid_until(ctx, settings)
    if err:
        return err

    _apply_tag_injection(ctx)

    err = _validate_content_and_provenance(ctx)
    if err:
        return err

    return _validate_gate_and_policy(ctx)


@observe(tier="stage")
def _validate_tier_and_parity(ctx: MemorizeContext, settings) -> dict | None:
    """Validate tier value and apply is_protected/tier parity."""
    # v5.8.0: tier validation
    if ctx.tier is not None and ctx.tier not in _VALID_TIERS:
        return {
            "stored": False,
            "reason": f"invalid tier: {ctx.tier!r}. Must be one of {sorted(_VALID_TIERS)}",
        }

    # v5.10.2: is_protected parity — auto-set tier=conditional when unset
    if ctx.is_protected and ctx.tier is None:
        ctx.tier = "conditional"

    # v5.10.2: semantic_immortal requires reason
    if (
        ctx.tier == "semantic_immortal"
        and not ctx.reason
        and getattr(settings, "ANCHOR_SEMANTIC_IMMORTAL_REQUIRES_REASON", False)
    ):
        return {
            "stored": False,
            "reason": "tier=semantic_immortal requires a non-empty reason argument",
        }
    return None


@observe(tier="stage")
def _validate_valid_until(ctx: MemorizeContext, settings) -> dict | None:
    """Validate and compute valid_until / ttl_days conflict + computation."""
    # v5.8.0: conflicting valid_until + ttl_days
    if ctx.valid_until is not None and ctx.ttl_days is not None:
        return {
            "stored": False,
            "reason": "conflict: both valid_until and ttl_days provided — choose one",
        }

    if ctx.tier is not None or ctx.valid_until is not None or ctx.ttl_days is not None:
        try:
            from yadgar.core.server.tools.memorize import _compute_valid_until

            ctx.computed_valid_until = _compute_valid_until(
                ctx.tier, ctx.valid_until, ctx.ttl_days, settings
            )
        except ValueError as exc:
            return {"stored": False, "reason": str(exc)}

    # v5.8.0: tier auto-sets is_protected
    if ctx.tier is not None:
        ctx.is_protected = True
    return None


@observe(tier="stage")
def _apply_tag_injection(ctx: MemorizeContext) -> None:
    """Inject _anchor and anchor:<reason> tags when is_protected."""
    if not ctx.is_protected:
        return
    tags_list = list(ctx.tags)
    if "_anchor" not in tags_list:
        tags_list.append("_anchor")
    if ctx.reason and f"anchor:{ctx.reason}" not in tags_list:
        tags_list.append(f"anchor:{ctx.reason}")
    ctx.tags = tags_list


@observe(tier="stage")
def _validate_content_and_provenance(ctx: MemorizeContext) -> dict | None:
    """Validate content size and provenance_agent."""
    if len(ctx.content) > 32_768:
        return {"stored": False, "reason": "content_too_large", "max_bytes": 32_768}

    ctx.provenance_agent_resolved = (
        ctx.provenance_agent if ctx.provenance_agent is not None else "default"
    )
    try:
        from yadgar._shared.storage.memory import _validate_provenance_agent

        _validate_provenance_agent(ctx.provenance_agent_resolved)
    except ValueError as exc:
        return {"stored": False, "reason": f"invalid_provenance_agent: {exc}"}
    return None


@observe(tier="stage")
def _validate_gate_and_policy(ctx: MemorizeContext) -> dict | None:
    """Run secret gate, write policy, and unicode check."""
    # v5.15.0: secret gate
    gate = gate_or_reject(ctx.content, tags=list(ctx.tags) if ctx.tags else [])
    if gate is not None:
        try:
            from yadgar._shared.metrics import yadgar_writegate_outcome  # noqa: PLC0415

            yadgar_writegate_outcome.labels(outcome="rejected_secret").inc()
        except Exception:
            pass
        return gate

    # Write-path policy rules
    if _st._rules_engine is not None:
        wp_blocked, wp_reason, wp_modified = _st._rules_engine.check_write_policy(
            ctx.content, ctx.context, ctx.tags
        )
        if wp_blocked:
            return {"stored": False, "reason": f"blocked_by_policy: {wp_reason}"}
        if wp_modified is not None:
            ctx.content = wp_modified

    if _has_unpaired_surrogate(ctx.content):
        return {"stored": False, "reason": "invalid_unicode_surrogates"}

    return None
