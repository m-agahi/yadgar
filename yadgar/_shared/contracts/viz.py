"""Viz edge-type registry contract (T2 Car E3).

DUAL by the placement laws: the backend ``GraphAPI`` stamps every emitted
edge's ``role`` field from this registry, and the core legend builder
(``core.viz.viz_meta``) styles the viz legend from the same rows. Extracted
from ``viz_meta.py`` when the graph data assembly moved to the backend
(census verdict #11) so neither layer imports across the boundary for it.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Edge type registry
# ---------------------------------------------------------------------------
# Keys MUST match the "type" strings emitted in graph_api.py.
# Colors come from Settings at response time (see http.py api_viz_config).
# 'causal' uses fallback #484f58 — no VIZ_EDGE_COLOR_CAUSAL setting.
#
# role (v5.54.3, per docs/contracts/EDGE_CONTRACT.md; renamed v5.80 viz-fidelity-v2):
#   "retrieval"     — feeds recall ranking (PPR / spreading / precomputed prior).
#                     These are the load-bearing edges; styled solid/brighter.
#   "informational" — structural or derived; not wired to retrieval scoring.
#                     Styled dashed/dimmer. Renamed from "display" in v5.80.
#
# Entity typed-relations (co_occurrence/resolved_by/caused_by — imports/calls
# dropped from viz in v5.86 Batch-2, P0.4: code-only, always empty on prose):
#   role=retrieval — these power PPR + spreading in balanced/full profiles, and
#   the precomputed graph_prior in fast profile (v5.54.1). The biggest hidden
#   capability — previously invisible in the viz.
#
# default_on (v5.54.3): whether the toggle is checked by default.
#   retrieval-role edges default ON; heavy informational-only (semantic) OFF.
EDGE_TYPES: dict[str, dict] = {
    # v5.87 C3: "semantic" removed from the viz registry. It was lazy/off-by-
    # default, O(n²) KNN, and informational (redundant with the vector signal
    # recall already uses). The legend checkbox surfaced but did nothing useful,
    # so it was dropped to stop the legend advertising a dead/unwanted toggle.
    # The backend on-demand compute path (_get/_compute_semantic_edges +
    # _parse_embedding_vectors / _deduplicated_edges) was deleted with it, so the
    # produced≡contracted invariant (I29 / drift-guard) stays satisfied.
    # ── Informational (temporal slot co-membership — weak signal) ────────────
    "temporal": {
        "label": "Temporal",
        "description": "Co-occurrence in time: two memories stored within the same temporal slot. Informational — weak signal, not wired to retrieval.",
        "settings_color_key": "VIZ_EDGE_COLOR_TEMPORAL",
        "fallback_color": "#6e40c9",
        "role": "informational",
        "default_on": True,
    },
    # ── Retrieval-active (co-recall precomputed prior, v5.54.2) ──────────────
    "transition": {
        "label": "Transition",
        "description": "Co-recall pattern: memories retrieved together ≥2 times (strength ∝ edge width). Retrieval-active: powers the cofire_prior boost in all profiles (v5.54.2).",
        "settings_color_key": "VIZ_EDGE_COLOR_TRANSITION",
        "fallback_color": "#3fb950",
        "role": "retrieval",
        "default_on": True,
    },
    # ── Informational (wiki structure) ────────────────────────────────────────
    "wiki_crossref": {
        "label": "Wiki Link",
        "description": "Explicit cross-reference between two wiki pages (from page [[link]] syntax). Informational — structural, not retrieval-active.",
        "settings_color_key": "VIZ_EDGE_COLOR_WIKI_CROSSREF",
        "fallback_color": "#d2a8ff",
        "role": "informational",
        "default_on": True,
    },
    # ── Informational (memory→wiki provenance) ───────────────────────────────
    "memory_wiki": {
        "label": "Mem→Wiki",
        "description": "A memory was used as a source when the linked wiki page was created. Informational — provenance link, not retrieval-active.",
        "settings_color_key": "VIZ_EDGE_COLOR_MEMORY_WIKI",
        "fallback_color": "#ffa657",
        "role": "informational",
        "default_on": True,
    },
    # ── Informational (PC-algorithm causal discovery) ─────────────────────────
    "causal": {
        "label": "Causal",
        "description": "Causal relationship between two entity nodes, inferred by causal-discovery algorithm. Informational — causal ≠ retrieval relevance.",
        "settings_color_key": None,  # No VIZ_EDGE_COLOR_CAUSAL — renders at fallback
        "fallback_color": "#484f58",
        "role": "informational",
        "default_on": True,
    },
    # ── Informational (near-duplicate memory pairs from CLS phase) ────────────
    "memory_similarity_link": {
        "label": "Near-Duplicate",
        "description": "Near-duplicate memory pair detected by CLS phase (cosine ≥ threshold). Informational — structural dedup signal, not a retrieval edge.",
        "settings_color_key": None,
        "fallback_color": "#58a6ff",
        "role": "informational",
        "default_on": True,
    },
    # ── Retrieval-active entity typed-relations (the big hidden capability) ───
    # These power PPR (w=0.5) + spreading (w=0.3) in balanced/full profiles,
    # and the precomputed graph_prior in fast profile (v5.54.1).
    # Previously INVISIBLE in the viz — now rendered (v5.54.3).
    "co_occurrence": {
        "label": "Co-Occurrence",
        "description": "Entity co-occurrence: two entities extracted from the same memory. Core retrieval signal — feeds PPR + spreading activation + graph_prior.",
        "settings_color_key": None,
        "fallback_color": "#e8b86d",
        "role": "retrieval",
        "default_on": True,
    },
    # v5.86 VIZ Batch-2 (P0.4): "imports" + "calls" removed from the viz registry.
    # They populate only from literal source code; on a prose work-summary corpus
    # they are always empty, so advertising them made the legend lie. They remain
    # valid entity-graph relations (VALID_REL_TYPES, retrieval-active) — just not
    # surfaced in the viz. resolved_by is now genuinely populated and stays.
    "resolved_by": {
        "label": "Resolved By",
        "description": "Error resolved by an entity (error→fix pattern). Retrieval-active via entity graph.",
        "settings_color_key": None,
        "fallback_color": "#f85149",
        "role": "retrieval",
        "default_on": True,
    },
    "caused_by": {
        "label": "Caused By",
        "description": "Causal entity link (entity A caused by entity B). Retrieval-active via entity graph.",
        "settings_color_key": None,
        "fallback_color": "#ff7b72",
        "role": "retrieval",
        "default_on": True,
    },
}

# ---------------------------------------------------------------------------
# Lazy edge types (v5.54.3)
# ---------------------------------------------------------------------------
# Types that are NOT included in the default /api/graph payload.
# Fetched on-demand via /api/graph/edges?type=<type> when toggle flips ON.
# v5.87 C3: emptied — "semantic" was the only lazy type and was removed from the
# viz (see EDGE_TYPES note above). get_edges_by_type() now gates every type out.
LAZY_EDGE_TYPES: frozenset[str] = frozenset()
