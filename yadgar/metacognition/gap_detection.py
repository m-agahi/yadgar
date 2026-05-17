"""Gap detection mixin — 'What don't I know about this project?'"""

from collections import defaultdict


class _GapDetectionMixin:
    """Metacognitive gap detection (MetaRAG signal 2)."""

    def detect_gaps(self, directory: str = "") -> list[dict]:
        """Analyze knowledge completeness for a directory/project.

        Returns a list of knowledge gaps with type, description,
        severity, affected entities, and remediation suggestions.
        """
        gaps: list[dict] = []

        # a) Isolated entities: entities with 0 or 1 relationships
        all_entities = self._storage.get_all_entities(min_heat=0.0, include_archived=False)
        for entity in all_entities:
            neighbors = self._graph._get_adjacent(entity["id"], None)
            if len(neighbors) <= 1:
                gaps.append(
                    {
                        "type": "isolated_entity",
                        "description": (
                            f"Entity '{entity['name']}' has only "
                            f"{len(neighbors)} connection(s) — poorly integrated "
                            f"into the knowledge graph."
                        ),
                        "severity": 0.6 if len(neighbors) == 0 else 0.4,
                        "entities": [entity["name"]],
                        "suggestion": (
                            f"Add more context about '{entity['name']}' to "
                            f"strengthen its connections in the knowledge graph."
                        ),
                    }
                )

        # b) Stale regions: clusters of memories with heat < 0.3
        if directory:
            dir_memories = self._storage.get_memories_for_directory(directory, min_heat=0.0)
        else:
            dir_memories = self._storage.get_all_memories_for_decay()

        stale_memories = [m for m in dir_memories if m.get("heat", 1.0) < 0.3]
        if len(stale_memories) >= 2:
            stale_tags = set()
            for m in stale_memories:
                tags = m.get("tags", [])
                if isinstance(tags, list):
                    stale_tags.update(tags)
            gaps.append(
                {
                    "type": "stale_region",
                    "description": (
                        f"{len(stale_memories)} memories have decayed below "
                        f"heat 0.3 — knowledge may be outdated."
                    ),
                    "severity": min(0.9, 0.3 + len(stale_memories) * 0.1),
                    "entities": list(stale_tags)[:10],
                    "suggestion": (
                        "Review and refresh these memories or validate "
                        "against current project state."
                    ),
                }
            )

        # c) Low-confidence zones: memories with confidence < 0.5
        low_conf = [m for m in dir_memories if m.get("confidence", 1.0) < 0.5]
        if low_conf:
            low_conf_descriptions = []
            for m in low_conf[:5]:
                content_preview = m.get("content", "")[:60]
                low_conf_descriptions.append(content_preview)
            gaps.append(
                {
                    "type": "low_confidence",
                    "description": (
                        f"{len(low_conf)} memories have confidence below 0.5 "
                        f"— unreliable knowledge detected."
                    ),
                    "severity": min(0.8, 0.3 + len(low_conf) * 0.1),
                    "entities": low_conf_descriptions,
                    "suggestion": (
                        "Validate low-confidence memories against "
                        "current source code or documentation."
                    ),
                }
            )

        # d) Missing connections: entities that co-occur in content
        #    but have no relationship in the graph
        entity_cooccurrence = defaultdict(set)
        for m in dir_memories:
            content = m.get("content", "")
            entities_in_mem = []
            for entity in all_entities:
                if entity["name"] in content:
                    entities_in_mem.append(entity["id"])
            for i, eid_a in enumerate(entities_in_mem):
                for eid_b in entities_in_mem[i + 1 :]:
                    entity_cooccurrence[(eid_a, eid_b)].add(m.get("id"))

        # ONE bulk fetch for all co-occurring entity pairs instead of O(N²) HTTP calls.
        cooccurrence_ids = list({eid for pair in entity_cooccurrence for eid in pair})
        existing_rel_index: set[tuple[int, int]] = set()
        if cooccurrence_ids:
            for rel in self._storage.get_relationships_among_entities(cooccurrence_ids):
                sid = rel.get("source_entity_id")
                tid = rel.get("target_entity_id")
                if sid is not None and tid is not None:
                    existing_rel_index.add((min(sid, tid), max(sid, tid)))

        entity_by_id = {e["id"]: e for e in all_entities}
        for (eid_a, eid_b), mem_ids in entity_cooccurrence.items():
            if len(mem_ids) < 2:
                continue
            # Check if relationship exists via pre-fetched index
            existing = (min(eid_a, eid_b), max(eid_a, eid_b)) in existing_rel_index
            if not existing:
                ent_a = entity_by_id.get(eid_a)
                ent_b = entity_by_id.get(eid_b)
                if ent_a and ent_b:
                    gaps.append(
                        {
                            "type": "missing_connection",
                            "description": (
                                f"'{ent_a['name']}' and '{ent_b['name']}' co-occur in "
                                f"{len(mem_ids)} memories but have no relationship."
                            ),
                            "severity": min(0.7, 0.2 + len(mem_ids) * 0.1),
                            "entities": [ent_a["name"], ent_b["name"]],
                            "suggestion": (
                                f"Add a relationship between '{ent_a['name']}' and "
                                f"'{ent_b['name']}' to capture their connection."
                            ),
                        }
                    )

        # e) One-sided knowledge: only errors stored, no solutions (or vice versa)
        error_entities = [e for e in all_entities if e.get("type") == "error"]
        solution_entities = [e for e in all_entities if e.get("type") == "solution"]
        {e["name"] for e in error_entities}
        {e["name"] for e in solution_entities}

        # Check for "resolved_by" relationships from error entities
        for err_entity in error_entities:
            has_resolution = self._storage.get_relationship_by_source_and_type(
                err_entity["id"], "resolved_by"
            )
            if has_resolution is None:
                gaps.append(
                    {
                        "type": "one_sided_knowledge",
                        "description": (
                            f"Error '{err_entity['name']}' has no recorded "
                            f"resolution — only the problem is known."
                        ),
                        "severity": 0.5,
                        "entities": [err_entity["name"]],
                        "suggestion": (
                            f"Record how '{err_entity['name']}' was resolved "
                            f"to complete the knowledge."
                        ),
                    }
                )

        return gaps
