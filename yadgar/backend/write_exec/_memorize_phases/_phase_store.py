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
    # C10 (f) (0047 PR#40 §5): ``ctx.context`` is now an OPTIONAL REAL PATH and
    # this is one of its only two surviving consumers (the other is the
    # ``upsert_file_hash`` registration below) — carve-out 3. Absent path → no
    # hash attempt, which is the same best-effort outcome ``_file_hash`` already
    # produced for a directory or a prose string.
    fhash = _file_hash(ctx.context) if ctx.context else None

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
        # C10 (f): ``classify_memory``'s third parameter is named ``project_id``
        # (C9b renamed it); it was still being handed ``ctx.context``. Feeding a
        # parameter one value while it is named for another is how the two keys
        # stayed indistinguishable — pass the scope key it asks for.
        store_type = _st._consolidation.cls.classify_memory(ctx.content, ctx.tags, ctx.project_id)
        storage.update_memory_fields(ctx.memory_id, store_type=store_type)

    # Register file hash so staleness detector can find the filepath later.
    # ``fhash`` is non-None only when ``ctx.context`` was a readable file, so
    # the path handed to the registration is always a real one (carve-out 3).
    if fhash is not None:
        storage.upsert_file_hash(ctx.context, fhash)

    # Capture in sensory buffer. C10 (f): the buffer's ``directory`` feeds the
    # ``episode`` table, which does NOT get a ``project_id`` column until C11 —
    # so this deliberately keeps passing the path and does NOT substitute the
    # project_id, which would mint a row the path-keyed readers cannot find.
    buffer.capture(ctx.content, ctx.context or "")

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
        # C10 (f): still forwarded as the path hint, but it is NO LONGER what
        # the row is stamped with — ``insert_new_memory`` now takes the
        # ``directory_context`` from ``spec.project_id``. This arm is the
        # PRODUCTION arm, so a stamp fix that reached only ``_direct_insert``
        # would go green in a curator-less harness and stay broken live.
        ctx.context or "",
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
            # C4b (0047 PR#40 §5): the curator branch is the PRODUCTION branch
            # — ``phase_store`` prefers it whenever a curator and an embedding
            # are both present. Stamping only ``_direct_insert`` below would
            # leave the live path re-deriving inside a container that cannot
            # (ADR-0227 §1.1). The merge arm needs nothing: it UPDATEs a row
            # whose project_id was stamped by whoever inserted it.
            project_id=ctx.project_id,
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
            # C10 (f) (0047 PR#40 §5): THE STAMP. It comes from the resolved
            # ``project_id``, never from ``context``. ``context`` named a
            # directory that no ``directory`` grep in the survey could reach,
            # and the corpus shows callers filling it with prose (18 distinct
            # non-path values live). Scope is now carried by exactly one key.
            "directory_context": ctx.project_id,
            "heat": ctx.initial_heat,
            "is_stale": False,
            "file_hash": fhash,
            "embedding_model": embeddings.get_model_name(),
            "provenance_agent": ctx.provenance_agent_resolved,
            "tier": ctx.tier,
            "valid_until": ctx.computed_valid_until,
            # C4b (0047 PR#40 §5): the enqueue-time stamp reaches
            # ``insert_memory`` as ``_resolve_project_id_for_write``'s
            # ``caller_value``, so a stamped write never touches the classifier.
            "project_id": ctx.project_id,
        },
        embeddings_engine=embeddings,
        settings=get_settings(),
    )
