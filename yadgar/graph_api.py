"""Graph API — assembles graph JSON for knowledge graph visualization."""

import gc
import logging
import os
import time
from pathlib import Path

from yadgar.metrics import (
    yadgar_graph_api_orphan_edges_dropped_total,
    yadgar_process_cpu_percent,
    yadgar_process_open_fds,
    yadgar_process_rss_bytes,
    yadgar_python_gc_duration_ms,
)
from yadgar.tracing import trace_span

logger = logging.getLogger(__name__)

# ── GC duration instrumentation ───────────────────────────────────────────────

_gc_start_times: dict[int, float] = {}


def _gc_callback(phase: str, info: dict) -> None:
    """Record GC collection duration into yadgar_python_gc_duration_ms histogram."""
    if phase == "start":
        _gc_start_times[info["generation"]] = time.perf_counter()
    elif phase == "stop":
        start = _gc_start_times.pop(info["generation"], None)
        if start is not None:
            duration_ms = (time.perf_counter() - start) * 1000
            yadgar_python_gc_duration_ms.labels(generation=str(info["generation"])).observe(
                duration_ms
            )


# Idempotent registration — safe across importlib.reload().
# Check by __qualname__ because reload() creates new function objects with a
# different identity, so `_gc_callback not in gc.callbacks` would always be True.
_already_registered = any(
    getattr(cb, "__qualname__", "") == _gc_callback.__qualname__ for cb in gc.callbacks
)
if not _already_registered:
    gc.callbacks.append(_gc_callback)


