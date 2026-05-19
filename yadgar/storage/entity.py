"""Entity and relationship storage mixin."""

import logging

_log = logging.getLogger(__name__)


class _EntityMixin:
    """Entity + relationship CRUD — mixed into StorageEngine."""

    # ------------------------------------------------------------------ Entities

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

    # ------------------------------------------------------------------ Relationships

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
        self, entity_id: int, rel_types: list[str] | None = None
    ) -> list[dict]:
        """Return all relationships where entity_id is source or target, with entity names."""
        if rel_types:
            rows = self._q(
                "SELECT * FROM relationship WHERE "
                "(source_entity_id = $eid OR target_entity_id = $eid) "
                "AND relationship_type IN $types",
                {"eid": entity_id, "types": rel_types},
            )
        else:
            rows = self._q(
                "SELECT * FROM relationship WHERE "
                "source_entity_id = $eid OR target_entity_id = $eid",
                {"eid": entity_id},
            )
        results = self._rows_to_dicts(rows)
        # Enrich with entity names via lookup
        for d in results:
            src_id = int(d.get("source_entity_id", 0))
            tgt_id = int(d.get("target_entity_id", 0))
            src_rows = self._q(f"SELECT name FROM entity:{src_id}") if src_id else []
            tgt_rows = self._q(f"SELECT name FROM entity:{tgt_id}") if tgt_id else []
            d["source_name"] = src_rows[0]["name"] if src_rows else None
            d["target_name"] = tgt_rows[0]["name"] if tgt_rows else None
        return results

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

    def insert_typed_relationship(
        self,
        source_entity_id: int,
        target_entity_id: int,
        relationship_type: str,
        weight: float = 1.0,
        event_time: str | None = None,
        record_time: str | None = None,
        is_causal: int = 0,
        confidence: float = 1.0,
        source_memory_id: int | None = None,
    ) -> int:
        """Insert a relationship with bi-temporal and causal metadata.

        source_memory_id (C3): optional citation — the memory that triggered this
        entity-entity link. Omitted from SET when None (SurrealDB option<int>
        rejects explicit NULL on a DEFINE FIELD column).
        """
        now = self._now_iso()
        rid = self._next_id("relationship")
        # C1: bi-temporal validity. valid_from defaults to now().
        params: dict = {
            "id": rid,
            "src": source_entity_id,
            "tgt": target_entity_id,
            "rt": relationship_type,
            "w": weight,
            "cat": record_time or now,
            "lr": record_time or now,
            "et": event_time or now,
            "rct": record_time or now,
            "ic": bool(is_causal),
            "conf": confidence,
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
        if source_memory_id is not None:
            sql += ", source_memory_id = $smid"
            params["smid"] = source_memory_id
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
