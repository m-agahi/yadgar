"""Rich typed temporal knowledge graph for Yadgar."""

import logging
import re
from collections import deque
from datetime import UTC, datetime

from yadgar.config import Settings
from yadgar.storage import RelationshipMeta, StorageEngine

logger = logging.getLogger(__name__)

VALID_REL_TYPES = frozenset(
    {
        "co_occurrence",
        "imports",
        "calls",
        "debugged_with",
        "decided_to_use",
        "caused_by",
        "resolved_by",
        "preceded_by",
        "derived_from",
    }
)

# Patterns for typed entity extraction
_IMPORT_FULL_RE = re.compile(r"(?:^|\n)\s*import\s+([\w.]+)")
_FROM_IMPORT_RE = re.compile(r"(?:^|\n)\s*from\s+([\w.]+)\s+import\s+([\w, ]+)")
_DEF_RE = re.compile(r"\bdef\s+(\w+)\s*\(")
_CALL_RE = re.compile(r"\b(\w+)\s*\(")
_ERROR_FIX_RE = re.compile(
    r"(?:fix(?:ed)?|resolv(?:ed|e|ing)|solved?)\s+(?:the\s+)?(\w*(?:Error|Exception|error|bug|issue))",
    re.IGNORECASE,
)
_DECIDED_RE = re.compile(
    r"decided\s+to\s+use\s+(\w+(?:\s+\w+){0,2})\s+instead\s+of\s+(\w+(?:\s+\w+){0,2})",
    re.IGNORECASE,
)


