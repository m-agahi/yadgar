"""Strengthen, reweight, and derive passes for the memify self-improvement cycle."""

import logging

from yadgar.embeddings import EmbeddingEngine
from yadgar.storage import StorageEngine
from yadgar.storage.directory import dominant_directory

logger = logging.getLogger(__name__)


def _memify_strengthen(
    storage: StorageEngine,
    stats: dict,
) -> None:
    """Boost importance for memories accessed > 5 times with confidence > 0.8."""
    candidates = storage.get_memories_by_heat(min_heat=0.0, limit=10000)
    to_update: list[tuple[int, float]] = []
    for mem in candidates:
        if (
            (mem.get("access_count") or 0) > 5
            and (mem.get("confidence") or 0.0) > 0.8
            and (mem.get("importance") or 0.0) < 1.0
        ):
            current_importance = mem.get("importance") or 0.5
            new_importance = min(current_importance + 0.1, 1.0)
            to_update.append((mem["id"], new_importance))
            stats["strengthened"] += 1

    if to_update:
        batch = [
            (
                "UPDATE type::record('memory', $id) SET importance = $importance",
                {"id": mid, "importance": new_imp},
            )
            for mid, new_imp in to_update
        ]
        storage.batch_writes(batch)


def _memify_reweight(
    storage: StorageEngine,
    stats: dict,
) -> None:
    """Adjust relationship weights based on usage patterns.

    Relationships between frequently co-retrieved memories get weight boost.
    Relationships between rarely-used entities get weight decay.
    """
    entities = storage.get_all_entities(min_heat=0.0, include_archived=True)
    entity_heat = {e["id"]: (e.get("heat") or 0.0) for e in entities}

    # ONE query for all relationships instead of O(N²) per-pair HTTP calls.
    relationships = storage.get_all_relationships()

    pending: list[tuple[int, float]] = []  # (rel_id, delta)
    for rel in relationships:
        sid = rel.get("source_entity_id")
        tid = rel.get("target_entity_id")
        if sid is None or tid is None:
            continue

        weight = rel.get("weight") or 1.0
        avg_heat = (entity_heat.get(sid, 0.0) + entity_heat.get(tid, 0.0)) / 2.0

        if avg_heat > 0.7 and weight >= 5.0:
            # Both entities are hot AND relationship is established -> boost
            pending.append((rel["id"], 0.5))
            stats["reweighted"] += 1
        elif avg_heat < 0.1:
            # Both entities are cold -> decay relationship
            new_weight = max(weight * 0.9, 0.1)
            delta = new_weight - weight
            if abs(delta) > 1e-9:
                pending.append((rel["id"], delta))
                stats["reweighted"] += 1

    if pending:
        now = storage._now_iso()
        batch = [
            (
                "UPDATE type::record('relationship', $id) SET "
                "weight = weight + $inc, last_reinforced = $now",
                {"id": rel_id, "inc": delta, "now": now},
            )
            for rel_id, delta in pending
        ]
        storage.batch_writes(batch)


# Tags marking a memory as machine-derived — excluded from directory votes so
# derived memories never reinforce their own (possibly wrong) directory_context.
_DERIVED_TAGS = frozenset({"derived", "auto-generated"})


def _derive_pair_directory(src_name: str, tgt_name: str, source_mems: list[dict]) -> str:
    """Originating directory for a co-occurrence pair.

    Vote with the directory_context of every source memory whose content mentions
    either entity name. dominant_directory() returns the single real dir when
    unambiguous, else "global" (cross-project / unknown).
    """
    dir_votes = [
        m.get("directory_context")
        for m in source_mems
        if src_name in (m.get("content") or "") or tgt_name in (m.get("content") or "")
    ]
    return dominant_directory(dir_votes)


