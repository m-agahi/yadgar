"""yadgar.backend.graph — knowledge-graph data assembly + layout compute.

T2 Car E3 (census verdict #11): moved from ``yadgar.core.graph`` — the
DB-heavy graph assembly and the force-layout compute run next to the DB.
Core /api/graph* endpoints forward via POST /viz (``backend.viz_exec``);
the layout precompute runs inside the backend consolidation full/nightly
cycle (``backend.consolidation``).

C4 (module-standardization-train-2026-07-13): internal I13 split.

  graph_api.py    — GraphAPI (public API: get_full_graph, get_graph_stats,
                    get_neighborhood, get_edges_by_type) + _limit_clause +
                    _extract_id + mixin wiring
  graph_nodes.py  — GraphAPINodesMixin: node assembly helpers
                    (_assemble_memory_nodes, _assemble_wiki_nodes,
                    _assemble_entity_nodes, _expand_memory)
  graph_edges.py  — GraphAPIEdgesMixin: edge builder helpers
                    (_build_temporal_edges, _build_transition_edges,
                    _build_wiki_crossref_edges, _build_memory_wiki_edges,
                    _build_causal_edges, _build_entity_rel_edges,
                    _build_similarity_link_edges, _build_clusters_payload)
  graph_layout.py — cached force-layout compute + position attachment

The process/system-metrics sampler that historically shared graph_api.py
lives in ``yadgar.core.daemon.system_metrics`` (core-process introspection).
"""

from yadgar.backend.graph.graph_api import GraphAPI

__all__ = ["GraphAPI"]
