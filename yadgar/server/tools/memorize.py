"""memorize and remember MCP tool registrations."""

from __future__ import annotations

import json
import logging
from datetime import UTC
from pathlib import Path

import yadgar.server._state as _st
from yadgar.config import get_settings
from yadgar.file_queue import is_draining
from yadgar.secrets import check_secrets
from yadgar.server._app import _tool
from yadgar.server._helpers import (
    _DECISION_STRONG_RE,
    _file_hash,
    _has_unpaired_surrogate,
    _push_event,
)
from yadgar.server.lifecycle import (
    _get_buffer,
    _get_embeddings,
    _get_file_queue,
    _get_storage,
)

logger = logging.getLogger(__name__)

settings = get_settings()

_reinject_skip_logged: bool = False  # I12: log once per process when reinjection is gated off


@_tool()
def memorize(  # noqa: C901 — pre-existing complexity, tracked for P13 refactor
    content: str,
    context: str,
    tags: list[str],
    is_protected: bool = False,
    provenance_agent: str | None = None,
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

    provenance_agent: identifies the agent or subagent type that stored this memory.
      Defaults to "default". Must be ASCII alphanumeric/hyphen/underscore, ≤64 chars.
      Used for provenance tracking across multi-agent workflows.
    """
    if len(content) > 32_768:
        return {"stored": False, "reason": "content_too_large", "max_bytes": 32_768}

    # Validate and normalise provenance_agent before any further processing
    _provenance_agent: str = provenance_agent if provenance_agent is not None else "default"
    try:
        from yadgar.storage.memory import _validate_provenance_agent

        _validate_provenance_agent(_provenance_agent)
    except ValueError as _ve:
        return {"stored": False, "reason": f"invalid_provenance_agent: {_ve}"}

    # Secret detection — always on, fires before anything else
    sec_blocked, sec_reason, sec_pattern = check_secrets(content)
    if sec_blocked:
        try:
            from yadgar.metrics import yadgar_writegate_outcome  # noqa: PLC0415

            yadgar_writegate_outcome.labels(outcome="rejected_secret").inc()
        except Exception:
            pass
        return {"stored": False, "reason": sec_reason, "pattern_matched": sec_pattern}

    # Write-path policy rules — may block or redact content
    if _st._rules_engine is not None:
        wp_blocked, wp_reason, wp_modified = _st._rules_engine.check_write_policy(
            content, context, tags
        )
        if wp_blocked:
            return {"stored": False, "reason": f"blocked_by_policy: {wp_reason}"}
        if wp_modified is not None:
            content = wp_modified

    if _has_unpaired_surrogate(content):
        return {"stored": False, "reason": "invalid_unicode_surrogates"}

    # Capture branch at API boundary — must happen before any enqueue so
    # the drainer replays with the branch value from write time.
    _branch = None
    try:
        import yadgar.server as _srv

        _branch = _srv._detect_branch(context)
    except Exception:
        pass  # non-fatal — memory inserts with branch=NONE

    # Async path: enqueue and return immediately (skip during drain replay)
    if not is_draining():
        try:
            _fq_path = _get_file_queue().enqueue(
                "memorize",
                {
                    "content": content,
                    "context": context,
                    "tags": list(tags),
                    "is_protected": is_protected,
                    "branch": _branch,
                    "provenance_agent": _provenance_agent,
                },
            )
            from pathlib import Path as _Path

            return {"stored": True, "queued": True, "queue_id": _Path(_fq_path).name}
        except Exception as _fq_exc:
            logger.warning(
                "enqueue_failed",
                extra={
                    "component": "memorize",
                    "action": "enqueue",
                    "outcome": "error",
                    "error": type(_fq_exc).__name__,
                    "fallback": "sync",
                },
            )

    # Sync path — only runs during drain replay (is_draining=True) or queue fallback
    storage = _get_storage()
    embeddings = _get_embeddings()
    buffer = _get_buffer()

    # Predictive coding write gate — FIRST check before any storage
    gate_result = None
    if _st._write_gate is not None:
        should_store, surprisal, reason = _st._write_gate.should_store(content, context, tags)
        gate_result = {
            "surprisal": round(surprisal, 4),
            "gate_reason": reason,
        }
        if not should_store:
            try:
                from yadgar.metrics import yadgar_writegate_outcome  # noqa: PLC0415

                yadgar_writegate_outcome.labels(outcome="skipped_low_surprise").inc()
            except Exception:
                pass
            return {
                "stored": False,
                "surprisal": round(surprisal, 4),
                "reason": reason,
                "message": "Memory below surprisal threshold, skipped",
            }

    # Archive path var not needed in sync path (drainer archives on its own)
    _fq_path = None

    # Generate contextual prefix for richer embedding semantics
    contextual_prefix = None
    retriever = _st._retriever
    if retriever is not None and settings.CONTEXTUAL_PREFIX_ENABLED:
        from datetime import datetime

        contextual_prefix = retriever.generate_contextual_prefix(
            content, context, tags, datetime.now(UTC)
        )

    # Embed with contextual prefix prepended if available
    embed_text = f"{contextual_prefix}{content}" if contextual_prefix else content
    embedding = embeddings.encode(embed_text)
    fhash = _file_hash(context)

    # Compute thermodynamic scores
    thermo = _st._thermo
    if thermo is not None:
        surprise = thermo.compute_surprise(content, context)
        importance = thermo.compute_importance(content, tags)
        valence = thermo.compute_valence(content)
        initial_heat = thermo.apply_surprise_boost(1.0, surprise)
    else:
        surprise = 0.0
        importance = 0.5
        valence = 0.0
        initial_heat = 1.0

    # C4: LLM conflict resolution (Mem0 parity, Ollama-only, env-gated).
    # Runs before insert. On error always degrades to ADD (fail-soft).
    import os as _os

    if _os.environ.get("YADGAR_CONFLICT_RESOLVER", "off").lower() == "on":
        try:
            from yadgar.conflict_resolver import resolve_conflict

            _cr_candidate = {"content": content, "tags": list(tags), "context": context}
            _cr_result = resolve_conflict(_cr_candidate)
            _cr_op = _cr_result.get("op", "ADD")
            _cr_target_id = _cr_result.get("target_id")
            _cr_reason = _cr_result.get("reason", "")
            logger.info(
                "conflict_resolver: op=%s target_id=%s reason=%r",
                _cr_op,
                _cr_target_id,
                _cr_reason,
            )
            if _cr_op == "NOOP":
                return {
                    "stored": False,
                    "reason": "conflict_resolver_noop",
                    "cr_reason": _cr_reason,
                }
            if _cr_op == "UPDATE" and _cr_target_id is not None:
                try:
                    storage.update_memory_fields(
                        _cr_target_id,
                        content=content,
                        tags=list(tags),
                    )
                    return {
                        "stored": True,
                        "action": "conflict_resolver_update",
                        "memory_id": _cr_target_id,
                        "cr_reason": _cr_reason,
                    }
                except Exception as _cr_exc:
                    logger.warning(
                        "conflict_resolver UPDATE failed (%s), falling back to ADD", _cr_exc
                    )
            if _cr_op == "DELETE" and _cr_target_id is not None:
                try:
                    storage.delete_memory(_cr_target_id)
                except Exception as _cr_exc:
                    logger.warning("conflict_resolver DELETE failed (%s), skipping", _cr_exc)
                return {
                    "stored": False,
                    "reason": "conflict_resolver_delete",
                    "cr_reason": _cr_reason,
                }
            # ADD (or fallthrough from failed UPDATE): proceed with normal insert below
        except Exception as _cr_outer_exc:
            logger.warning("conflict_resolver outer error (%s) — degrading to ADD", _cr_outer_exc)

    # Use curator for intelligent ingestion (merge/link/create)
    curator = _st._curator
    if curator is not None and embedding is not None:
        curation_result = curator.curate_on_remember(
            content,
            context,
            tags,
            embedding,
            initial_heat=initial_heat,
            surprise=surprise,
            importance=importance,
            valence=valence,
            file_hash=fhash,
            embedding_model=embeddings.get_model_name(),
            contextual_prefix=contextual_prefix,
        )
        memory_id = curation_result["memory_id"]
        curation_action = curation_result["action"]
        # Race: candidate was deleted between search and merge — fall back to direct insert
        if memory_id is None:
            memory_id = storage.insert_memory(
                {
                    "content": content,
                    "embedding": embedding,
                    "tags": tags,
                    "directory_context": context,
                    "heat": initial_heat,
                    "is_stale": False,
                    "file_hash": fhash,
                    "embedding_model": embeddings.get_model_name(),
                    "provenance_agent": _provenance_agent,
                },
                branch=_branch,
            )
            curation_action = "created"
        elif _branch is not None:
            # Curator inserted the memory — backfill branch via update
            storage.update_memory_fields(memory_id, branch=_branch)
    else:
        # Fallback: direct insert (no curator or no embedding)
        memory_id = storage.insert_memory(
            {
                "content": content,
                "embedding": embedding,
                "tags": tags,
                "directory_context": context,
                "heat": initial_heat,
                "is_stale": False,
                "file_hash": fhash,
                "embedding_model": embeddings.get_model_name(),
                "provenance_agent": _provenance_agent,
            },
            branch=_branch,
        )

        if contextual_prefix:
            storage.update_memory_fields(memory_id, contextual_prefix=contextual_prefix)

        storage.update_memory_scores(
            memory_id,
            surprise_score=surprise,
            importance=importance,
            emotional_valence=valence,
        )
        curation_action = "created"

    # CLS dual-store: classify memory as episodic or semantic
    if _st._consolidation is not None and _st._consolidation.cls is not None:
        store_type = _st._consolidation.cls.classify_memory(content, tags, context)
        storage.update_memory_fields(memory_id, store_type=store_type)

    # Register file hash so staleness detector can find the filepath later
    if fhash is not None:
        storage.upsert_file_hash(context, fhash)

    # Capture in sensory buffer
    buffer.capture(content, context)

    # Record activity on consolidation engine
    if _st._consolidation is not None:
        _st._consolidation.record_activity()

    # Assign to astrocyte processes for domain-aware consolidation
    if _st._pool is not None:
        mem_data = storage.get_memory(memory_id)
        if mem_data:
            _st._pool.assign_memory(mem_data)

    # Synaptic boost for high-importance memories
    if thermo is not None and importance > 0.7:
        thermo.synaptic_boost(memory_id, initial_heat)

    # Prospective memory: auto-create triggers from content & check existing triggers
    triggered_memories = []
    if _st._prospective is not None:
        _st._prospective.auto_create_from_content(content, context)

        from datetime import datetime as _dt

        trigger_context = {
            "directory": context,
            "content": content,
            "entities": tags,
            "current_time": _dt.now(UTC),
        }
        triggered_memories = _st._prospective.check_triggers(trigger_context)

    # Engram allocation — competitive slot assignment with temporal linking
    engram_result = None
    if _st._engram is not None:
        try:
            engram_result = _st._engram.allocate(memory_id)
        except Exception:
            logger.debug("Engram allocation failed for memory %s", memory_id)

    # ── Zero-Gap Enhancements ────────────────────────────────────────────

    # 1. Record store in write gate for task continuity tracking
    if _st._write_gate is not None:
        _st._write_gate.record_stored(content, context, embedding)

    # 2. Explicit protection + decision auto-protection
    auto_protected = False
    explicit_anchor = is_protected or "_anchor" in tags
    if explicit_anchor:
        storage.update_memory_fields(memory_id, is_protected=1, importance=1.0)
        if "_anchor" not in tags:
            tags = list(tags) + ["_anchor"]
            storage.update_memory_fields(memory_id, tags=tags)
        auto_protected = True
        logger.debug("Explicitly protected: memory %s", memory_id)
    elif settings.DECISION_AUTO_PROTECT and _DECISION_STRONG_RE.search(content):
        storage.update_memory_fields(memory_id, is_protected=1, importance=1.0)
        auto_protected = True
        logger.debug("Decision auto-protected: memory %s", memory_id)

    # 3. Session coherence: boost heat for current-session memories
    if thermo is not None:
        mem_data = storage.get_memory(memory_id)
        if mem_data and mem_data.get("created_at"):
            coherent_heat = thermo.apply_session_coherence(mem_data["heat"], mem_data["created_at"])
            if coherent_heat != mem_data["heat"]:
                storage.update_memory_heat(memory_id, coherent_heat)

    # 4. Micro-checkpoint: auto-checkpoint on significant events
    if _st._replay is not None and settings.MICRO_CHECKPOINT_ENABLED:
        gate_surprisal = gate_result["surprisal"] if gate_result else 0.0
        should_micro, micro_reason = _st._replay.should_micro_checkpoint(
            content, tags, gate_surprisal
        )
        if should_micro:
            try:
                _st._replay.create_micro_checkpoint(context, content, micro_reason)
                logger.debug("Micro-checkpoint created: %s", micro_reason)
            except Exception:
                logger.debug("Micro-checkpoint failed")

    # 5. Action stream: log this remember operation
    if buffer is not None:
        summary = content[:150].replace("\n", " ")
        buffer.capture_action("memorize", context, summary, curation_action)

    # 6. Related context reinjection: surface what you already know
    # P7: gated by YADGAR_REINJECT_ON_WRITE (default OFF) per I1/I9 — skips
    # sync vector search on write path when disabled.
    global _reinject_skip_logged
    related_context = []
    if not settings.REINJECT_ON_WRITE:
        if not _reinject_skip_logged:
            logger.debug("reinjection on write is disabled (YADGAR_REINJECT_ON_WRITE=0)")
            _reinject_skip_logged = True
    elif settings.REINJECTION_ENABLED and _st._retriever is not None:
        try:
            related = _st._retriever.recall(
                content[:300],
                max_results=settings.REINJECTION_MAX_RESULTS + 1,
                min_heat=0.0,
            )
            for r in related:
                if r["id"] != memory_id:
                    r_content = r.get("content", "")
                    if len(r_content) > 300:
                        r_content = r_content[:300] + "..."
                    related_context.append(
                        {
                            "id": r["id"],
                            "content": r_content,
                            "heat": r.get("heat", 0),
                        }
                    )
                if len(related_context) >= settings.REINJECTION_MAX_RESULTS:
                    break
        except Exception:
            logger.debug("Reinjection recall failed")

    # Track tool call for auto-checkpoint interval
    if _st._replay is not None:
        _st._replay.record_tool_call()

    # ── Build Response ─────────────────────────────────────────────────

    memory = storage.get_memory(memory_id)
    if memory is None:
        # Archive queue entry even on readback failure — DB write succeeded
        if _fq_path is not None:
            try:
                _get_file_queue().archive(Path(_fq_path))
            except Exception as _fq_exc:
                logger.debug("File queue archive failed (non-fatal): %s", _fq_exc)
        return {
            "stored": True,
            "id": memory_id,
            "curation_action": curation_action,
            "warning": "memory written but not found on readback",
        }
    # Strip binary fields from response (not JSON-serializable)
    memory.pop("embedding", None)

    # Publish visualization event
    _push_event(
        {
            "event": "memory_added",
            "node": {
                "id": f"mem:{memory_id}",
                "heat": memory.get("heat", initial_heat),
                "content": content[:200],
                "tags": tags,
                "directory": context,
            },
        }
    )

    memory.setdefault("file_hash", None)
    # Stamp CRDT vector_clock on newly created memories.
    # provenance_agent is set by the caller via the explicit arg (defaults to "default").
    # CRDT_AGENT_ID is used only for vector_clock tracking — it no longer overwrites
    # provenance_agent so that per-call provenance is preserved (v5.3.0 A1).
    if curation_action == "created":
        _crdt_agent = settings.CRDT_AGENT_ID
        _clock = json.dumps({_crdt_agent: 1})
        storage._q(
            "UPDATE type::record('memory', $id) SET provenance_agent = $a, vector_clock = $c",
            {"id": memory_id, "a": _provenance_agent, "c": _clock},
        )
        memory["provenance_agent"] = _provenance_agent
        memory["vector_clock"] = _clock
    memory["curation_action"] = curation_action
    if gate_result is not None:
        memory["surprisal"] = gate_result["surprisal"]
        memory["gate_reason"] = gate_result["gate_reason"]
    if triggered_memories:
        memory["triggered_prospective_memories"] = [
            {"id": pm["id"], "content": pm["content"]} for pm in triggered_memories
        ]
    if engram_result is not None:
        memory["engram_slot"] = engram_result["slot_index"]
        memory["temporal_links"] = engram_result["temporally_linked"]
        memory["temporal_link_count"] = engram_result["link_count"]
    if auto_protected:
        memory["auto_protected"] = True
        memory["protection_reason"] = "decision_detected"
    if related_context:
        memory["related_context"] = related_context

    # Archive the queue entry — DB write confirmed
    if _fq_path is not None:
        try:
            _get_file_queue().archive(Path(_fq_path))
        except Exception as _fq_exc:
            logger.debug("File queue archive failed (non-fatal): %s", _fq_exc)

    # P11: record stored outcome
    try:
        from yadgar.metrics import yadgar_writegate_outcome  # noqa: PLC0415

        yadgar_writegate_outcome.labels(outcome="stored").inc()
    except Exception:
        pass

    return memory


@_tool()
def remember(
    content: str,
    context: str,
    tags: list[str],
    is_protected: bool = False,
) -> dict:
    """Renamed to memorize. Update your MCP config or CLAUDE.md."""
    return {
        "stored": False,
        "reason": "Tool renamed to memorize — call memorize() instead",
    }
