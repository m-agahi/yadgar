"""Back-compat shim — graph_api split in two (T2 Car E3, census verdict #11).

- ``GraphAPI`` + graph data assembly → ``yadgar.backend.graph.graph_api``
  (DB-heavy compute runs next to the DB; core /api/graph* endpoints forward).
- The process/system-metrics sampler → ``yadgar.core.daemon.system_metrics``
  (it introspects the CORE daemon process via /proc).

PEP-562 shim (Car 0 #167 precedent): symbol imports from the old path keep
working; the lazy string-based importlib forward avoids a static core→backend
edge. New code must import from the split homes directly.
"""

from typing import Final

_GRAPH_TARGET: Final = "yadgar.backend.graph.graph_api"
_METRICS_TARGET: Final = "yadgar.core.daemon.system_metrics"

_TARGETS: Final[dict[str, str]] = {
    # Graph data assembly (backend)
    "EDGE_TYPES": _GRAPH_TARGET,
    "GraphAPI": _GRAPH_TARGET,
    "LAZY_EDGE_TYPES": _GRAPH_TARGET,
    "_limit_clause": _GRAPH_TARGET,
    "yadgar_graph_api_orphan_edges_dropped_total": _GRAPH_TARGET,
    "trace_span": _GRAPH_TARGET,
    "observe": _GRAPH_TARGET,
    "logger": _GRAPH_TARGET,
    "logging": _GRAPH_TARGET,
    # Process/system metrics sampler (core daemon)
    "_already_registered": _METRICS_TARGET,
    "_gc_callback": _METRICS_TARGET,
    "_gc_start_times": _METRICS_TARGET,
    "_metrics_cache": _METRICS_TARGET,
    "_metrics_sampled_at": _METRICS_TARGET,
    "_observe_dbsize_ms": _METRICS_TARGET,
    "_prev_cpu_ticks": _METRICS_TARGET,
    "_prev_cpu_time": _METRICS_TARGET,
    "_sample_cpu_pct": _METRICS_TARGET,
    "_sample_db_size": _METRICS_TARGET,
    "_sample_loadavg": _METRICS_TARGET,
    "_sample_meminfo": _METRICS_TARGET,
    "_sample_open_fds": _METRICS_TARGET,
    "_sample_rss_threads": _METRICS_TARGET,
    "gc": _METRICS_TARGET,
    "os": _METRICS_TARGET,
    "time": _METRICS_TARGET,
    "Path": _METRICS_TARGET,
    "run_metrics_sampler": _METRICS_TARGET,
    "sample_system_metrics": _METRICS_TARGET,
    "yadgar_process_cpu_percent": _METRICS_TARGET,
    "yadgar_process_open_fds": _METRICS_TARGET,
    "yadgar_process_rss_bytes": _METRICS_TARGET,
    "yadgar_python_gc_duration_ms": _METRICS_TARGET,
}


def __getattr__(name: str):
    target = _TARGETS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 shim)

    return getattr(importlib.import_module(target), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_TARGETS)
