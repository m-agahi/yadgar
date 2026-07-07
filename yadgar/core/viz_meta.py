"""viz_meta.py — Canonical viz metadata constants + legend builder.

Single source of truth for edge type registry and node type descriptions.
Referenced by graph_api.py (edge type keys), http.py (legend block), and
the frontend help.js renderer.

v5.50.13: extracted from scattered literals in graph_api.py / index.html.
v5.54.3: added `role` field (retrieval|display) per EDGE_CONTRACT; added
    entity typed-relation types (co_occurrence, resolved_by, caused_by;
    imports/calls also added then, but dropped from the viz in v5.86 Batch-2
    P0.4 — code-only, always empty on a prose corpus) — the retrieval-active
    entity graph now visible in viz.
v5.80 (#80 viz-fidelity-v2): renamed role "display" → "informational" to
    reflect accurate semantics (these edges carry real structural info, not
    mere decoration). Added memory_similarity_link edge type.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from yadgar._shared.config import Settings

# ---------------------------------------------------------------------------
# Edge type registry
# ---------------------------------------------------------------------------
# Keys MUST match the "type" strings emitted in graph_api.py.
# Colors come from Settings at response time (see http.py api_viz_config).
# 'causal' uses fallback #484f58 — no VIZ_EDGE_COLOR_CAUSAL setting.
#
# role (v5.54.3, per docs/EDGE_CONTRACT.md; renamed v5.80 viz-fidelity-v2):
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

# ---------------------------------------------------------------------------
# Node type descriptions (informational — shapes/colors still driven by code)
# ---------------------------------------------------------------------------
NODE_TYPES: list[dict[str, str]] = [
    {
        "key": "memory",
        "shape": "sphere",
        "color_rule": "heat gradient (blue cold → red hot)",
        "description": "An episodic memory. Color encodes recall heat: cold/blue = rarely recalled, hot/red = frequently recalled.",
    },
    {
        "key": "wiki",
        "shape": "octahedron",
        "color_rule": "category color",
        "description": "A wiki knowledge page. Shape is an octahedron; color reflects the page's category.",
    },
    {
        "key": "entity",
        "shape": "sphere",
        "color_rule": "heat gradient (blue cold → red hot)",
        "description": "A named entity extracted from memories. Color encodes heat like memory nodes.",
    },
]

# ---------------------------------------------------------------------------
# Heat description (informational)
# ---------------------------------------------------------------------------
HEAT_META: dict[str, str] = {
    "description": (
        "Memory and entity nodes carry a heat score that rises +0.05 per recall and decays over time. "
        "Wiki nodes have no heat — they are colored by category only."
    ),
    "gradient": "blue (cold, rarely recalled) → red (hot, frequently recalled)",
}

_CAT_COLOR_FALLBACK = "#8b949e"


def build_category_colors(settings: Settings) -> dict[str, str]:
    """Build category_colors by iterating WikiStore.CATEGORIES.

    New categories auto-appear with fallback grey; no hardcoded 8-key literal.
    """
    from yadgar._shared.wiki import WikiStore  # noqa: PLC0415

    return {
        cat: getattr(settings, f"VIZ_CAT_COLOR_{cat.upper()}", _CAT_COLOR_FALLBACK)
        for cat in sorted(WikiStore.CATEGORIES)
    }


def build_edge_colors(settings: Settings) -> dict[str, str]:
    """Build edge.color by iterating EDGE_TYPES, pulling colors from Settings."""
    return {
        key: (
            getattr(settings, meta["settings_color_key"], meta["fallback_color"])
            if meta["settings_color_key"]
            else meta["fallback_color"]
        )
        for key, meta in EDGE_TYPES.items()
    }


def build_legend(settings: Settings) -> dict:
    """Build the legend block for /api/viz/config.

    Returns categories, edges, node_types, heat — the only authored text in the
    system. All content flows from canonical sets (WikiStore.CATEGORIES, EDGE_TYPES,
    NODE_TYPES, HEAT_META) rather than being duplicated across files.
    """
    from yadgar._shared.wiki import WikiStore  # noqa: PLC0415

    category_colors = build_category_colors(settings)
    edge_colors = build_edge_colors(settings)

    legend_categories = [
        {
            "key": cat,
            "color": category_colors[cat],
            "label": cat.capitalize(),
            "description": f"Wiki pages classified as '{cat}'.",
        }
        for cat in sorted(WikiStore.CATEGORIES)
    ]
    legend_edges = [
        {
            "key": key,
            "color": edge_colors[key],
            "label": meta["label"],
            "description": meta["description"],
            "role": meta.get("role", "informational"),
            "default_on": meta.get("default_on", True),
            "lazy": key in LAZY_EDGE_TYPES,
        }
        for key, meta in EDGE_TYPES.items()
    ]
    legend_node_types = [
        {
            "key": nt["key"],
            "shape": nt["shape"],
            "color_rule": nt["color_rule"],
            "description": nt["description"],
        }
        for nt in NODE_TYPES
    ]
    return {
        "categories": legend_categories,
        "edges": legend_edges,
        "node_types": legend_node_types,
        "heat": HEAT_META,
    }