class KnowledgeGraph:
    """Rich typed temporal knowledge graph with bi_temporal|event_time|record_time tracking.

    Implements a bi_temporal model where relationships track both
    event_time (when something happened) and record_time (when we
    learned about it), enabling point-in-time queries across both
    temporal dimensions.
    """

    def __init__(self, storage: StorageEngine, settings: Settings) -> None:
        self._storage = storage
        self._settings = settings

    # -- a. Typed Relationship Management --

    def add_relationship(
        self,
        source: str,
        target: str,
        rel_type: str,
        event_time: datetime | None = None,
        confidence: float = 1.0,
    ) -> int:
        if rel_type not in VALID_REL_TYPES:
            raise ValueError(f"Invalid rel_type: {rel_type}. Must be one of {VALID_REL_TYPES}")

        source_entity = self._ensure_entity(source)
        target_entity = self._ensure_entity(target)
        now = datetime.now(UTC).isoformat()
        event_time_iso = event_time.isoformat() if event_time else now

        existing = self._get_typed_relationship(source_entity["id"], target_entity["id"], rel_type)
        if existing:
            self._reinforce_typed_relationship(existing["id"], now)
            return existing["id"]

        return self._insert_typed_relationship(
            source_entity["id"],
            target_entity["id"],
            rel_type,
            event_time_iso,
            now,
            confidence,
        )

    # -- b. Bi-Temporal Queries --

    def get_relationships_at_time(self, entity_name: str, event_time: datetime) -> list[dict]:
        entity = self._storage.get_entity_by_name(entity_name)
        if not entity:
            return []
        eid = entity["id"]
        event_iso = event_time.isoformat()
        all_rels = self._storage.get_relationships_for_entity(eid)
        return [r for r in all_rels if r.get("event_time") and r["event_time"] <= event_iso]

    def get_relationship_history(self, source: str, target: str) -> list[dict]:
        source_entity = self._storage.get_entity_by_name(source)
        target_entity = self._storage.get_entity_by_name(target)
        if not source_entity or not target_entity:
            return []
        sid, tid = source_entity["id"], target_entity["id"]
        all_rels = self._storage.get_relationships_for_entity(sid)
        filtered = [
            r
            for r in all_rels
            if (r["source_entity_id"] == sid and r["target_entity_id"] == tid)
            or (r["source_entity_id"] == tid and r["target_entity_id"] == sid)
        ]
        return sorted(filtered, key=lambda r: r.get("created_at", ""))

    # -- c. Causal Edge Detection --

    def detect_causality(self) -> int:
        threshold = self._settings.CAUSAL_THRESHOLD
        created = 0

        entities = self._storage.get_all_entities(min_heat=0.0, include_archived=True)
        logger.info("detect_causality: %d entities, threshold=%.2f", len(entities), threshold)
        entity_map = {e["id"]: e for e in entities}

        co_rels = self._storage.get_relationships_by_type_and_weight(
            "co_occurrence", min_weight=threshold
        )

        for rel in co_rels:
            sid, tid = rel["source_entity_id"], rel["target_entity_id"]
            if sid not in entity_map or tid not in entity_map:
                continue
            src_name = entity_map[sid]["name"]
            tgt_name = entity_map[tid]["name"]

            order = self._check_temporal_order(src_name, tgt_name)
            if order is None:
                continue

            if order == "before":
                causal_src, causal_tgt = src_name, tgt_name
            else:
                causal_src, causal_tgt = tgt_name, src_name

            existing = self._get_typed_relationship_by_name(causal_src, causal_tgt, "caused_by")
            if existing:
                self._reinforce_typed_relationship(
                    existing["id"],
                    datetime.now(UTC).isoformat(),
                )
                self._storage.update_relationship_fields(existing["id"], is_causal=1)
            else:
                src_e = self._storage.get_entity_by_name(causal_src)
                tgt_e = self._storage.get_entity_by_name(causal_tgt)
                if src_e and tgt_e:
                    now = datetime.now(UTC).isoformat()
                    self._storage.insert_typed_relationship(
                        src_e["id"],
                        tgt_e["id"],
                        "caused_by",
                        RelationshipMeta(
                            weight=1.0,
                            event_time=now,
                            record_time=now,
                            is_causal=1,
                            confidence=0.8,
                        ),
                    )
                    created += 1
        logger.debug("detect_causality: %d causal edges added", created)
        return created

    # -- d. Enhanced Entity Extraction --

    def extract_entities_typed(self, content: str, directory: str) -> list[tuple[str, str, str]]:
        results: list[tuple[str, str, str]] = []

        # Import relationships: "from X import Y" -> (X, dependency, imports)
        for m in _FROM_IMPORT_RE.finditer(content):
            module = m.group(1)
            names = [n.strip() for n in m.group(2).split(",")]
            results.append((module, "dependency", ""))
            for name in names:
                if name:
                    results.append((name, "function", "imports"))

        # Plain imports
        for m in _IMPORT_FULL_RE.finditer(content):
            results.append((m.group(1), "dependency", ""))

        # Function definitions
        defined_funcs = set()
        for m in _DEF_RE.finditer(content):
            fname = m.group(1)
            defined_funcs.add(fname)
            results.append((fname, "function", ""))

        # Function calls -> "calls" relationship context
        for m in _CALL_RE.finditer(content):
            fname = m.group(1)
            # Q9: removed always-false branch (fname == m.group(1) then != check)
            # Only mark as call if it's not a definition itself at this position
            # and is a known defined function being called elsewhere
            if fname in defined_funcs:
                # Check if this match is NOT the definition line
                start = m.start()
                line_start = content.rfind("\n", 0, start) + 1
                line = content[line_start : content.find("\n", start)]
                if not line.strip().startswith("def "):
                    results.append((fname, "function", "calls"))

        # Error-fix pattern -> "resolved_by"
        for m in _ERROR_FIX_RE.finditer(content):
            results.append((m.group(1), "error", "resolved_by"))

        # Decision pattern -> "decided_to_use"
        for m in _DECIDED_RE.finditer(content):
            chosen = m.group(1).strip()
            rejected = m.group(2).strip()
            results.append((chosen, "decision", "decided_to_use"))
            results.append((rejected, "decision", "decided_to_use"))

        # Deduplicate preserving order
        seen: set[tuple[str, str, str]] = set()
        unique: list[tuple[str, str, str]] = []
        for triple in results:
            if triple not in seen:
                seen.add(triple)
                unique.append(triple)
        return unique

    # -- e. Graph Traversal --

    def get_neighbors(
        self,
        entity_name: str,
        depth: int = 1,
        rel_types: list[str] | None = None,
    ) -> list[dict]:
        start_entity = self._storage.get_entity_by_name(entity_name)
        if not start_entity:
            return []

        visited: set[int] = {start_entity["id"]}
        result: list[dict] = []
        queue: deque[tuple[int, int]] = deque([(start_entity["id"], 0)])

        while queue:
            current_id, current_depth = queue.popleft()
            if current_depth >= depth:
                continue

            neighbors = self._get_adjacent(current_id, rel_types)
            for neighbor in neighbors:
                nid = neighbor["entity_id"]
                if nid not in visited:
                    visited.add(nid)
                    result.append(
                        {
                            "entity_id": nid,
                            "entity_name": neighbor["entity_name"],
                            "relationship_type": neighbor["relationship_type"],
                            "weight": neighbor["weight"],
                            "depth": current_depth + 1,
                        }
                    )
                    queue.append((nid, current_depth + 1))
        return result

    def get_subgraph(self, entity_names: list[str], depth: int = 2) -> dict:
        nodes: dict[int, dict] = {}
        edges: list[dict] = []
        edge_set: set[tuple[int, int, str]] = set()

        seed_ids: set[int] = set()
        for name in entity_names:
            entity = self._storage.get_entity_by_name(name)
            if entity:
                seed_ids.add(entity["id"])
                nodes[entity["id"]] = {
                    "id": entity["id"],
                    "name": entity["name"],
                    "type": entity["type"],
                    "heat": entity["heat"],
                }

        queue: deque[tuple[int, int]] = deque((eid, 0) for eid in seed_ids)
        visited: set[int] = set(seed_ids)

        while queue:
            current_id, current_depth = queue.popleft()
            if current_depth >= depth:
                continue

            neighbors = self._get_adjacent(current_id, None)
            for neighbor in neighbors:
                nid = neighbor["entity_id"]
                if nid not in nodes:
                    nent = self._storage.get_entity_by_id(nid)
                    if nent:
                        nodes[nid] = {
                            "id": nid,
                            "name": nent["name"],
                            "type": nent["type"],
                            "heat": nent["heat"],
                        }

                edge_key = (
                    min(current_id, nid),
                    max(current_id, nid),
                    neighbor["relationship_type"],
                )
                if edge_key not in edge_set:
                    edge_set.add(edge_key)
                    edges.append(
                        {
                            "source": current_id,
                            "target": nid,
                            "relationship_type": neighbor["relationship_type"],
                            "weight": neighbor["weight"],
                        }
                    )

                if nid not in visited:
                    visited.add(nid)
                    queue.append((nid, current_depth + 1))

        return {"nodes": list(nodes.values()), "edges": edges}

    # -- Internal helpers --

    def _ensure_entity(self, name: str) -> dict:
        existing = self._storage.get_entity_by_name(name)
        if existing:
            return existing
        eid = self._storage.insert_entity({"name": name, "type": "variable"})
        return self._storage.get_entity_by_id(eid)

    def _get_typed_relationship(self, source_id: int, target_id: int, rel_type: str) -> dict | None:
        return self._storage.get_typed_relationship(source_id, target_id, rel_type)

    def _get_typed_relationship_by_name(
        self, source_name: str, target_name: str, rel_type: str
    ) -> dict | None:
        src = self._storage.get_entity_by_name(source_name)
        tgt = self._storage.get_entity_by_name(target_name)
        if not src or not tgt:
            return None
        return self._get_typed_relationship(src["id"], tgt["id"], rel_type)

    def _insert_typed_relationship(
        self,
        source_id: int,
        target_id: int,
        rel_type: str,
        event_time_iso: str,
        record_time_iso: str,
        confidence: float,
    ) -> int:
        return self._storage.insert_typed_relationship(
            source_id,
            target_id,
            rel_type,
            RelationshipMeta(
                weight=1.0,
                event_time=event_time_iso,
                record_time=record_time_iso,
                is_causal=0,
                confidence=confidence,
            ),
        )

    def _reinforce_typed_relationship(self, rel_id: int, now_iso: str) -> None:
        self._storage.reinforce_relationship(rel_id, weight_increase=1.0)

    def _get_adjacent(self, entity_id: int, rel_types: list[str] | None) -> list[dict]:
        rows = self._storage.get_relationships_for_entity(entity_id, rel_types)
        result = []
        for row_d in rows:
            other_id = (
                row_d["target_entity_id"]
                if row_d["source_entity_id"] == entity_id
                else row_d["source_entity_id"]
            )
            other_name = (
                row_d.get("target_name")
                if row_d["source_entity_id"] == entity_id
                else row_d.get("source_name")
            )
            result.append(
                {
                    "entity_id": other_id,
                    "entity_name": other_name,
                    "relationship_type": row_d["relationship_type"],
                    "weight": row_d["weight"],
                }
            )
        return result

    def _check_temporal_order(self, entity_a: str, entity_b: str) -> str | None:
        # Only scan the most recent episodes — avoid a full-table scan that is
        # O(episodes × content_len) and grows unboundedly with age.
        _MAX_SCAN = 2000
        episodes = self._storage.get_recent_episodes(_MAX_SCAN)

        a_before_b = 0
        b_before_a = 0

        for ep in episodes:
            content = ep["raw_content"]
            pos_a = content.find(entity_a)
            pos_b = content.find(entity_b)
            if pos_a >= 0 and pos_b >= 0:
                if pos_a < pos_b:
                    a_before_b += 1
                elif pos_b < pos_a:
                    b_before_a += 1

        threshold = self._settings.CAUSAL_THRESHOLD
        if a_before_b >= threshold and a_before_b > b_before_a:
            return "before"
        if b_before_a >= threshold and b_before_a > a_before_b:
            return "after"
        return None
