"""Entity and relationship storage mixin."""

import logging
from dataclasses import dataclass

from yadgar.tracing import trace_span

_log = logging.getLogger(__name__)


@dataclass
class RelationshipMeta:
    """Optional metadata for insert_typed_relationship.

    Bundles the 6 optional bi-temporal + causal params so the public
    signature stays within the I13 param cap (≤8 non-self args).
    """

    weight: float = 1.0
    event_time: str | None = None
    record_time: str | None = None
    is_causal: int = 0
    confidence: float = 1.0
    source_memory_id: int | None = None


class _EntityMixin:
    """Entity + relationship CRUD — mixed into StorageEngine."""

    # ------------------------------------------------------------------ Entities

    @trace_span("storage.entity.insert_entity")
    def insert_entity(self, entity: dict) -> int:
        now = self._now_iso()
        eid = self._next_id("entity")
        self._q(
            "CREATE type::record('entity', $id) SET "
            "name = $name, type = $type, created_at = $created_at, "
            "last_accessed = $last_accessed, heat = $heat, archived = $archived",
            {
                "id": eid,
                "name": entity["name"],
                "type": entity["type"],
                "created_at": entity.get("created_at", now),
                "last_accessed": entity.get("last_accessed", now),
                "heat": entity.get("heat", 1.0),
                "archived": bool(entity.get("archived", False)),
            },
        )
        return eid

    @trace_span("storage.entity.get_entity_by_name")
    def get_entity_by_name(self, name: str) -> dict | None:
        rows = self._q(
            "SELECT * FROM entity WHERE name = $name LIMIT 1",
            {"name": name},
        )
        return self._row_to_dict(rows[0]) if rows else None

    def get_all_entities(self, min_heat: float = 0.0, include_archived: bool = False) -> list[dict]:
        if include_archived:
            rows = self._q(
                "SELECT * FROM entity WHERE heat >= $min ORDER BY heat DESC",
                {"min": min_heat},
            )
        else:
            rows = self._q(
                "SELECT * FROM entity WHERE heat >= $min AND archived = false ORDER BY heat DESC",
                {"min": min_heat},
            )
        return self._rows_to_dicts(rows)

    def update_entity_heat(self, entity_id: int, new_heat: float):
        self._q(
            "UPDATE type::record('entity', $id) SET heat = $heat",
            {"id": entity_id, "heat": new_heat},
        )

    def get_all_entities_for_decay(self) -> list[dict]:
        rows = self._q("SELECT * FROM entity WHERE archived = false")
        return self._rows_to_dicts(rows)

    def archive_entity(self, entity_id: int):
        self._q(
            "UPDATE type::record('entity', $id) SET archived = true",
            {"id": entity_id},
        )

    def reinforce_entity(self, entity_id: int, heat_bump: float = 0.1):
        self._q(
            "UPDATE type::record('entity', $id) SET "
            "heat = math::min([heat + $bump, 1.0]), last_accessed = $now",
            {"id": entity_id, "bump": heat_bump, "now": self._now_iso()},
        )

    def get_entity_by_id(self, entity_id: int) -> dict | None:
        """Fetch a single entity row by its integer ID."""
        eid = int(entity_id)
        rows = self._q(f"SELECT * FROM entity:{eid}")
        return self._row_to_dict(rows[0]) if rows else None

    def get_entities_by_ids(self, entity_ids: list[int]) -> dict[int, dict]:
        """Bulk-fetch entity rows for a list of ids in ONE query (v5.102.0).

        Batched counterpart of ``get_entity_by_id`` — collapses the spreading-
        activation per-entity N+1 (one ``SELECT * FROM entity:{id}`` per activated
        entity) into a single ``WHERE id IN [...]`` round-trip. Returns a
        ``{entity_id: row}`` map so callers keep exact per-entity attribution.

        Record ids are inlined into the IN list (``WHERE id IN [entity:N, ...]``)
        rather than bound as a ``$param`` — parameterised IN with record-ids is not
        portable to the embedded SurrealKV SDK (mirrors ``get_memories_by_ids``).
        ``int()`` sanitises each id so the inlined literal can never carry injection.
        Missing ids are simply absent from the map (``get_entity_by_id`` would return
        None). Duplicate input ids collapse to one entry.
        """
        if not entity_ids:
            return {}
        unique_ids = list(dict.fromkeys(int(e) for e in entity_ids))
        id_list = ", ".join(f"entity:{eid}" for eid in unique_ids)
        rows = self._q(f"SELECT * FROM entity WHERE id IN [{id_list}]")
        out: dict[int, dict] = {}
        for r in rows:
            d = self._row_to_dict(r)
            out[int(d["id"])] = d
        return out

    # ------------------------------------------------------------------ Relationships

    @trace_span("storage.entity.insert_relationship")
    def insert_relationship(self, relationship: dict) -> int:
        now = self._now_iso()
        rid = self._next_id("relationship")
        # C1: bi-temporal validity — valid_from = now(), valid_until = NULL.
        self._q(
            "CREATE type::record('relationship', $id) SET "
            "source_entity_id = $src, target_entity_id = $tgt, "
            "relationship_type = $rtype, weight = $weight, "
            "created_at = $created_at, last_reinforced = $last_reinforced, "
            "valid_from = $vf",
            {
                "id": rid,
                "src": relationship["source_entity_id"],
                "tgt": relationship["target_entity_id"],
                "rtype": relationship["relationship_type"],
                "weight": relationship.get("weight", 1.0),
                "created_at": relationship.get("created_at", now),
                "last_reinforced": relationship.get("last_reinforced", now),
                "vf": relationship.get("valid_from", now),
            },
        )
        return rid

    @trace_span("storage.entity.get_relationship_between")
    def get_relationship_between(self, source_id: int, target_id: int) -> dict | None:
        rows = self._q(
            "SELECT * FROM relationship WHERE "
            "(source_entity_id = $src AND target_entity_id = $tgt) OR "
            "(source_entity_id = $tgt AND target_entity_id = $src) LIMIT 1",
            {"src": source_id, "tgt": target_id},
        )
        return self._row_to_dict(rows[0]) if rows else None

    def get_all_relationships(self) -> list[dict]:
        """Return all relationships in the store.

        Used by memify_reweight to avoid the O(N²) per-pair HTTP pattern.
        One HTTP request instead of N(N-1)/2.
        """
        rows = self._q("SELECT * FROM relationship")
        return self._rows_to_dicts(rows)

    def get_relationships_by_types(self, rel_types: list[str]) -> list[dict]:
        """Return all relationships whose relationship_type is in rel_types.

        Used by memify_derive to avoid the O(N²) per-pair HTTP pattern.
        One HTTP request instead of N(N-1)/2.
        """
        rows = self._q(
            "SELECT * FROM relationship WHERE relationship_type IN $types",
            {"types": rel_types},
        )
        return self._rows_to_dicts(rows)

    def get_relationships_among_entities(self, entity_ids: list[int]) -> list[dict]:
        """Return all relationships where both endpoints are in entity_ids.

        Used to avoid O(N²) per-pair HTTP calls when processing a bounded entity set.
        One HTTP request instead of N(N-1)/2.
        """
        rows = self._q(
            "SELECT * FROM relationship WHERE source_entity_id IN $ids AND target_entity_id IN $ids",
            {"ids": entity_ids},
        )
        return self._rows_to_dicts(rows)

    def get_typed_relationship(self, source_id: int, target_id: int, rel_type: str) -> dict | None:
        """Return a relationship between two entities of a specific type (directional)."""
        rows = self._q(
            "SELECT * FROM relationship WHERE "
            "source_entity_id = $src AND target_entity_id = $tgt "
            "AND relationship_type = $rt LIMIT 1",
            {"src": source_id, "tgt": target_id, "rt": rel_type},
        )
        return self._row_to_dict(rows[0]) if rows else None

    def get_relationships_for_entity(
        self, entity_id: int, rel_types: list[str] | None = None, with_names: bool = True
    ) -> list[dict]:
        """Return all relationships where entity_id is source or target.

        When ``with_names`` is True (default) each row is enriched with
        ``source_name`` / ``target_name`` via two extra per-row lookups — needed by
        display/viz callers. The graph-traversal hot path (PPR build + spreading BFS)
        passes ``with_names=False`` to skip those lookups (~2/3 of the round-trips);
        those consumers read only ``entity_id`` / ``weight``, never the names.
        """
        if rel_types:
            rows = self._q(
                "SELECT * FROM relationship WHERE "
                "(source_entity_id = $eid OR target_entity_id = $eid) "
                "AND relationship_type IN $types ORDER BY id",
                {"eid": entity_id, "types": rel_types},
            )
        else:
            rows = self._q(
                "SELECT * FROM relationship WHERE "
                "source_entity_id = $eid OR target_entity_id = $eid ORDER BY id",
                {"eid": entity_id},
            )
        results = self._rows_to_dicts(rows)
        if with_names:
            self._enrich_relationship_names(results)
        return results

    def _enrich_relationship_names(self, rows: list[dict]) -> None:
        """Add source_name / target_name to each relationship row (in place)."""
        for d in rows:
            src_id = int(d.get("source_entity_id", 0))
            tgt_id = int(d.get("target_entity_id", 0))
            src_rows = self._q(f"SELECT name FROM entity:{src_id}") if src_id else []
            tgt_rows = self._q(f"SELECT name FROM entity:{tgt_id}") if tgt_id else []
            d["source_name"] = src_rows[0]["name"] if src_rows else None
            d["target_name"] = tgt_rows[0]["name"] if tgt_rows else None

    def get_relationships_for_frontier(
        self, entity_ids: list[int], rel_types: list[str] | None = None
    ) -> list[dict]:
        """Return all relationships where EITHER endpoint is in ``entity_ids``.

        One query per BFS depth instead of one query per frontier node — the batched
        counterpart of ``get_relationships_for_entity`` (OR semantics, so frontier→
        outside edges are kept, unlike ``get_relationships_among_entities`` which
        requires both endpoints inside the set).

        Never enriches names — the only callers are the graph-traversal hot path.
        Rows are ordered by ``id`` so that grouping by endpoint reproduces the same
        per-entity ordering the per-node query returns (exact traversal parity).
        """
        if not entity_ids:
            return []
        if rel_types:
            rows = self._q(
                "SELECT * FROM relationship WHERE "
                "(source_entity_id IN $ids OR target_entity_id IN $ids) "
                "AND relationship_type IN $types ORDER BY id",
                {"ids": entity_ids, "types": rel_types},
            )
        else:
            rows = self._q(
                "SELECT * FROM relationship WHERE "
                "(source_entity_id IN $ids OR target_entity_id IN $ids) ORDER BY id",
                {"ids": entity_ids},
            )
        return self._rows_to_dicts(rows)

    def get_relationships_by_type_and_weight(
        self, rel_type: str, min_weight: float = 0.0
    ) -> list[dict]:
        """Return all relationships of a given type with weight >= min_weight."""
        rows = self._q(
            "SELECT * FROM relationship WHERE relationship_type = $rt AND weight >= $mw",
            {"rt": rel_type, "mw": min_weight},
        )
        return self._rows_to_dicts(rows)

    def update_relationship_fields(self, rel_id: int, **fields) -> None:
        """Update arbitrary columns on a relationship row."""
        from yadgar.storage.client import _RELATIONSHIP_UPDATABLE_FIELDS

        if not fields:
            return
        fields = {k: v for k, v in fields.items() if k in _RELATIONSHIP_UPDATABLE_FIELDS}
        if not fields:
            return
        sets = ", ".join(f"{k} = ${k}" for k in fields)
        params = dict(fields)
        params["id"] = rel_id
        self._q(f"UPDATE type::record('relationship', $id) SET {sets}", params)

    @trace_span("storage.entity.insert_typed_relationship")
    def insert_typed_relationship(
        self,
        source_entity_id: int,
        target_entity_id: int,
        relationship_type: str,
        meta: RelationshipMeta | None = None,
    ) -> int:
        """Insert a relationship with bi-temporal and causal metadata.

        meta.source_memory_id (C3): optional citation — the memory that triggered this
        entity-entity link. Omitted from SET when None (SurrealDB option<int>
        rejects explicit NULL on a DEFINE FIELD column).
        """
        m = meta or RelationshipMeta()
        return self._insert_typed_relationship_impl(
            source_entity_id, target_entity_id, relationship_type, m
        )

    def _insert_typed_relationship_impl(
        self,
        source_entity_id: int,
        target_entity_id: int,
        relationship_type: str,
        meta: RelationshipMeta,
    ) -> int:
        """Core insert — called with a resolved RelationshipMeta."""
        now = self._now_iso()
        rid = self._next_id("relationship")
        # C1: bi-temporal validity. valid_from defaults to now().
        params: dict = {
            "id": rid,
            "src": source_entity_id,
            "tgt": target_entity_id,
            "rt": relationship_type,
            "w": meta.weight,
            "cat": meta.record_time or now,
            "lr": meta.record_time or now,
            "et": meta.event_time or now,
            "rct": meta.record_time or now,
            "ic": bool(meta.is_causal),
            "conf": meta.confidence,
            "vf": now,
        }
        sql = (
            "CREATE type::record('relationship', $id) SET "
            "source_entity_id = $src, target_entity_id = $tgt, "
            "relationship_type = $rt, weight = $w, "
            "created_at = $cat, last_reinforced = $lr, "
            "event_time = $et, record_time = $rct, "
            "is_causal = $ic, confidence = $conf, "
            "valid_from = $vf"
        )
        if meta.source_memory_id is not None:
            sql += ", source_memory_id = $smid"
            params["smid"] = meta.source_memory_id
        self._q(sql, params)
        return rid

    def reinforce_relationship(self, rel_id: int, weight_increase: float = 1.0):
        self._q(
            "UPDATE type::record('relationship', $id) SET "
            "weight = weight + $inc, last_reinforced = $now",
            {"id": rel_id, "inc": weight_increase, "now": self._now_iso()},
        )

    def get_relationship_by_source_and_type(
        self, source_entity_id: int, relationship_type: str
    ) -> dict | None:
        rows = self._q(
            "SELECT * FROM relationship WHERE source_entity_id = $src "
            "AND relationship_type = $rt LIMIT 1",
            {"src": source_entity_id, "rt": relationship_type},
        )
        return self._row_to_dict(rows[0]) if rows else None
