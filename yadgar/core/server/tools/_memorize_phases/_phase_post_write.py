"""Phase 6 — post_write: zero-gap hooks + build response dict.

Handles: synaptic boost, prospective memory, engram allocation,
write-gate record, protection, session coherence, micro-checkpoint,
action stream capture, reinjection, CRDT stamp, visualization event,
and final response construction.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import yadgar._shared.runtime.lifecycle as _lifecycle
import yadgar._shared.runtime.state as _st
from yadgar._shared.observability.observe import observe
from yadgar._shared.tracing import trace_span
from yadgar.core.server._helpers import _DECISION_STRONG_RE, _push_event

from .context import MemorizeContext

logger = logging.getLogger(__name__)

# Module-level flag: log once per process when reinjection is gated off
_reinject_skip_logged: bool = False


@trace_span("memorize.post_write")
def phase_post_write(ctx: MemorizeContext, settings) -> dict:
    """Run post-write hooks and build the final response dict.

    Returns the memory dict (success response).
    """
    storage = _lifecycle._get_storage()
    buffer = _lifecycle._get_buffer()

    _run_synaptic_boost(ctx)
    _run_prospective(ctx, storage)
    _run_engram(ctx)
    _run_zero_gap(ctx, storage, buffer, settings)
    _run_tool_call_record()
    _bump_shadow_epoch(ctx)

    return _build_response(ctx, storage, settings)


@observe(tier="stage")
def _bump_shadow_epoch(ctx: MemorizeContext) -> None:
    """v5.96.0: a memorize is a structural write → bump the directory's shadow epoch
    so the recall shadow-cache counter treats prior would-be keys as stale (miss).

    Instrumentation only, fully guarded — must never break or block the write path.
    """
    try:
        # v5.111.0 (Car 1): normalize ctx.context to its git-root before bumping so
        # the epoch lands on the SAME key project_brief reads. bump_epoch(ctx.context)
        # (raw dir) would land on a different _DIR_EPOCH key than the resolved git-root
        # project_brief keys on → the epoch would be decorative and never bust a cached
        # brief on a memorize into a subdir.
        from yadgar.core.server.tools.project import _bump_epoch_for_context  # noqa: PLC0415

        _bump_epoch_for_context(ctx.context)
    except Exception:  # pragma: no cover - instrumentation must never break writes
        pass


@observe(tier="stage")
def _run_synaptic_boost(ctx: MemorizeContext) -> None:
    """Synaptic boost for high-importance memories."""
    thermo = _st._thermo
    if thermo is not None and ctx.importance > 0.7:
        thermo.synaptic_boost(ctx.memory_id, ctx.initial_heat)


@observe(tier="stage")
def _run_prospective(ctx: MemorizeContext, storage) -> None:
    """Auto-create triggers and check existing triggers."""
    if _st._prospective is None:
        return
    _st._prospective.auto_create_from_content(ctx.content, ctx.context)
    trigger_context = {
        "directory": ctx.context,
        "content": ctx.content,
        "entities": ctx.tags,
        "current_time": datetime.now(UTC),
    }
    ctx.triggered_memories = _st._prospective.check_triggers(trigger_context)


@observe(tier="stage")
def _run_engram(ctx: MemorizeContext) -> None:
    """Engram allocation — competitive slot assignment with temporal linking."""
    if _st._engram is None:
        return
    try:
        ctx.engram_result = _st._engram.allocate(ctx.memory_id)
    except Exception:
        logger.debug("Engram allocation failed for memory %s", ctx.memory_id)


@observe(tier="stage")
def _run_zero_gap(ctx: MemorizeContext, storage, buffer, settings) -> None:
    """Zero-gap enhancements 1–5."""
    _zero_gap_1_write_gate(ctx)
    _zero_gap_2_protection(ctx, storage, settings)
    _zero_gap_3_session_coherence(ctx, storage)
    _zero_gap_4_micro_checkpoint(ctx, settings)
    _zero_gap_5_action_stream(ctx, buffer)
    _zero_gap_6_reinjection(ctx, settings)


@observe(tier="stage")
def _zero_gap_1_write_gate(ctx: MemorizeContext) -> None:
    """Record store in write gate for task continuity tracking."""
    if _st._write_gate is not None:
        _st._write_gate.record_stored(ctx.content, ctx.context, ctx.embedding)


@observe(tier="stage")
def _zero_gap_2_protection(ctx: MemorizeContext, storage, settings) -> None:
    """Explicit protection + decision auto-protection."""
    explicit_anchor = ctx.is_protected or "_anchor" in ctx.tags
    if explicit_anchor:
        storage.update_memory_fields(ctx.memory_id, is_protected=1, importance=1.0)
        if "_anchor" not in ctx.tags:
            ctx.tags = list(ctx.tags) + ["_anchor"]
        storage.update_memory_fields(ctx.memory_id, tags=ctx.tags)
        ctx.auto_protected = True
        logger.debug("Explicitly protected: memory %s", ctx.memory_id)
    elif settings.DECISION_AUTO_PROTECT and _DECISION_STRONG_RE.search(ctx.content):
        storage.update_memory_fields(ctx.memory_id, is_protected=1, importance=1.0)
        ctx.auto_protected = True
        logger.debug("Decision auto-protected: memory %s", ctx.memory_id)


@observe(tier="stage")
def _zero_gap_3_session_coherence(ctx: MemorizeContext, storage) -> None:
    """Session coherence: boost heat for current-session memories."""
    thermo = _st._thermo
    if thermo is None:
        return
    mem_data = storage.get_memory(ctx.memory_id)
    if mem_data and mem_data.get("created_at"):
        coherent_heat = thermo.apply_session_coherence(mem_data["heat"], mem_data["created_at"])
        if coherent_heat != mem_data["heat"]:
            storage.update_memory_heat(ctx.memory_id, coherent_heat)


@observe(tier="stage")
def _zero_gap_4_micro_checkpoint(ctx: MemorizeContext, settings) -> None:
    """Micro-checkpoint: auto-checkpoint on significant events."""
    if _st._replay is None or not settings.MICRO_CHECKPOINT_ENABLED:
        return
    gate_surprisal = ctx.gate_result["surprisal"] if ctx.gate_result else 0.0
    should_micro, micro_reason = _st._replay.should_micro_checkpoint(
        ctx.content, ctx.tags, gate_surprisal
    )
    if should_micro:
        try:
            _st._replay.create_micro_checkpoint(ctx.context, ctx.content, micro_reason)
            logger.debug("Micro-checkpoint created: %s", micro_reason)
        except Exception:
            logger.debug("Micro-checkpoint failed")


@observe(tier="stage")
def _zero_gap_5_action_stream(ctx: MemorizeContext, buffer) -> None:
    """Action stream: log this memorize operation."""
    if buffer is not None:
        summary = ctx.content[:150].replace("\n", " ")
        buffer.capture_action("memorize", ctx.context, summary, ctx.curation_action)


@observe(tier="stage")
def _zero_gap_6_reinjection(ctx: MemorizeContext, settings) -> None:
    """Related context reinjection: surface what you already know."""
    global _reinject_skip_logged  # noqa: PLW0603

    if not settings.REINJECT_ON_WRITE:
        if not _reinject_skip_logged:
            logger.debug("reinjection on write is disabled (YADGAR_REINJECT_ON_WRITE=0)")
            _reinject_skip_logged = True
        return

    if not settings.REINJECTION_ENABLED or _st._retriever is None:
        return

    try:
        related = _st._retriever.recall(
            ctx.content[:300],
            max_results=settings.REINJECTION_MAX_RESULTS + 1,
            min_heat=0.0,
        )
        for r in related:
            if r["id"] != ctx.memory_id:
                r_content = r.get("content", "")
                if len(r_content) > 300:
                    r_content = r_content[:300] + "..."
                ctx.related_context.append(
                    {"id": r["id"], "content": r_content, "heat": r.get("heat", 0)}
                )
            if len(ctx.related_context) >= settings.REINJECTION_MAX_RESULTS:
                break
    except Exception:
        logger.debug("Reinjection recall failed")


@observe(tier="stage")
def _run_tool_call_record() -> None:
    """Track tool call for auto-checkpoint interval."""
    if _st._replay is not None:
        _st._replay.record_tool_call()


@observe(tier="stage")
def _build_response(ctx: MemorizeContext, storage, settings) -> dict:
    """Build and return the final memory response dict."""
    memory = storage.get_memory(ctx.memory_id)
    if memory is None:
        return {
            "stored": True,
            "id": ctx.memory_id,
            "memory_id": ctx.memory_id,
            "curation_action": ctx.curation_action,
            "warning": "memory written but not found on readback",
        }

    # Strip binary fields from response
    memory.pop("embedding", None)

    # Publish visualization event
    _push_event(
        {
            "event": "memory_added",
            "node": {
                "id": f"mem:{ctx.memory_id}",
                "type": "memory",
                "heat": memory.get("heat", ctx.initial_heat),
                "content": ctx.content[:200],
                "tags": ctx.tags,
                "directory": ctx.context,
            },
        }
    )

    memory.setdefault("file_hash", None)

    # Stamp CRDT vector_clock on newly created memories
    if ctx.curation_action == "created":
        crdt_agent = settings.CRDT_AGENT_ID
        clock = json.dumps({crdt_agent: 1})
        storage._q(
            "UPDATE type::record('memory', $id) SET provenance_agent = $a, vector_clock = $c",
            {"id": ctx.memory_id, "a": ctx.provenance_agent_resolved, "c": clock},
        )
        memory["provenance_agent"] = ctx.provenance_agent_resolved
        memory["vector_clock"] = clock

    memory["memory_id"] = ctx.memory_id
    memory["curation_action"] = ctx.curation_action

    if ctx.gate_result is not None:
        memory["surprisal"] = ctx.gate_result["surprisal"]
        memory["gate_reason"] = ctx.gate_result["gate_reason"]

    if ctx.triggered_memories:
        memory["triggered_prospective_memories"] = [
            {"id": pm["id"], "content": pm["content"]} for pm in ctx.triggered_memories
        ]

    if ctx.engram_result is not None:
        memory["engram_slot"] = ctx.engram_result["slot_index"]
        memory["temporal_links"] = ctx.engram_result["temporally_linked"]
        memory["temporal_link_count"] = ctx.engram_result["link_count"]

    if ctx.auto_protected:
        memory["auto_protected"] = True
        memory["protection_reason"] = "decision_detected"

    if ctx.related_context:
        memory["related_context"] = ctx.related_context

    return memory
