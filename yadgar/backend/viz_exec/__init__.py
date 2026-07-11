"""Backend viz-op execution package (T2 Car E3, census verdict #11).

The viz HTTP server + static assets stay core; the DB-heavy graph data
assembly runs here, next to the DB. The core ``/api/graph*`` endpoints keep
their route shells (param parsing, CORS, hook metrics) and forward via the
core ``_forward_viz`` helper to the backend ``POST /viz`` route, which
dispatches through ``run_viz_op`` — mirroring the ``/admin`` +
``run_admin_op`` forward pattern (reads-flavored twin surface).

Ops:
  graph              — full graph payload (nodes/edges/clusters) + cached
                       layout positions when VIZ_PRECOMPUTED_LAYOUT_ENABLED.
  graph_stats        — counts + top entities by heat.
  graph_edges        — on-demand lazy edge computation.
  graph_neighborhood — 1–2 hop subgraph around a node.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from yadgar._shared.observability.observe import observe
from yadgar._shared.runtime.lifecycle import _get_storage

logger = logging.getLogger(__name__)


@observe(tier="boundary", metric="backend.viz.graph")
def _op_graph(payload: dict) -> dict:
    """Full graph payload. Mirrors the pre-E3 core /api/graph assembly.

    payload: {"max_memories": int, "top_k": int, "max_wiki": int,
              "max_entities": int} — caps already resolved core-side
    (query params over VIZ_MAX_* settings defaults).
    """
    from yadgar.backend.graph.graph_api import GraphAPI  # noqa: PLC0415

    storage = _get_storage()
    data = GraphAPI(storage).get_full_graph(
        int(payload.get("max_memories", 0)),
        int(payload.get("top_k", 8)),
        False,
        None,
        int(payload.get("max_wiki", 0)),
        int(payload.get("max_entities", 0)),
    )

    # v5.88: attach precomputed positions (by node-id) when the flag is on and
    # a layout cache exists. Default OFF → no x/y/z. Ran core-side pre-E3;
    # both the cache read and the attach live backend-side now.
    from yadgar._shared.config import get_settings  # noqa: PLC0415

    if getattr(get_settings(), "VIZ_PRECOMPUTED_LAYOUT_ENABLED", False):
        try:
            from yadgar.backend.graph.graph_layout import (  # noqa: PLC0415
                attach_cached_positions,
            )

            cache = storage.get_graph_layout_cache()
            attach_cached_positions(data, cache, enabled=True)
        except Exception:  # noqa: BLE001 — layout attach is best-effort
            logger.debug("attach_cached_positions failed (non-fatal)", exc_info=True)
    return data


@observe(tier="boundary", metric="backend.viz.graph_stats")
def _op_graph_stats(payload: dict) -> dict:  # noqa: ARG001 — uniform op signature
    """Graph statistics: counts + top entities by heat."""
    from yadgar.backend.graph.graph_api import GraphAPI  # noqa: PLC0415

    return GraphAPI(_get_storage()).get_graph_stats()


@observe(tier="boundary", metric="backend.viz.graph_edges")
def _op_graph_edges(payload: dict) -> dict:
    """On-demand edge computation for lazy edge types (v5.54.3).

    payload: {"edge_type": str, "max_memories": int, "top_k": int}
    """
    from yadgar.backend.graph.graph_api import GraphAPI  # noqa: PLC0415

    return GraphAPI(_get_storage()).get_edges_by_type(
        payload.get("edge_type", ""),
        int(payload.get("max_memories", 500)),
        int(payload.get("top_k", 8)),
    )


@observe(tier="boundary", metric="backend.viz.graph_neighborhood")
def _op_graph_neighborhood(payload: dict) -> dict:
    """1–2 hop subgraph around a node. payload: {"node_id": str, "hops": int}."""
    from yadgar.backend.graph.graph_api import GraphAPI  # noqa: PLC0415

    return GraphAPI(_get_storage()).get_neighborhood(
        payload.get("node_id", ""), int(payload.get("hops", 2))
    )


# Dispatch table: op name → backend impl. Single source of truth for the /viz
# surface; the /viz route validates ``op`` against these keys (mirrors _ADMIN_OPS).
_VIZ_OPS: dict[str, Callable[[dict], dict]] = {
    "graph": _op_graph,
    "graph_stats": _op_graph_stats,
    "graph_edges": _op_graph_edges,
    "graph_neighborhood": _op_graph_neighborhood,
}


def viz_ops() -> frozenset[str]:
    """Return the set of registered viz op names (I32 capability discovery)."""
    return frozenset(_VIZ_OPS)


@observe(tier="boundary", metric="backend.viz.run_viz_op")
def run_viz_op(op: str, payload: dict) -> dict:
    """Dispatch a single viz op to its backend execution body.

    Raises:
        KeyError: if ``op`` is not a registered viz op (route maps to 400).
    """
    impl = _VIZ_OPS.get(op)
    if impl is None:
        raise KeyError(f"unknown viz op: {op!r}")
    return impl(payload)