class GraphAPI:
    """Assembles graph data (nodes + edges) from StorageEngine for visualization."""

    def __init__(self, storage) -> None:
        self._s = storage

    # ── Public API ────────────────────────────────────────────────────────────

    @trace_span("graph_api.get_full_graph")
    def get_full_graph(
        self,
        max_memories: int = 500,
        top_k: int = 8,
        include_invalidated: bool = False,
        as_of: str | None = None,
    ) -> dict:
        """Return full graph: memory nodes with semantic, temporal, and transition edges.

        include_invalidated: when False (default), excludes invalidated KG edges.
        as_of (v5.29.0): ISO-8601 timestamp for point-in-time graph snapshot.
        """
        nodes: list[dict] = []
        edges: list[dict] = []

        # ── Memory nodes ──────────────────────────────────────────────────────
        try:
            memories = self._s._q(
                "SELECT id, content, heat, tags, directory_context, created_at, "
                "slot_index, embedding FROM memory ORDER BY heat DESC LIMIT $lim",
                {"lim": max_memories},
            )
        except Exception:
            memories = []

        embeddings_for_sem: list[tuple[str, bytes]] = []  # (node_id, bytes)
        mem_ids: set[int] = set()  # track which memories are in the graph
        slot_map: dict[int, list[tuple[int, str]]] = {}  # slot_index → [(raw_id, created_at)]

        for m in memories:
            raw_id = self._extract_id(m.get("id"))
            if raw_id is None:
                continue
            node_id = f"mem:{raw_id}"
            mem_ids.add(raw_id)
            nodes.append(
                {
                    "id": node_id,
                    "type": "memory",
                    "heat": round(float(m.get("heat") or 0), 4),
                    "label": (m.get("content") or "")[:60],
                    "content": (m.get("content") or "")[:400],
                    "tags": m.get("tags") or [],
                    "directory": m.get("directory_context") or "",
                    "created_at": str(m.get("created_at") or ""),
                }
            )
            if len(embeddings_for_sem) < 200:
                emb = m.get("embedding")
                if emb:
                    embeddings_for_sem.append((node_id, emb))

            # Collect slot assignments for temporal edges
            slot = m.get("slot_index")
            if slot is not None:
                slot_map.setdefault(int(slot), []).append((raw_id, str(m.get("created_at") or "")))

        # ── Temporal edges (memories sharing an engram slot) ──────────────────
        for _slot, members in slot_map.items():
            # Cap to 10 most recent per slot to avoid O(n^2) in large slots
            if len(members) > 10:
                members = sorted(members, key=lambda x: x[1], reverse=True)[:10]
            for i, (id_a, _) in enumerate(members):
                for id_b, _ in members[i + 1 :]:
                    edges.append(
                        {
                            "source": f"mem:{id_a}",
                            "target": f"mem:{id_b}",
                            "type": "temporal",
                        }
                    )

        # ── Transition edges (memory co-recall patterns) ──────────────────────
        try:
            transitions = self._s.get_all_transitions()
        except Exception:
            transitions = []

        for t in transitions:
            from_id = self._extract_id(t.get("from_memory_id"))
            to_id = self._extract_id(t.get("to_memory_id"))
            count = int(t.get("count") or 0)
            if from_id is None or to_id is None or count < 2:
                continue
            if from_id not in mem_ids or to_id not in mem_ids:
                continue
            edges.append(
                {
                    "source": f"mem:{from_id}",
                    "target": f"mem:{to_id}",
                    "type": "transition",
                    "count": count,
                }
            )

        # ── Wiki nodes ────────────────────────────────────────────────────────
        try:
            wiki_pages = self._s._q(
                "SELECT id, title, slug, category, tags, links, source_memory_ids, "
                "embedding, updated_at FROM wiki_page ORDER BY updated_at DESC LIMIT 200"
            )
        except Exception:
            wiki_pages = []

        wiki_slug_to_id: dict[str, str] = {}
        for wp in wiki_pages or []:
            raw_id = self._extract_id(wp.get("id"))
            if raw_id is None:
                continue
            node_id = f"wiki:{raw_id}"
            slug = wp.get("slug") or ""
            wiki_slug_to_id[slug] = node_id
            nodes.append(
                {
                    "id": node_id,
                    "type": "wiki",
                    "label": wp.get("title") or slug,
                    "slug": slug,
                    "category": wp.get("category") or "",
                    "tags": wp.get("tags") or [],
                    "updated_at": str(wp.get("updated_at") or ""),
                }
            )
            emb = wp.get("embedding")
            if emb and len(embeddings_for_sem) < 400:
                embeddings_for_sem.append((node_id, emb))

        # ── Semantic edges (memories + wikis, cosine ≥ 0.75, top-K per node) ─
        if len(embeddings_for_sem) >= 2:
            edges.extend(self._compute_semantic_edges(embeddings_for_sem, top_k=top_k))

        # ── Wiki cross-reference edges ────────────────────────────────────
        try:
            crossrefs = self._s.get_all_wiki_crossrefs()
        except Exception:
            crossrefs = []

        for cr in crossrefs:
            src = wiki_slug_to_id.get(cr.get("from_slug"))
            tgt = wiki_slug_to_id.get(cr.get("to_slug"))
            if src and tgt:
                edges.append(
                    {
                        "source": src,
                        "target": tgt,
                        "type": "wiki_crossref",
                    }
                )

        # ── Memory → Wiki edges (via source_memory_ids) ──────────────────
        for wp in wiki_pages or []:
            raw_id = self._extract_id(wp.get("id"))
            if raw_id is None:
                continue
            source_ids = wp.get("source_memory_ids") or []
            wiki_nid = f"wiki:{raw_id}"
            for mid in source_ids:
                if isinstance(mid, int) and mid in mem_ids:
                    edges.append(
                        {
                            "source": f"mem:{mid}",
                            "target": wiki_nid,
                            "type": "memory_wiki",
                        }
                    )

        # ── Entity nodes (C1/v5.31.1: required so causal edges pass orphan filter) ─
        self._assemble_entity_nodes(nodes)

        # ── Causal edges (C3: include source_memory_id for citation tracing) ──
        # C1: filter out invalidated edges by default.
        # v5.29.0: as_of parameter enables point-in-time graph snapshots.
        try:
            causal_edges_raw = self._s.get_all_causal_edges(
                include_invalidated=include_invalidated, as_of=as_of
            )
        except Exception:
            causal_edges_raw = []

        for ce in causal_edges_raw:
            src_eid = self._extract_id(ce.get("source_entity_id"))
            tgt_eid = self._extract_id(ce.get("target_entity_id"))
            if src_eid is None or tgt_eid is None:
                continue
            edge: dict = {
                "source": f"entity:{src_eid}",
                "target": f"entity:{tgt_eid}",
                "type": "causal",
                "confidence": float(ce.get("confidence") or 0.0),
                "algorithm": ce.get("algorithm") or "",
            }
            smid = ce.get("source_memory_id")
            if smid is not None:
                edge["source_memory_id"] = int(smid)
            edges.append(edge)

        # ── Orphan-edge filter (v5.10.9) ─────────────────────────────────────────
        # force-graph.min.js throws 'node not found: <id>' synchronously when any
        # link references an ID not in the node set. One orphan edge crashes the
        # entire physics simulation (tick count stays 0, all nodes clump at origin).
        # v5.31.1: entity nodes now included above; causal edges no longer orphans.
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

        return {"nodes": nodes, "edges": filtered_edges}

    @trace_span("graph_api.get_graph_stats")
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

    @trace_span("graph_api.get_neighborhood")
    def get_neighborhood(self, node_id: str, hops: int = 2) -> dict:
        """Return subgraph around a memory node."""
        nodes: list[dict] = []
        seen_nodes: set[str] = set()

        if node_id.startswith("mem:"):
            raw_id = self._extract_id(node_id[4:])
            if raw_id is not None:
                self._expand_memory(raw_id, nodes, seen_nodes)

        return {"nodes": nodes, "edges": []}

    # ── Private helpers ───────────────────────────────────────────────────────

    def _assemble_entity_nodes(self, nodes: list[dict]) -> None:
        """Fetch all entities and append entity:* node dicts to *nodes*.

        v5.31.1: entity nodes were removed in v5.0.0 monolith split.  Without
        them every causal edge references an absent node ID and is dropped by
        the orphan filter — making include_invalidated filtering unobservable
        via get_full_graph().  Restoring entity nodes fixes the orphan filter
        for causal edges while keeping all other edge types unchanged.
        """
        try:
            all_entities = self._s.get_all_entities(include_archived=True)
        except Exception:
            all_entities = []
        for ent in all_entities:
            raw_id = self._extract_id(ent.get("id"))
            if raw_id is None:
                continue
            nodes.append(
                {
                    "id": f"entity:{raw_id}",
                    "type": "entity",
                    "label": (ent.get("name") or "")[:60],
                    "heat": round(float(ent.get("heat") or 0), 4),
                }
            )

    def _expand_memory(self, raw_id: int, nodes: list, seen: set) -> None:
        try:
            m = self._s.get_memory(raw_id)
        except Exception:
            return
        if m is None:
            return
        nid = f"mem:{raw_id}"
        if nid not in seen:
            seen.add(nid)
            nodes.append(
                {
                    "id": nid,
                    "type": "memory",
                    "heat": round(float(m.get("heat") or 0), 4),
                    "label": (m.get("content") or "")[:60],
                    "content": m.get("content") or "",
                    "tags": m.get("tags") or [],
                    "directory": m.get("directory_context") or "",
                    "created_at": str(m.get("created_at") or ""),
                }
            )

    def _compute_semantic_edges(
        self,
        embeddings_list: list[tuple[str, bytes]],
        threshold: float = 0.75,
        top_k: int = 5,
    ) -> list[dict]:
        """Compute pairwise cosine similarity; return top-K edges per node above threshold."""
        try:
            import numpy as np

            ids = []
            vecs = []
            for node_id, emb_data in embeddings_list:
                try:
                    if isinstance(emb_data, (bytes, bytearray)):
                        arr = np.frombuffer(emb_data, dtype=np.float32).copy()
                    elif isinstance(emb_data, list):
                        arr = np.array(emb_data, dtype=np.float32)
                    else:
                        continue
                    if arr.size > 0:
                        ids.append(node_id)
                        vecs.append(arr)
                except Exception:
                    pass

            if len(ids) < 2:
                return []

            matrix = np.stack(vecs)
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            norms = np.where(norms == 0, 1e-10, norms)
            matrix = matrix / norms
            sim = matrix @ matrix.T
            n = len(ids)
            seen: set[tuple[int, int]] = set()
            result = []
            for i in range(n):
                # Collect top-K neighbours above threshold, sorted by similarity desc
                neighbours = sorted(
                    (
                        (float(sim[i, j]), j)
                        for j in range(n)
                        if j != i and float(sim[i, j]) >= threshold
                    ),
                    reverse=True,
                )
                for s, j in neighbours[:top_k]:
                    key = (min(i, j), max(i, j))
                    if key not in seen:
                        seen.add(key)
                        result.append(
                            {
                                "source": ids[key[0]],
                                "target": ids[key[1]],
                                "type": "semantic",
                                "similarity": round(s, 3),
                            }
                        )
            return result
        except Exception as exc:
            logger.debug("Semantic edge computation failed: %s", exc)
            return []

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


