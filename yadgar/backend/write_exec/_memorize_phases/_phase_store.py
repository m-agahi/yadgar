"""Phase 5 — store: curator/direct insert + CLS + file hash + buffer capture."""

from __future__ import annotations

import logging

import yadgar._shared.runtime.lifecycle as _lifecycle
import yadgar._shared.runtime.state as _st
from yadgar._shared.observability.observe import observe
from yadgar._shared.observability.tracing import trace_span
from yadgar._shared.server_helpers import _file_hash
from yadgar._shared.write_exec import MemorizeContext
from yadgar.backend.curation import CurateParams

logger = logging.getLogger(__name__)


@trace_span()
def phase_store(ctx: MemorizeContext) -> None:
    """Write memory to storage via curator or direct insert.

    Mutations on ctx:
    - memory_id set
    - curation_action set
    """
    storage = _lifecycle._get_storage()
    embeddings = _lifecycle._get_embeddings()
    buffer = _lifecycle._get_buffer()
    fhash = _file_hash(ctx.context)

    curator = _st._curator
    if curator is not None and ctx.embedding is not None:
        _store_via_curator(ctx, storage, embeddings, fhash)
    else:
        _store_direct(ctx, storage, embeddings, fhash)

    # Shadow gate stamp (v5.73.0) — overwrite surprise_score with the GATE's surprisal
    # (distinct from ctx.surprise which is the thermo heat-boost score) + stamp would_reject.
    # Both fields are None when the gate is disabled; skip the update to avoid a no-op write.
    if ctx.gate_surprisal is not None or ctx.would_reject is not None:
        shadow_fields: dict = {}
        if ctx.gate_surprisal is not None:
            shadow_fields["surprise_score"] = ctx.gate_surprisal
        if ctx.would_reject is not None:
            shadow_fields["would_reject"] = ctx.would_reject
        storage.update_memory_fields(ctx.memory_id, **shadow_fields)

    # CLS dual-store: classify memory as episodic or semantic
    if _st._consolidation is not None and _st._consolidation.cls is not None:
        store_type = _st._consolidation.cls.classify_memory(ctx.content, ctx.tags, ctx.context)
        storage.update_memory_fields(ctx.memory_id, store_type=store_type)

    # Register file hash so staleness detector can find the filepath later
    if fhash is not None:
        storage.upsert_file_hash(ctx.context, fhash)

    # Capture in sensory buffer
    buffer.capture(ctx.content, ctx.context)

    # Record activity on consolidation engine
    if _st._consolidation is not None:
        _st._consolidation.record_activity()

    # Assign to astrocyte processes for domain-aware consolidation
    if _st._pool is not None:
        mem_data = storage.get_memory(ctx.memory_id)
        if mem_data:
            _st._pool.assign_memory(mem_data)


@observe(tier="stage")
def _store_via_curator(ctx: MemorizeContext, storage, embeddings, fhash: str | None) -> None:
    """Use curator for intelligent ingestion (merge/link/create)."""
    curator = _st._curator
    result = curator.curate_on_remember(
        ctx.content,
        ctx.context,
        ctx.tags,
        ctx.embedding,
        params=CurateParams(
            initial_heat=ctx.initial_heat,
            surprise=ctx.surprise,
            importance=ctx.importance,
            valence=ctx.valence,
            file_hash=fhash,
            embedding_model=embeddings.get_model_name(),
            contextual_prefix=ctx.contextual_prefix,
        ),
    )
    memory_id = result["memory_id"]
    ctx.curation_action = result["action"]

    # Race: candidate was deleted between search and merge — fall back to direct insert
    if memory_id is None:
        memory_id = _direct_insert(ctx, storage, embeddings, fhash)
        ctx.curation_action = "created"
    else:
        # Curator inserted or merged — backfill the v5.8 fields
        update_kw: dict = {}
        if ctx.tier is not None:
            update_kw["tier"] = ctx.tier
        if ctx.computed_valid_until is not None:
            update_kw["valid_until"] = ctx.computed_valid_until
        if update_kw:
            storage.update_memory_fields(memory_id, **update_kw)

    ctx.memory_id = memory_id


@observe(tier="stage")
def _store_direct(ctx: MemorizeContext, storage, embeddings, fhash: str | None) -> None:
    """Fallback: direct insert (no curator or no embedding)."""
    memory_id = _direct_insert(ctx, storage, embeddings, fhash)
    if ctx.contextual_prefix:
        storage.update_memory_fields(memory_id, contextual_prefix=ctx.contextual_prefix)
    storage.update_memory_scores(
        memory_id,
        surprise_score=ctx.surprise,
        importance=ctx.importance,
        emotional_valence=ctx.valence,
    )
    ctx.memory_id = memory_id
    ctx.curation_action = "created"


def _direct_insert(ctx: MemorizeContext, storage, embeddings, fhash: str | None) -> int:
    """Insert memory directly into storage and return memory_id."""
    from yadgar._shared.config import get_settings

    return storage.insert_memory(
        {
            "content": ctx.content,
            "embedding": ctx.embedding,
            "tags": ctx.tags,
            "directory_context": ctx.context,
            "heat": ctx.initial_heat,
            "is_stale": False,
            "file_hash": fhash,
            "embedding_model": embeddings.get_model_name(),
            "provenance_agent": ctx.provenance_agent_resolved,
            "tier": ctx.tier,
            "valid_until": ctx.computed_valid_until,
        },
        embeddings_engine=embeddings,
        settings=get_settings(),
    )
