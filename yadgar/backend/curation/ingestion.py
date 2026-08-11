"""Active curation on memory ingestion — merge, link, and create decisions."""

import json
import logging
from dataclasses import dataclass, field

from yadgar._shared.embeddings import EmbeddingEngine
from yadgar._shared.observability.observe import observe
from yadgar._shared.observability.tracing import trace_span
from yadgar._shared.storage import StorageEngine

logger = logging.getLogger(__name__)

# Moderate similarity range for linking
_LINK_LOW = 0.6
_LINK_HIGH = 0.85


@dataclass
class NewMemorySpec:
    """All inputs needed to create a new memory row.

    Bundles the 9 per-write params into one object so insert_new_memory
    stays within the I13 PLR0913 cap (≤8 non-self args per function).
    """

    tags: list[str] = field(default_factory=list)
    embedding: bytes = b""
    heat: float = 1.0
    file_hash: str | None = None
    embedding_model: str | None = None
    contextual_prefix: str | None = None
    surprise: float = 0.0
    importance: float = 0.5
    valence: float = 0.0
    # C4b (0047 PR#40 §5): enqueue-time project_id; reaches ``insert_memory``
    # as ``_resolve_project_id_for_write``'s ``caller_value``.
    project_id: str | None = None


@trace_span()
def find_similar_memories(
    storage: StorageEngine,
    embeddings: EmbeddingEngine,
    embedding: bytes,
    min_sim: float = 0.6,
) -> list[tuple[int, float]]:
    """Find existing memories above min_sim, sorted by descending similarity."""
    if embedding is None:
        return []

    vec_hits = storage.search_vectors(embedding, top_k=10, min_heat=0.0)
    results = []
    for mid, _distance in vec_hits:
        mem = storage.get_memory(mid)
        if mem and mem.get("embedding"):
            sim = embeddings.similarity(embedding, mem["embedding"])
            if sim >= min_sim:
                results.append((mid, sim))

    results.sort(key=lambda x: x[1], reverse=True)
    return results


@observe(tier="stage")
def has_textual_overlap(new_content: str, existing_content: str) -> bool:
    """Check if new content has meaningful textual overlap with existing.

    Prevents merging memories that are semantically similar (high embedding
    similarity) but clearly different pieces of information.
    """
    new_words = set(new_content.lower().split())
    existing_words = set(existing_content.lower().split())
    if not new_words or not existing_words:
        return False
    intersection = new_words & existing_words
    union = new_words | existing_words
    jaccard = len(intersection) / len(union) if union else 0.0
    return jaccard > 0.5


@observe(tier="stage")
def merge_memory(
    storage: StorageEngine,
    embeddings: EmbeddingEngine,
    existing_id: int,
    new_content: str,
    new_tags: list[str],
    new_embedding: bytes,
    contextual_prefix: str | None,
) -> dict:
    """Merge new content into an existing memory."""
    existing = storage.get_memory(existing_id)
    if existing is None:
        # Race: candidate was deleted between search and merge — signal caller to handle gracefully
        return {"action": "created", "memory_id": None}

    # Combine content
    merged_content = existing["content"] + "\n" + new_content

    # Union tags
    existing_tags = existing.get("tags", [])
    if isinstance(existing_tags, str):
        existing_tags = json.loads(existing_tags)
    merged_tags = list(set(existing_tags) | set(new_tags))

    # Re-embed merged content
    embed_text = f"{contextual_prefix}{merged_content}" if contextual_prefix else merged_content
    merged_embedding = embeddings.encode(embed_text)

    # Update memory in DB
    storage.update_memory_fields(
        existing_id,
        content=merged_content,
        tags=json.dumps(merged_tags),
        heat=1.0,
        last_accessed=storage._now_iso(),
    )

    # Update embedding in vec0
    if merged_embedding is not None:
        storage.update_memory_fields(existing_id, embedding=merged_embedding)
        try:
            storage.update_vector(existing_id, merged_embedding)
        except Exception:
            pass

    # Update FTS content is handled by the trigger on memories table

    logger.debug("Merged new content into memory %d", existing_id)
    return {"action": "merged", "memory_id": existing_id}


@observe(tier="stage")
def insert_new_memory(
    storage: StorageEngine,
    content: str,
    context: str,
    spec: NewMemorySpec | None = None,
    embeddings_engine=None,
    settings=None,
) -> int:
    """Insert a brand-new memory and set its scores.

    spec bundles tags, embedding, heat, file_hash, embedding_model,
    contextual_prefix, surprise, importance, and valence.

    embeddings_engine and settings are forwarded to storage.insert_memory so
    that the INDEX_ENRICHMENT_ENABLED pipeline runs when configured.

    ``context`` (C10 (f), 0047 PR#40 §5) is NO LONGER the scope key and no
    longer reaches the row: ``directory_context`` is stamped from
    ``spec.project_id``. The parameter is retained because it is positional in
    ``MemoryCurator.curate_on_remember``'s published signature — deleting it
    would churn call sites for no behavioural gain — and it still carries the
    caller's path hint. Anything that needs to know WHICH PROJECT this row
    belongs to must read ``spec.project_id``.
    """
    s = spec or NewMemorySpec()
    memory_id = storage.insert_memory(
        {
            "content": content,
            "embedding": s.embedding,
            "tags": s.tags,
            # C10 (f) (0047 PR#40 §5): THE CURATOR-ARM STAMP — the production
            # arm's half of the pair whose other half is ``_direct_insert``.
            # Both now read the same single scope key.
            "directory_context": s.project_id,
            "heat": s.heat,
            "is_stale": False,
            "file_hash": s.file_hash,
            "embedding_model": s.embedding_model,
            # C4b (0047 PR#40 §5): stamped independently of ``context`` —
            # ownership and reach are different facts (§1.4).
            "project_id": s.project_id,
        },
        embeddings_engine=embeddings_engine,
        settings=settings,
    )

    if s.contextual_prefix:
        storage.update_memory_fields(memory_id, contextual_prefix=s.contextual_prefix)

    storage.update_memory_scores(
        memory_id,
        surprise_score=s.surprise,
        importance=s.importance,
        emotional_valence=s.valence,
    )

    return memory_id


@observe(tier="stage")
def create_link(storage: StorageEngine, new_id: int, existing_id: int) -> None:
    """Create a derived_from relationship between two memories via entities."""
    storage._now_iso()
    # Use entity system: create ephemeral entity nodes for both memories
    # and link them with a derived_from relationship
    src_entity = storage.get_entity_by_name(f"memory:{new_id}")
    if src_entity is None:
        src_eid = storage.insert_entity({"name": f"memory:{new_id}", "type": "file"})
    else:
        src_eid = src_entity["id"]

    tgt_entity = storage.get_entity_by_name(f"memory:{existing_id}")
    if tgt_entity is None:
        tgt_eid = storage.insert_entity({"name": f"memory:{existing_id}", "type": "file"})
    else:
        tgt_eid = tgt_entity["id"]

    storage.insert_relationship(
        {
            "source_entity_id": src_eid,
            "target_entity_id": tgt_eid,
            "relationship_type": "derived_from",
        }
    )
    logger.debug("Linked memory %d -> derived_from -> memory %d", new_id, existing_id)
