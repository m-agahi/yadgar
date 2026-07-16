"""Backend viz-op execution package (T2 Car E3, census verdict #11).

The viz HTTP server + static assets stay core; the DB-heavy graph data
assembly runs here, next to the DB. The core ``/api/graph*`` endpoints keep
their route shells (param parsing, CORS, hook metrics) and forward via the
core ``_forward_viz`` helper to the backend ``POST /viz`` route, which
dispatches through ``run_viz_op`` — mirroring the ``/admin`` +
``run_admin_op`` forward pattern (reads-flavored twin surface).

Ops:
  graph              — full graph payload (nodes/edges/clusters) + cached
                       layout positions (attached whenever a cache row exists).
  graph_stats        — counts + top entities by heat.
  graph_edges        — on-demand lazy edge computation.
  graph_neighborhood — 1–2 hop subgraph around a node.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import yadgar._shared.runtime.state as _st
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
    from yadgar._shared.config import get_settings  # noqa: PLC0415
    from yadgar.backend.graph.graph_api import EdgeCaps, GraphAPI  # noqa: PLC0415

    storage = _get_storage()
    # viz-render-perf (Car A): per-edge-type caps resolved from settings here (at the
    # /api/graph call site) and threaded into get_full_graph. The precompute path
    # calls get_full_graph without edge_caps → uncapped full-graph layout.
    _settings = get_settings()
    edge_caps = EdgeCaps(
        transitions=int(getattr(_settings, "VIZ_MAX_TRANSITIONS", 0)),
        wiki_crossrefs=int(getattr(_settings, "VIZ_MAX_WIKI_CROSSREFS", 0)),
        causal_edges=int(getattr(_settings, "VIZ_MAX_CAUSAL_EDGES", 0)),
        relationships=int(getattr(_settings, "VIZ_MAX_RELATIONSHIPS", 0)),
        similarity_links=int(getattr(_settings, "VIZ_MAX_SIMILARITY_LINKS", 0)),
        # viz-rest #89: opt-in weak-edge render, threaded from ?include_weak.
        include_weak=bool(payload.get("include_weak", False)),
    )
    data = GraphAPI(storage).get_full_graph(
        int(payload.get("max_memories", 0)),
        int(payload.get("top_k", 8)),
        False,
        None,
        int(payload.get("max_wiki", 0)),
        int(payload.get("max_entities", 0)),
        edge_caps=edge_caps,
    )

    # viz-render-perf (Car A): attach precomputed positions (by node-id) whenever
    # a layout cache exists — unconditional now (the VIZ_PRECOMPUTED_LAYOUT_ENABLED
    # knob was removed). Empty cache → no x/y/z → the client seed-miss fallback
    # (cold d3-force layout) runs. Ran core-side pre-E3; both the cache read and
    # the attach live backend-side now.
    try:
        from yadgar.backend.graph.graph_layout import (  # noqa: PLC0415
            attach_cached_positions,
        )

        cache = storage.get_graph_layout_cache()
        attach_cached_positions(data, cache)
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


@observe(tier="boundary", metric="backend.viz.events")
def _op_events(payload: dict) -> dict:
    """Return backend SSE ring-buffer entries with ``seq > since`` (F2 relay).

    The write-path (memory_added/wiki_added/wiki_updated) and heat decay
    (heat_updated) push events into the BACKEND process's ``_event_queue`` — a
    process-local deque that the CORE ``/api/graph/events`` SSE stream cannot
    read. Core polls this op each SSE loop iteration and re-stamps the returned
    entries onto its own queue so browser clients see them (relay option (a)).

    payload: {"since": int} — the client's last-seen BACKEND seq cursor.
    Returns: {"events": [ring-buffer dicts with seq>since], "latest_seq": int}.
    ``latest_seq`` lets core seed its cursor to the head on first poll without
    replaying the whole backlog.
    """
    since = int(payload.get("since", 0))
    with _st._event_lock:
        events = [dict(e) for e in _st._event_queue if e["seq"] > since]
        latest_seq = _st._event_seq
    return {"events": events, "latest_seq": latest_seq}


# Dispatch table: op name → backend impl. Single source of truth for the /viz
# surface; the /viz route validates ``op`` against these keys (mirrors _ADMIN_OPS).
_VIZ_OPS: dict[str, Callable[[dict], dict]] = {
    "graph": _op_graph,
    "graph_stats": _op_graph_stats,
    "graph_edges": _op_graph_edges,
    "graph_neighborhood": _op_graph_neighborhood,
    "events": _op_events,
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
