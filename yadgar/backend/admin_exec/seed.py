"""Backend execution body for the seed_store admin op (T2 Car E1).

Census verdict #9 (layer-boundary train): seed_project's STORE phase runs
backend-side. Core keeps the host-FS half — ``scan_project`` +
``generate_memories`` + the ``_project_init`` draft — and forwards one
``seed_store`` op carrying the generated memory dicts. This impl owns:

- embedding + thermodynamic scoring (the backend has the ML engines),
- ``insert_memory`` / ``update_memory_scores``,
- old ``_seed`` row deletion (insert-first, §6 Q17 crash-safety order),
- the ``_project_init`` upsert.
"""

from __future__ import annotations

import logging

from yadgar._shared.observability.observe import observe
from yadgar._shared.runtime.lifecycle import _get_embeddings, _get_storage, _get_thermo

logger = logging.getLogger(__name__)


@observe(tier="stage", metric="backend.admin.seed_store_one")
def _store_one(storage, embeddings, thermo, mem: dict) -> int:
    """Embed + score + insert one seed memory. Returns the new memory id."""
    content = mem["content"]
    context = mem["context"]
    tags = mem["tags"]
    base_heat = float(mem.get("base_heat", 0.6))

    embedding = embeddings.encode(content)

    surprise = thermo.compute_surprise(content, context)
    importance = thermo.compute_importance(content, tags)
    valence = thermo.compute_valence(content)
    # Use modest surprise boost so seeded memories don't all max out.
    initial_heat = min(base_heat + surprise * 0.1, 1.0)

    memory_id = storage.insert_memory(
        {
            "content": content,
            "embedding": embedding,
            "tags": tags,
            "directory_context": context,
            "heat": initial_heat,
            "is_stale": False,
            "file_hash": None,
            "embedding_model": embeddings.get_model_name(),
        }
    )
    storage.update_memory_scores(
        memory_id,
        surprise_score=surprise,
        importance=importance,
        emotional_valence=valence,
    )
    return memory_id


@observe(tier="stage", metric="backend.admin.seed_delete_existing")
def _delete_existing_seed_memories(
    storage, directory: str, exclude_ids: list[int] | None = None
) -> int:
    """Delete existing _seed tagged memories for this directory before re-seeding.

    §6 Q17: exclude_ids lets callers preserve newly-inserted memories so the
    delete step only removes OLD seed memories, not the fresh ones.

    Returns count of deleted memories.
    """
    rows = storage._q(
        "SELECT id FROM memory WHERE directory_context = $dir AND '_seed' IN tags",
        {"dir": directory},
    )
    if not rows:
        return 0

    exclude_set: set[int] = set(exclude_ids or [])
    ids = [
        storage._extract_id(r.get("id"))
        for r in rows
        if storage._extract_id(r.get("id")) not in exclude_set
    ]
    for mid in ids:
        # Delete SR transitions referencing this memory
        storage._q(
            "DELETE memory_transition WHERE from_memory_id = $id OR to_memory_id = $id",
            {"id": mid},
        )
        # Delete the memory itself (embedding fields are on the record — no separate table)
        storage._q("DELETE type::record('memory', $id)", {"id": mid})

    return len(ids)


@observe(tier="boundary", metric="backend.admin.seed_store")
def seed_store(payload: dict) -> dict:
    """Store one generated seed batch. Storage-write half of seed_project.

    payload: {
        "root": str,                    # resolved project root (scan_data["root"])
        "memories": [{"content", "context", "tags", "base_heat"}, ...],
        "init_content": str,            # drafted _project_init markdown ("" = skip)
    }
    Returns {"created": int, "replaced": int}.
    """
    root = payload["root"]
    memories = payload.get("memories") or []
    init_content = payload.get("init_content") or ""

    storage = _get_storage()
    embeddings = _get_embeddings()
    thermo = _get_thermo()

    # §6 Q17: build new memories FIRST; delete old ones only after successful
    # insert — a crash mid-insert must not leave the DB with no seed memories.
    new_memory_ids: list[int] = []
    for mem in memories:
        new_memory_ids.append(_store_one(storage, embeddings, thermo, mem))
        logger.info("Seed memory [created]: %s", mem["content"][:80])

    replaced = _delete_existing_seed_memories(storage, root, exclude_ids=new_memory_ids)
    if replaced:
        logger.info("Cleared %d old seed memories for %s", replaced, root)

    if init_content:
        # §23: starter _project_init from README + top-level docs (drafted core-side).
        try:
            storage.upsert_project_init(root, init_content)
            logger.info("Drafted _project_init for %s", root)
        except Exception:  # noqa: BLE001 — init draft failure must not fail the seed
            logger.warning("Failed to draft _project_init for %s", root, exc_info=True)

    return {"created": len(new_memory_ids), "replaced": replaced}
