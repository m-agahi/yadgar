"""Graph API — assembles graph JSON for knowledge graph visualization (BACKEND).

T2 Car E3 (census verdict #11): moved from core — the DB-heavy data assembly
runs next to the DB; core /api/graph* endpoints forward via POST /viz. The
process/system-metrics sampler that shared this module historically now lives
in ``yadgar.core.daemon.system_metrics`` (it introspects the CORE daemon).

v5.54.3: entity typed-relation edges (co_occurrence/resolved_by/caused_by;
imports/calls dropped in v5.86 Batch-2 P0.4 — code-only, empty on prose)
now included in the default /api/graph payload with role="retrieval" sourced from
EDGE_TYPES (viz_meta.py). All edges carry a `role` field.

v5.87 (C3): semantic edges removed entirely — they were lazy/off-by-default,
O(n²) KNN, and informational (redundant with the vector signal recall uses). The
legend toggle did nothing useful, so the edge type was dropped from EDGE_TYPES +
LAZY_EDGE_TYPES and its on-demand compute path (_get/_compute_semantic_edges,
_parse_embedding_vectors, _deduplicated_edges) was deleted.

v5.80 (#80 viz-fidelity-v2): role vocabulary renamed display→informational in viz_meta.
clusters[] added to get_full_graph() payload (real memory_cluster rows via
get_memory_clusters() + get_cluster_members()). memory_similarity_link edges added
(_build_similarity_link_edges) with role="informational".

C4 (module-standardization-train-2026-07-13): internal split — node assembly
helpers in ``graph_nodes.py``, edge builders in ``graph_edges.py``, merged into
``GraphAPI`` via mixin inheritance. Public API and re-export surface unchanged.
"""

import logging

# T2 Car E3: the edge registry is a _shared CONTRACT (dual: backend stamps
# roles, core styles the legend) — never import core.viz from the backend.
from yadgar._shared.contracts.viz import LAZY_EDGE_TYPES
from yadgar._shared.observability.metrics import (
    yadgar_graph_api_orphan_edges_dropped_total,
)
from yadgar._shared.observability.tracing import trace_span

from .graph_edges import GraphAPIEdgesMixin
from .graph_nodes import GraphAPINodesMixin

logger = logging.getLogger(__name__)


def _limit_clause(cap: int) -> tuple[str, dict]:
    """Return (sql_suffix, params) for a node cap. 0 or -1 (any <=0) = unlimited.

    Capped → " LIMIT $lim" + {"lim": cap}; unlimited → ("", {}) so the query
    omits the LIMIT entirely (v5.88 FIX2 configurable node caps).
    """
    if cap <= 0:
        return "", {}
    return " LIMIT $lim", {"lim": cap}


