"""yadgar.backend.graph — knowledge-graph data assembly + layout compute.

T2 Car E3 (census verdict #11): moved from ``yadgar.core.graph`` — the
DB-heavy graph assembly and the force-layout compute run next to the DB.
Core /api/graph* endpoints forward via POST /viz (``backend.viz_exec``);
the layout precompute runs inside the backend consolidation full/nightly
cycle (``backend.consolidation``).

  graph_api.py    — GraphAPI (viz graph assembly)
  graph_layout.py — cached force-layout compute + position attachment

The process/system-metrics sampler that historically shared graph_api.py
lives in ``yadgar.core.daemon.system_metrics`` (core-process introspection).
"""