def _collect_derive_inserts(
    storage: StorageEngine,
    embeddings: EmbeddingEngine,
    stats: dict,
    entity_map: dict,
    existing_contents: set[str],
    source_mems: list[dict],
) -> list[dict]:
    """Phase 1: walk co_occurrence relationships, build insert payloads.

    ONE query for co_occurrence relationships instead of O(N²) per-pair HTTP calls.
    """
    relationships = storage.get_relationships_by_types(["co_occurrence"])
    to_insert: list[dict] = []
    for rel in relationships:
        sid = rel.get("source_entity_id")
        tid = rel.get("target_entity_id")
        if sid is None or tid is None:
            continue

        # Check if co-occurrence count is high enough (weight as proxy)
        if (rel.get("weight") or 0.0) < 10.0:
            continue

        src_entity = entity_map.get(sid)
        tgt_entity = entity_map.get(tid)
        if src_entity is None or tgt_entity is None:
            continue

        src_name = src_entity["name"]
        tgt_name = tgt_entity["name"]

        # Check if we already derived a fact for this pair
        derived_content = f"{src_name} and {tgt_name} are frequently modified together"
        if derived_content in existing_contents:
            continue

        to_insert.append(
            {
                "memory_id": storage._next_id("memory"),
                "content": derived_content,
                "embedding": embeddings.encode(derived_content),
                "src_name": src_name,
                "tgt_name": tgt_name,
                "directory_context": _derive_pair_directory(src_name, tgt_name, source_mems),
            }
        )
        existing_contents.add(derived_content)
        stats["derived"] += 1

    return to_insert


def _memify_derive(
    storage: StorageEngine,
    embeddings: EmbeddingEngine,
    stats: dict,
) -> None:
    """Generate synthetic derived-fact memories for high-weight entity pairs."""
    entities = storage.get_all_entities(min_heat=0.0, include_archived=True)
    entity_map = {e["id"]: e for e in entities}

    # Pre-build existing content set for dedup + full dicts for directory derivation.
    all_mems = storage.get_all_memories_with_embeddings()
    existing_contents = {m["content"] for m in all_mems}
    source_mems = [m for m in all_mems if not _DERIVED_TAGS.intersection(set(m.get("tags") or []))]

    now = storage._now_iso()
    model_name = embeddings.get_model_name()

    to_insert = _collect_derive_inserts(
        storage, embeddings, stats, entity_map, existing_contents, source_mems
    )
    if not to_insert:
        return

    # Phase 2: batch all CREATEs and score UPDATEs into one transaction
    batch: list[tuple[str, dict | None]] = []
    for item in to_insert:
        mid = item["memory_id"]
        emb = item["embedding"]
        emb_floats = storage._bytes_to_floats(emb) if emb else None
        batch.append(
            (
                "CREATE type::record('memory', $id) SET "
                "content = $content, embedding = $embedding, tags = $tags, "
                "source_episode_id = $source_episode_id, "
                "directory_context = $directory_context, "
                "created_at = $created_at, last_accessed = $last_accessed, "
                "heat = $heat, is_stale = $is_stale, file_hash = $file_hash, "
                "embedding_model = $embedding_model, "
                "plasticity = $plasticity, stability = $stability, "
                "excitability = $excitability, store_type = $store_type, "
                "compression_level = $compression_level, sr_x = $sr_x, sr_y = $sr_y, "
                "reconsolidation_count = $reconsolidation_count, "
                "provenance_agent = $provenance_agent, vector_clock = $vector_clock, "
                "is_protected = $is_protected",
                {
                    "id": mid,
                    "content": item["content"],
                    "embedding": emb_floats,
                    "tags": ["derived", "auto-generated"],
                    "source_episode_id": None,
                    "directory_context": item["directory_context"],
                    "created_at": now,
                    "last_accessed": now,
                    "heat": 0.5,
                    "is_stale": False,
                    "file_hash": None,
                    "embedding_model": model_name,
                    "plasticity": 1.0,
                    "stability": 0.0,
                    "excitability": 1.0,
                    "store_type": "episodic",
                    "compression_level": 0,
                    "sr_x": 0.0,
                    "sr_y": 0.0,
                    "reconsolidation_count": 0,
                    "provenance_agent": "default",
                    "vector_clock": "{}",
                    "is_protected": False,
                },
            )
        )
        batch.append(
            (
                "UPDATE type::record('memory', $id) SET "
                "importance = $importance, surprise_score = $surprise_score, "
                "emotional_valence = $emotional_valence",
                {
                    "id": mid,
                    "importance": 0.6,
                    "surprise_score": 0.0,
                    "emotional_valence": 0.0,
                },
            )
        )
    storage.batch_writes(batch)
