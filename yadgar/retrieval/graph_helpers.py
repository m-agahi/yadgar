"""_GraphHelpersMixin: networkx/entity graph helpers extracted from Retriever.core."""

from collections import defaultdict

import networkx as nx


class _GraphHelpersMixin:
    """Graph-traversal helpers for Retriever.

    Methods here read ``self._storage``, ``self._graph``, and ``self._settings``
    — all available on the Retriever instance via the normal MRO.
    """

    def _build_networkx_graph(
        self, seed_entity_ids: list[int], max_hops: int | None = None
    ) -> nx.DiGraph:
        """Build a networkx DiGraph around the seed entities.

        v5.99.0: adjacency for each BFS depth is fetched in ONE batched query
        (``_get_adjacent_batch``) instead of one query per frontier node, and the
        unused name enrichment is skipped. Node/edge insertion order is byte-identical
        to the legacy per-node build (see ``_build_networkx_graph_pernode`` and the
        parity gate in ``test_v5_99_ppr_batch_parity.py``), so PPR scores are
        unchanged.
        """
        if max_hops is None:
            max_hops = self._settings.GRAPH_MAX_HOPS
        G = nx.DiGraph()
        visited: set[int] = set()
        frontier = list(seed_entity_ids)

        for _ in range(max_hops):
            next_frontier: list[int] = []
            # Only expand nodes not yet visited — mirrors the per-node loop's
            # in-loop `visited` guard while collapsing the fetch into one query.
            to_expand = [eid for eid in frontier if eid not in visited]
            adjacency = self._graph._get_adjacent_batch(to_expand, None)
            for eid in frontier:
                if eid in visited:
                    continue
                visited.add(eid)
                G.add_node(eid)
                neighbors = adjacency.get(eid, [])
                for n in neighbors:
                    nid = n["entity_id"]
                    weight = n["weight"]
                    if weight < self._settings.GRAPH_MIN_EDGE_WEIGHT:
                        continue
                    G.add_node(nid)
                    G.add_edge(eid, nid, weight=weight)
                    G.add_edge(nid, eid, weight=weight)
                    if nid not in visited:
                        next_frontier.append(nid)
            frontier = next_frontier

        return G

    def _build_networkx_graph_pernode(
        self, seed_entity_ids: list[int], max_hops: int | None = None
    ) -> nx.DiGraph:
        """Legacy per-node PPR graph build — retained only as the parity baseline.

        Exact copy of the pre-v5.99 ``_build_networkx_graph`` (one ``_get_adjacent``
        query per frontier node, names enriched). The v5.99 batched build must be
        byte-identical to this; the parity test asserts it. Not used in production.
        """
        if max_hops is None:
            max_hops = self._settings.GRAPH_MAX_HOPS
        G = nx.DiGraph()
        visited: set[int] = set()
        frontier = list(seed_entity_ids)

        for _ in range(max_hops):
            next_frontier: list[int] = []
            for eid in frontier:
                if eid in visited:
                    continue
                visited.add(eid)
                G.add_node(eid)
                neighbors = self._graph._get_adjacent(eid, None)
                for n in neighbors:
                    nid = n["entity_id"]
                    weight = n["weight"]
                    if weight < self._settings.GRAPH_MIN_EDGE_WEIGHT:
                        continue
                    G.add_node(nid)
                    G.add_edge(eid, nid, weight=weight)
                    G.add_edge(nid, eid, weight=weight)
                    if nid not in visited:
                        next_frontier.append(nid)
            frontier = next_frontier

        return G

    def _find_memories_for_entity(self, entity_name: str) -> list[int]:
        """Find memory IDs whose content contains the entity name."""
        return self._storage.find_memory_ids_by_entity_name(entity_name)

    def _find_entities_in_content(self, content: str) -> set[int]:
        """Find entity IDs that appear in the given content."""
        entity_ids: set[int] = set()
        # Get all active entities and check which ones appear in the content
        entities = self._storage.get_all_entities(min_heat=0.0, include_archived=True)
        for entity in entities:
            if entity["name"] in content:
                entity_ids.add(entity["id"])
        return entity_ids

    def _get_top_cooccurring_entities(self, content: str, limit: int = 5) -> list[str]:
        """Find entities that co-occur with entities mentioned in this content."""
        # Find entities mentioned in the content
        content_entities = self._find_entities_in_content(content)
        if not content_entities:
            return []

        # Count co-occurrence partners
        partner_counts: dict[str, float] = defaultdict(float)
        for eid in content_entities:
            neighbors = self._graph._get_adjacent(eid, None)
            for n in neighbors:
                entity_row = self._storage.get_entity_by_id(n["entity_id"])
                if entity_row and n["entity_id"] not in content_entities:
                    partner_counts[entity_row["name"]] += n["weight"]

        # Sort by weight and return top
        sorted_partners = sorted(partner_counts.items(), key=lambda x: x[1], reverse=True)
        return [name for name, _ in sorted_partners[:limit]]
