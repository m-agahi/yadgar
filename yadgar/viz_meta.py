"""viz_meta.py — Canonical viz metadata constants + legend builder.

Single source of truth for edge type registry and node type descriptions.
Referenced by graph_api.py (edge type keys), http.py (legend block), and
the frontend help.js renderer.

v5.50.13: extracted from scattered literals in graph_api.py / index.html.
v5.54.3: added `role` field (retrieval|display) per EDGE_CONTRACT; added 5
    entity typed-relation types (co_occurrence, imports, calls, resolved_by,
    caused_by) — the retrieval-active entity graph now visible in viz.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from yadgar.config import Settings

# ---------------------------------------------------------------------------
# Edge type registry
# ---------------------------------------------------------------------------
# Keys MUST match the "type" strings emitted in graph_api.py.
# Colors come from Settings at response time (see http.py api_viz_config).
# 'causal' uses fallback #484f58 — no VIZ_EDGE_COLOR_CAUSAL setting.
#
# role (v5.54.3, per docs/EDGE_CONTRACT.md):
#   "retrieval" — feeds recall ranking (PPR / spreading / precomputed prior).
#                 These are the load-bearing edges; styled solid/brighter.
#   "display"   — viz only; decorative / structural teaching. Styled dashed/dimmer.
#
# Entity typed-relations (co_occurrence/imports/calls/resolved_by/caused_by):
#   role=retrieval — these power PPR + spreading in balanced/full profiles, and
#   the precomputed graph_prior in fast profile (v5.54.1). The biggest hidden
#   capability — previously invisible in the viz.
#
# default_on (v5.54.3): whether the toggle is checked by default.
#   retrieval-role edges default ON; heavy display-only (semantic) OFF.
EDGE_TYPES: dict[str, dict] = {
    # ── Display-only (semantic similarity — redundant with vector retrieval) ──
    "semantic": {
        "label": "Semantic",
        "description": "Cosine-similarity link between nodes with similar content (≥0.75 threshold). Display only — redundant with the vector signal recall already uses.",
        "settings_color_key": "VIZ_EDGE_COLOR_SEMANTIC",
        "fallback_color": "#1f6feb",
        "role": "display",
        "default_on": False,  # lazy; heavy O(n²) — off by default
    },
    # ── Display-only (temporal slot co-membership — weak signal) ─────────────
    "temporal": {
        "label": "Temporal",
        "description": "Co-occurrence in time: two memories stored within the same temporal slot. Display only — weak signal, not wired to retrieval.",
        "settings_color_key": "VIZ_EDGE_COLOR_TEMPORAL",
        "fallback_color": "#6e40c9",
        "role": "display",
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
    # ── Display-only (wiki structure) ─────────────────────────────────────────
    "wiki_crossref": {
        "label": "Wiki Link",
        "description": "Explicit cross-reference between two wiki pages (from page [[link]] syntax). Display only.",
        "settings_color_key": "VIZ_EDGE_COLOR_WIKI_CROSSREF",
        "fallback_color": "#d2a8ff",
        "role": "display",
        "default_on": True,
    },
    # ── Display-only (memory→wiki provenance) ────────────────────────────────
    "memory_wiki": {
        "label": "Mem→Wiki",
        "description": "A memory was used as a source when the linked wiki page was created. Display only.",
        "settings_color_key": "VIZ_EDGE_COLOR_MEMORY_WIKI",
        "fallback_color": "#ffa657",
        "role": "display",
        "default_on": True,
    },
    # ── Display-only (PC-algorithm causal discovery) ──────────────────────────
    "causal": {
        "label": "Causal",
        "description": "Causal relationship between two entity nodes, inferred by causal-discovery algorithm. Display only — causal ≠ retrieval relevance.",
        "settings_color_key": None,  # No VIZ_EDGE_COLOR_CAUSAL — renders at fallback
        "fallback_color": "#484f58",
        "role": "display",
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
    "imports": {
        "label": "Imports",
        "description": "Import dependency: one entity imports another (code context). Retrieval-active via entity graph.",
        "settings_color_key": None,
        "fallback_color": "#79c0ff",
        "role": "retrieval",
        "default_on": True,
    },
    "calls": {
        "label": "Calls",
        "description": "Function call relationship: one entity calls another. Retrieval-active via entity graph.",
        "settings_color_key": None,
        "fallback_color": "#56d364",
        "role": "retrieval",
        "default_on": True,
    },
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
LAZY_EDGE_TYPES: frozenset[str] = frozenset({"semantic"})

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
    from yadgar.wiki import WikiStore  # noqa: PLC0415

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
    from yadgar.wiki import WikiStore  # noqa: PLC0415

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
            "role": meta.get("role", "display"),
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
