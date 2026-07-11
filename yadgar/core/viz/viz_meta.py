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

# T2 Car C: category registry read from the wiki CONTRACT (not the WikiStore
# impl) — viz needs only the canonical set, never the store.
from yadgar._shared.wiki.contract import CATEGORIES as WIKI_CATEGORIES

if TYPE_CHECKING:
    from yadgar._shared.config import Settings

# ---------------------------------------------------------------------------
# Edge type registry — MOVED to yadgar._shared.contracts.viz (T2 Car E3).
# The registry is DUAL: the backend GraphAPI stamps edge `role` fields from it
# and the core legend builder styles from it. Re-exported here for back-compat.
# ---------------------------------------------------------------------------
from yadgar._shared.contracts.viz import EDGE_TYPES, LAZY_EDGE_TYPES  # noqa: E402

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
    """Build category_colors by iterating the canonical wiki CATEGORIES registry.

    New categories auto-appear with fallback grey; no hardcoded 8-key literal.
    """
    return {
        cat: getattr(settings, f"VIZ_CAT_COLOR_{cat.upper()}", _CAT_COLOR_FALLBACK)
        for cat in sorted(WIKI_CATEGORIES)
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
    system. All content flows from canonical sets (wiki contract CATEGORIES,
    EDGE_TYPES, NODE_TYPES, HEAT_META) rather than being duplicated across files.
    """
    category_colors = build_category_colors(settings)
    edge_colors = build_edge_colors(settings)

    legend_categories = [
        {
            "key": cat,
            "color": category_colors[cat],
            "label": cat.capitalize(),
            "description": f"Wiki pages classified as '{cat}'.",
        }
        for cat in sorted(WIKI_CATEGORIES)
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
