"""_GraphHelpersMixin: networkx/entity graph helpers extracted from Retriever.core."""

from collections import defaultdict

import networkx as nx

from yadgar._shared.observability.observe import observe


class _GraphHelpersMixin:
    """Graph-traversal helpers for Retriever.

    Methods here read ``self._storage``, ``self._graph``, and ``self._settings``
    — all available on the Retriever instance via the normal MRO.
    """

    @observe(tier="hot", metric="retrieval.graph.build_networkx")
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

    def _find_memories_for_entities(self, entity_names: list[int]) -> dict[str, list[int]]:
        """Batched ``_find_memories_for_entity`` — one round-trip for N names (v5.102.0).

        Thin wrapper over ``storage.find_memory_ids_by_entities``; used by the
        spreading-activation per-depth batch. Returns ``{name: [memory_id, ...]}``,
        exact-parity with calling ``_find_memories_for_entity`` per name.
        """
        return self._storage.find_memory_ids_by_entities(entity_names)

    @observe(tier="hot", metric="retrieval.graph.find_entities_in_content")
    def _find_entities_in_content(self, content: str) -> set[int]:
        """Find entity IDs that appear in the given content."""
        entity_ids: set[int] = set()
        # Get all active entities and check which ones appear in the content
        entities = self._storage.get_all_entities(min_heat=0.0, include_archived=True)
        for entity in entities:
            if entity["name"] in content:
                entity_ids.add(entity["id"])
        return entity_ids

    @observe(tier="hot", metric="retrieval.graph.top_cooccurring")
    def _get_top_cooccurring_entities(self, content: str, limit: int = 5) -> list[str]:
        """Find entities that co-occur with entities mentioned in this content."""
        # Find entities mentioned in the content
        content_entities = self._find_entities_in_content(content)
        if not content_entities:
            return []

        # Count co-occurrence partners via ONE batched adjacency query (no name
        # enrichment) + ONE batched entity-name lookup for all unique neighbor ids.
        # The old per-entity _get_adjacent + per-neighbor get_entity_by_id pattern
        # fires 1 + 2*K name-enrichment queries per entity and one storage round-trip
        # per unique neighbour name — O(N²) on a dense graph. The batch path collapses
        # both loops to two total queries regardless of graph size.
        content_entity_ids = list(content_entities)
        adjacency_batch = self._graph._get_adjacent_batch(content_entity_ids, None)

        # Collect all neighbor ids that are not in the content-entity set so we can
        # resolve their names in a single bulk fetch.
        partner_weight: dict[int, float] = defaultdict(float)
        for eid in content_entity_ids:
            for n in adjacency_batch.get(eid, []):
                nid = n["entity_id"]
                if nid not in content_entities:
                    partner_weight[nid] += n["weight"]

        # Bulk-fetch entity rows for all partner ids; filter missing entries.
        partner_counts: dict[str, float] = defaultdict(float)
        if partner_weight:
            entity_map = self._storage.get_entities_by_ids(list(partner_weight))
            for nid, weight in partner_weight.items():
                entity_row = entity_map.get(nid)
                if entity_row:
                    partner_counts[entity_row["name"]] += weight

        # Sort by weight and return top
        sorted_partners = sorted(partner_counts.items(), key=lambda x: x[1], reverse=True)
        return [name for name, _ in sorted_partners[:limit]]