class GraphAPI(GraphAPINodesMixin, GraphAPIEdgesMixin):
    """Assembles graph data (nodes + edges) from StorageEngine for visualization."""

    def __init__(self, storage) -> None:
        self._s = storage

    # ── Shared helper (used by both node and edge mixins via self) ────────────

    @staticmethod
    def _extract_id(raw) -> int | None:
        """Extract numeric ID from a SurrealDB record ID (e.g. 'entity:42' → 42).

        Handles both integer and string record_id variants produced by the
        surrealdb Python client:
          - RecordID with int .id   → str() = "memory:42"    → 42
          - RecordID with str .id   → str() = "memory:'42'"  → .id attr → 42
        """
        if raw is None:
            return None
        if isinstance(raw, int):
            return raw
        # RecordID object: use .id attribute directly (handles both int and str IDs)
        if hasattr(raw, "id") and hasattr(raw, "table_name"):
            try:
                return int(raw.id)
            except (ValueError, TypeError) as _e:
                return None
        s = str(raw)
        if ":" in s:
            s = s.rsplit(":", 1)[-1]
        s = s.strip("'\"")
        try:
            return int(s)
        except (ValueError, TypeError) as _e:
            return None

    # ── Shared utility (used by both node and edge mixins via self) ───────────

    @staticmethod
    def _limit_clause(cap: int) -> tuple[str, dict]:
        """Instance-accessible alias for the module-level ``_limit_clause``."""
        return _limit_clause(cap)

    # ── Public API ────────────────────────────────────────────────────────────

    @trace_span()
    def get_full_graph(
        self,
        max_memories: int = 500,
        top_k: int = 8,
        include_invalidated: bool = False,
        as_of: str | None = None,
        max_wiki: int = 200,
        max_entities: int = 2000,
    ) -> dict:
        """Return full graph: memory + wiki + entity nodes with typed edges.

        include_invalidated: when False (default), excludes invalidated KG edges.
        as_of (v5.29.0): ISO-8601 timestamp for point-in-time graph snapshot.

        v5.54.3: entity typed-relation edges added; all edges carry `role`
        field sourced from EDGE_TYPES. (v5.87 C3: semantic edge type removed.)
        """
        nodes: list[dict] = []
        edges: list[dict] = []

        # ── Memory nodes + slot map ───────────────────────────────────────────
        mem_ids, slot_map, wiki_refs_map = self._assemble_memory_nodes(nodes, max_memories)

        # ── Temporal edges ────────────────────────────────────────────────────
        edges.extend(self._build_temporal_edges(slot_map))

        # ── Transition edges ──────────────────────────────────────────────────
        transition_edges, weak_edges_hidden = self._build_transition_edges(mem_ids)
        edges.extend(transition_edges)

        # ── Wiki nodes ────────────────────────────────────────────────────────
        _wiki_pages, wiki_slug_to_id = self._assemble_wiki_nodes(nodes, max_wiki)

        # ── Wiki cross-reference edges ────────────────────────────────────────
        edges.extend(self._build_wiki_crossref_edges(wiki_slug_to_id))

        # ── Memory → Wiki edges (P2.1: reverse memory.wiki_refs bridge) ───────
        edges.extend(self._build_memory_wiki_edges(wiki_refs_map, wiki_slug_to_id))

        # ── Entity nodes (required so entity edges pass orphan filter) ────────
        self._assemble_entity_nodes(nodes, max_entities)

        # ── Causal edges (PC-algorithm) ───────────────────────────────────────
        edges.extend(self._build_causal_edges(include_invalidated, as_of))

        # ── Entity typed-relation edges (v5.54.3 — retrieval-active, was invisible) ─
        edges.extend(self._build_entity_rel_edges())

        # ── Memory similarity-link edges (v5.80 — informational near-duplicate links) ─
        edges.extend(self._build_similarity_link_edges(mem_ids))

        # ── Orphan-edge filter (v5.10.9) ──────────────────────────────────────
        node_ids = {n["id"] for n in nodes}
        filtered_edges = [
            e for e in edges if e.get("source") in node_ids and e.get("target") in node_ids
        ]
        orphan_count = len(edges) - len(filtered_edges)
        if orphan_count > 0:
            logger.info(
                "graph_api: dropped %d orphan edge(s) (endpoints absent from node set)",
                orphan_count,
            )
            yadgar_graph_api_orphan_edges_dropped_total.inc(orphan_count)

        # ── Cluster payload (v5.80 — real memory_cluster rows) ────────────────
        clusters = self._build_clusters_payload(mem_ids)

        return {
            "nodes": nodes,
            "edges": filtered_edges,
            "weak_edges_hidden": weak_edges_hidden,  # F4 affordance — never silently drop DB truth
            "clusters": clusters,  # BC-VZ-R3: real memory_cluster rows (informational)
        }

    @trace_span()
    def get_edges_by_type(
        self,
        edge_type: str,
        max_memories: int = 500,
        top_k: int = 8,
    ) -> dict:
        """On-demand edge computation for lazy edge types.

        Generic gate for any edge type in LAZY_EDGE_TYPES. v5.87 C3: that set is
        now empty (semantic, its only member, was removed) so every type returns
        the not-lazy-computed error. Kept for future lazy edge types.

        Returns {"edges": [...]} (no nodes — caller merges into existing graph).
        """
        if edge_type not in LAZY_EDGE_TYPES:
            return {"edges": [], "error": f"Edge type '{edge_type}' is not lazy-computed."}

        # v5.87 C3: LAZY_EDGE_TYPES is now empty (semantic removed) — this point
        # is unreachable. Kept as a generic gate for any future lazy edge type.
        return {"edges": []}

    @trace_span()
    def get_graph_stats(self) -> dict:
        """Return graph statistics: memory count, edge type counts."""
        try:
            mem_count = (self._s._q("SELECT count() FROM memory GROUP ALL") or [{}])[0].get(
                "count", 0
            )
            transition_count = (
                self._s._q("SELECT count() FROM memory_transition WHERE count >= 2 GROUP ALL")
                or [{}]
            )[0].get("count", 0)
            # Temporal edges = memories sharing slots; approximate by counting slots with 2+ members
            # NOTE: must be `IS NOT NONE`, not `IS NOT NULL` — in SurrealDB an
            # unset field is NONE, and NONE passes `IS NOT NULL`. The old query
            # lumped every slot-less memory into one phantom group, reporting a
            # bogus all-pairs temporal-edge count (e.g. 1016 unassigned → ~515k).
            slot_rows = (
                self._s._q(
                    "SELECT slot_index, count() as cnt FROM memory "
                    "WHERE slot_index IS NOT NONE GROUP BY slot_index"
                )
                or []
            )
            temporal_count = sum(
                r.get("cnt", 0) * (r.get("cnt", 0) - 1) // 2
                for r in slot_rows
                if (r.get("cnt") or 0) >= 2
            )
            wiki_count = (self._s._q("SELECT count() FROM wiki_page GROUP ALL") or [{}])[0].get(
                "count", 0
            )
        except Exception as exc:
            logger.debug("graph_stats error: %s", exc)
            return {}

        return {
            "memory_count": mem_count,
            "temporal_edge_count": temporal_count,
            "transition_edge_count": transition_count,
            "wiki_page_count": wiki_count,
        }

    @trace_span()
    def get_neighborhood(self, node_id: str, hops: int = 2) -> dict:
        """Return subgraph around a memory node."""
        nodes: list[dict] = []
        seen_nodes: set[str] = set()

        if node_id.startswith("mem:"):
            raw_id = self._extract_id(node_id[4:])
            if raw_id is not None:
                self._expand_memory(raw_id, nodes, seen_nodes)

        return {"nodes": nodes, "edges": []}
