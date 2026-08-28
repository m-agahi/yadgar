"""Graph node assembly — memory, wiki, and entity node builders (C4 split).

This mixin is merged into ``GraphAPI`` via multiple inheritance. It holds the
four DB-query helpers that turn StorageEngine rows into visualization node dicts.
``_extract_id`` and ``_limit_clause`` live in ``graph_api`` (used by both
node and edge builders; keeping them there avoids cross-sibling import cycles).
"""

from yadgar._shared.observability.observe import observe


class GraphAPINodesMixin:
    """Node-assembly helpers for ``GraphAPI``."""

    @observe(tier="stage")
    def _assemble_memory_nodes(
        self, nodes: list[dict], max_memories: int
    ) -> tuple[set[int], dict[int, list[tuple[int, str]]], dict[int, list[str]]]:
        """Fetch memory rows, append node dicts.

        Returns (mem_ids, slot_map, wiki_refs_map). wiki_refs_map is
        {mem_id: [wiki_slug, ...]} sourced from the memory.wiki_refs column —
        the reverse of wiki_page.source_memory_ids (which is always empty on
        every write path). Used by _build_memory_wiki_edges (P2.1).
        """
        _suffix, _params = self._limit_clause(max_memories)
        try:
            memories = self._s._q(
                # viz-render-perf (Car A): embedding dropped from the SELECT — the
                # node dict never emits it (pure ~MBs/request waste over the wire).
                "SELECT id, content, heat, tags, directory_context, created_at, "
                "last_accessed, slot_index, cluster_id, wiki_refs FROM memory "
                "ORDER BY heat DESC" + _suffix,
                _params,
            )
        except Exception:  # noqa: BLE001 — per-node-type degradation in the viz payload builder: storage._q raises RuntimeError over HTTP and arbitrary SDK types embedded with no common base; an empty memory set still renders the wiki and entity layers
            memories = []
        mem_ids: set[int] = set()
        slot_map: dict[int, list[tuple[int, str]]] = {}
        wiki_refs_map: dict[int, list[str]] = {}
        for m in memories:
            raw_id = self._extract_id(m.get("id"))
            if raw_id is None:
                continue
            node_id = f"mem:{raw_id}"
            mem_ids.add(raw_id)
            refs = m.get("wiki_refs") or []
            if refs:
                wiki_refs_map[raw_id] = [str(r) for r in refs]
            # Coalesce content + created_at ONCE (used twice each below) — keeps the
            # loop's ``or`` count (and thus cyclomatic) flat despite the added
            # last_accessed field (#55), so _assemble_memory_nodes stays within I13.
            _content = m.get("content") or ""
            _created = str(m.get("created_at") or "")
            nodes.append(
                {
                    "id": node_id,
                    "type": "memory",
                    "heat": round(float(m.get("heat") or 0), 4),
                    "label": _content[:60],
                    "content": _content[:400],
                    "tags": m.get("tags") or [],
                    "directory": m.get("directory_context") or "",
                    "created_at": _created,
                    # #55: last_accessed (recency) — shown in the detail panel so a
                    # node's freshness reads independently of its heat value.
                    "last_accessed": str(m.get("last_accessed") or ""),
                    # P2.3: cluster_id (column on the memory row) → viz cluster tint
                    "cluster_id": self._extract_id(m.get("cluster_id")),
                }
            )
            slot = m.get("slot_index")
            if slot is not None:
                slot_map.setdefault(int(slot), []).append((raw_id, _created))
        return mem_ids, slot_map, wiki_refs_map

    @observe(tier="stage")
    def _assemble_wiki_nodes(
        self, nodes: list[dict], max_wiki: int = 200
    ) -> tuple[list[dict], dict[str, str]]:
        """Fetch wiki pages, append node dicts, return (wiki_pages, wiki_slug_to_id).

        max_wiki caps the result (0/-1 = unlimited → no LIMIT clause).
        """
        _suffix, _params = self._limit_clause(max_wiki)
        try:
            wiki_pages = self._s._q(
                # viz-render-perf (Car A): embedding dropped — neither the node dict
                # nor the returned wiki_pages consumer reads it (pure wire waste).
                "SELECT id, title, slug, category, tags, links, source_memory_ids, "
                "updated_at FROM wiki_page ORDER BY updated_at DESC" + _suffix,
                _params,
            )
        except Exception:  # noqa: BLE001 — per-node-type degradation for wiki_page rows; same untypeable storage._q surface
            wiki_pages = []
        wiki_slug_to_id: dict[str, str] = {}
        for wp in wiki_pages or []:
            raw_id = self._extract_id(wp.get("id"))
            if raw_id is None:
                continue
            node_id = f"wiki:{raw_id}"
            slug = wp.get("slug") or ""
            wiki_slug_to_id[slug] = node_id
            nodes.append(
                {
                    "id": node_id,
                    "type": "wiki",
                    "label": wp.get("title") or slug,
                    "slug": slug,
                    "category": wp.get("category") or "",
                    "tags": wp.get("tags") or [],
                    "updated_at": str(wp.get("updated_at") or ""),
                }
            )
        return wiki_pages or [], wiki_slug_to_id

    @observe(tier="stage")
    def _assemble_entity_nodes(self, nodes: list[dict], max_entities: int = 2000) -> None:
        """Fetch all entities and append entity:* node dicts to *nodes*.

        v5.31.1: entity nodes were removed in v5.0.0 monolith split.  Without
        them every causal edge references an absent node ID and is dropped by
        the orphan filter — making include_invalidated filtering unobservable
        via get_full_graph().  Restoring entity nodes fixes the orphan filter
        for causal edges while keeping all other edge types unchanged.

        v5.88 FIX2: max_entities caps the node set (0/-1 = unlimited). Sliced
        post-fetch because get_all_entities is shared by 9 callers; it already
        returns rows ORDER BY heat DESC, so the slice keeps the hottest N.
        """
        try:
            all_entities = self._s.get_all_entities(include_archived=True)
        except Exception:  # noqa: BLE001 — per-node-type degradation for entity rows; same untypeable storage surface
            all_entities = []
        if max_entities > 0:
            all_entities = all_entities[:max_entities]
        for ent in all_entities:
            raw_id = self._extract_id(ent.get("id"))
            if raw_id is None:
                continue
            nodes.append(
                {
                    "id": f"entity:{raw_id}",
                    "type": "entity",
                    "label": (ent.get("name") or "")[:60],
                    "heat": round(float(ent.get("heat") or 0), 4),
                }
            )

    @observe(tier="stage")
    def _expand_memory(self, raw_id: int, nodes: list, seen: set) -> None:
        try:
            m = self._s.get_memory(raw_id)
        except Exception:  # noqa: BLE001 — per-node expansion: a single get_memory over the untypeable storage surface; an unreadable id is skipped so the rest of the expansion still returns
            return
        if m is None:
            return
        nid = f"mem:{raw_id}"
        if nid not in seen:
            seen.add(nid)
            nodes.append(
                {
                    "id": nid,
                    "type": "memory",
                    "heat": round(float(m.get("heat") or 0), 4),
                    "label": (m.get("content") or "")[:60],
                    "content": m.get("content") or "",
                    "tags": m.get("tags") or [],
                    "directory": m.get("directory_context") or "",
                    "created_at": str(m.get("created_at") or ""),
                }
            )