# ── System metrics (no extra deps — reads /proc) ──────────────────────────────

_metrics_cache: dict = {}
_metrics_sampled_at: float = 0.0
_prev_cpu_ticks: int = 0
_prev_cpu_time: float = 0.0


def _observe_dbsize_ms(elapsed_ms: float) -> None:
    """Record dbsize sampling duration. Non-fatal; helper keeps cyclo of caller clean."""
    try:
        from yadgar.metrics import yadgar_viz_dbsize_sample_duration_ms  # noqa: PLC0415

        yadgar_viz_dbsize_sample_duration_ms.observe(elapsed_ms)
    except Exception:
        pass


def sample_system_metrics(pid: int, db_path: str, storage: object = None) -> dict:
    """Sample system metrics from /proc and update the in-process cache.

    Args:
        pid: PID of the daemon process (for /proc reads).
        db_path: Local filesystem path to the SurrealDB directory.
        storage: Optional StorageEngine instance.  When provided *and* the
            storage is in server mode (YADGAR_DB_URL is set), db_size_mb is
            obtained via ``storage.get_db_size()`` which proxies to the embed
            service's /admin/dbsize endpoint — the local path doesn't exist in
            that topology and would always return 0.
    """
    # PR-I: heartbeat — called at the start of every sampler iteration (lifecycle.py thread)
    try:
        from yadgar.metrics import loop_heartbeat  # noqa: PLC0415

        loop_heartbeat("metrics_sampler")
    except Exception:  # noqa: BLE001
        pass

    global _metrics_cache, _metrics_sampled_at, _prev_cpu_ticks, _prev_cpu_time

    result: dict = dict(_metrics_cache)  # start with last known values

    try:
        clk_tck = os.sysconf("SC_CLK_TCK")
    except (AttributeError, ValueError) as _e:
        clk_tck = 100

    # CPU% (two-sample delta)
    try:
        with open(f"/proc/{pid}/stat") as fh:
            parts = fh.read().split()
        # Fields 13=utime, 14=stime (0-indexed)
        cpu_ticks = int(parts[13]) + int(parts[14])
        now = time.monotonic()
        if _prev_cpu_time > 0:
            elapsed = now - _prev_cpu_time
            delta = cpu_ticks - _prev_cpu_ticks
            cpu_pct = round(delta / clk_tck / max(elapsed, 0.001) * 100, 1)
        else:
            cpu_pct = 0.0
        _prev_cpu_ticks = cpu_ticks
        _prev_cpu_time = now
        result["daemon_cpu_pct"] = cpu_pct
    except Exception:
        result.setdefault("daemon_cpu_pct", 0.0)

    # RSS + thread count from /proc/{pid}/status
    rss_kb = 0
    threads = 0
    try:
        with open(f"/proc/{pid}/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    rss_kb = int(line.split()[1])
                elif line.startswith("Threads:"):
                    threads = int(line.split()[1])
    except Exception:
        pass
    result["daemon_rss_mb"] = round(rss_kb / 1024, 1)
    result["rss_bytes"] = rss_kb * 1024
    result["daemon_threads"] = threads

    # Open file descriptors (self — /proc/self/fd is always accessible)
    try:
        result["open_fds"] = len(os.listdir("/proc/self/fd"))
    except Exception:
        result.setdefault("open_fds", 0)

    # System RAM
    total_ram_kb = avail_ram_kb = 0
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    total_ram_kb = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    avail_ram_kb = int(line.split()[1])
    except Exception:
        pass
    result["system_ram_total_mb"] = round(total_ram_kb / 1024, 1)
    result["system_ram_available_mb"] = round(avail_ram_kb / 1024, 1)

    # Load average
    try:
        with open("/proc/loadavg") as fh:
            la = fh.read().split()
        result["load_avg_1m"] = float(la[0])
        result["load_avg_5m"] = float(la[1])
        result["load_avg_15m"] = float(la[2])
    except Exception:
        result.setdefault("load_avg_1m", 0.0)
        result.setdefault("load_avg_5m", 0.0)
        result.setdefault("load_avg_15m", 0.0)

    # DB directory size — use storage.get_db_size() in server mode so we hit the
    # embed-service proxy rather than walking a path that doesn't exist locally.
    _db_size_set = False
    _dbsize_t0 = time.time()
    if storage is not None:
        _db_url = getattr(storage, "_db_url", None)
        if _db_url is not None:
            try:
                size_data = storage.get_db_size()
                size_bytes = size_data.get("db_size_bytes", 0)
                result["db_size_mb"] = round(size_bytes / 1024 / 1024, 1)
                _db_size_set = True
            except Exception:
                pass

    if not _db_size_set:
        try:
            db_dir = Path(db_path).expanduser()
            if db_dir.is_dir():
                size_bytes = sum(f.stat().st_size for f in db_dir.rglob("*") if f.is_file())
                result["db_size_mb"] = round(size_bytes / 1024 / 1024, 1)
            else:
                result["db_size_mb"] = 0.0
        except Exception:
            result.setdefault("db_size_mb", 0.0)

    # P11: observe dbsize sampling duration (non-fatal; bare call avoids cyclo branch).
    _dbsize_elapsed_ms = (time.time() - _dbsize_t0) * 1000.0
    _observe_dbsize_ms(_dbsize_elapsed_ms)

    result["sampled_at"] = time.time()
    _metrics_cache = result
    _metrics_sampled_at = time.time()

    # ── Bridge: push sampled values into Prometheus gauges ────────────────────
    yadgar_process_rss_bytes.set(result.get("rss_bytes", 0))
    yadgar_process_cpu_percent.set(result.get("daemon_cpu_pct", 0.0))
    yadgar_process_open_fds.set(result.get("open_fds", 0))

    return result


def run_metrics_sampler(pid: int, db_path: str, interval: float = 5.0) -> None:
    """Background thread: sample system metrics every `interval` seconds."""
    # First sample to prime the CPU delta baseline
    sample_system_metrics(pid, db_path)
    while True:
        time.sleep(interval)
        try:
            sample_system_metrics(pid, db_path)
        except Exception:
            pass
