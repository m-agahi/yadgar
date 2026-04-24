"""Graph API — assembles graph JSON for knowledge graph visualization."""

import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)


class GraphAPI:
    """Assembles graph data (nodes + edges) from StorageEngine for visualization."""

    def __init__(self, storage) -> None:
        self._s = storage

    # ── Public API ────────────────────────────────────────────────────────────

    def get_full_graph(self, max_memories: int = 500, top_k: int = 5) -> dict:
        """Return full graph: nodes (memories + entities) and edges.

        Memory nodes: id="mem:{id}", type="memory"
        Entity nodes: id="ent:{id}", type="entity"
        Edge types: "kg", "causal", "semantic"
        """
        nodes: list[dict] = []
        edges: list[dict] = []

        # ── Memory nodes ──────────────────────────────────────────────────────
        try:
            memories = self._s._q(
                "SELECT id, content, heat, tags, directory_context, created_at, "
                "embedding FROM memory ORDER BY heat DESC LIMIT $lim",
                {"lim": max_memories},
            )
        except Exception:
            memories = []

        ent_id_map: dict[int, str] = {}  # raw int id → "ent:{id}"
        embeddings_for_sem: list[tuple[str, bytes]] = []  # (node_id, bytes)

        for m in memories:
            raw_id = self._extract_id(m.get("id"))
            if raw_id is None:
                continue
            node_id = f"mem:{raw_id}"
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

        # ── Entity nodes ──────────────────────────────────────────────────────
        try:
            entities = self._s.get_all_entities(min_heat=0.0)
        except Exception:
            entities = []

        for e in entities:
            raw_id = self._extract_id(e.get("id"))
            if raw_id is None:
                continue
            node_id = f"ent:{raw_id}"
            ent_id_map[raw_id] = node_id
            nodes.append(
                {
                    "id": node_id,
                    "type": "entity",
                    "heat": round(float(e.get("heat") or 0), 4),
                    "label": e.get("name") or "",
                    "name": e.get("name") or "",
                    "entity_type": e.get("entity_type") or "concept",
                }
            )

        # ── KG relationship edges ─────────────────────────────────────────────
        try:
            rels = self._s._q("SELECT * FROM relationship")
        except Exception:
            rels = []

        for r in rels:
            src_id = self._extract_id(r.get("source_entity_id") or r.get("source_id"))
            tgt_id = self._extract_id(r.get("target_entity_id") or r.get("target_id"))
            if src_id is None or tgt_id is None:
                continue
            src_nid = ent_id_map.get(src_id)
            tgt_nid = ent_id_map.get(tgt_id)
            if src_nid and tgt_nid:
                edges.append(
                    {
                        "source": src_nid,
                        "target": tgt_nid,
                        "type": "kg",
                        "label": r.get("relationship_type") or r.get("rel_type") or "",
                        "weight": round(float(r.get("weight") or r.get("confidence") or 1.0), 3),
                    }
                )

        # ── Causal edges ──────────────────────────────────────────────────────
        try:
            causal = self._s.get_all_causal_edges()
        except Exception:
            causal = []

        for c in causal:
            src_id = self._extract_id(c.get("source_entity_id") or c.get("source_id"))
            tgt_id = self._extract_id(c.get("target_entity_id") or c.get("target_id"))
            if src_id is None or tgt_id is None:
                continue
            src_nid = ent_id_map.get(src_id)
            tgt_nid = ent_id_map.get(tgt_id)
            if src_nid and tgt_nid:
                edges.append(
                    {
                        "source": src_nid,
                        "target": tgt_nid,
                        "type": "causal",
                        "confidence": round(float(c.get("confidence") or 1.0), 3),
                    }
                )

        # ── Semantic edges (top-200 memories, cosine ≥ 0.75, top-K per node) ──
        if len(embeddings_for_sem) >= 2:
            edges.extend(self._compute_semantic_edges(embeddings_for_sem, top_k=top_k))

        return {"nodes": nodes, "edges": edges}

    def get_graph_stats(self) -> dict:
        """Return graph statistics: counts, density, top entities by heat."""
        try:
            mem_count = (self._s._q("SELECT count() FROM memory GROUP ALL") or [{}])[0].get(
                "count", 0
            )
            ent_count = (self._s._q("SELECT count() FROM entity GROUP ALL") or [{}])[0].get(
                "count", 0
            )
            rel_count = (self._s._q("SELECT count() FROM relationship GROUP ALL") or [{}])[0].get(
                "count", 0
            )
            causal_count = (self._s._q("SELECT count() FROM causal_dag_edge GROUP ALL") or [{}])[
                0
            ].get("count", 0)
            top_entities = self._s._q(
                "SELECT name, entity_type, heat FROM entity ORDER BY heat DESC LIMIT 10"
            )
        except Exception as exc:
            logger.debug("graph_stats error: %s", exc)
            return {}

        return {
            "memory_count": mem_count,
            "entity_count": ent_count,
            "relationship_count": rel_count,
            "causal_edge_count": causal_count,
            "top_entities": top_entities or [],
        }

    def get_neighborhood(self, node_id: str, hops: int = 2) -> dict:
        """Return 1–2 hop subgraph around a node."""
        nodes: list[dict] = []
        edges: list[dict] = []
        seen_nodes: set[str] = set()
        seen_edges: set[str] = set()

        if node_id.startswith("mem:"):
            raw_id = self._extract_id(node_id[4:])
            if raw_id is not None:
                self._expand_memory(raw_id, nodes, seen_nodes)
        elif node_id.startswith("ent:"):
            raw_id = self._extract_id(node_id[4:])
            if raw_id is not None:
                self._expand_entity(raw_id, nodes, edges, seen_nodes, seen_edges, hops, 0)

        return {"nodes": nodes, "edges": edges}

    # ── Private helpers ───────────────────────────────────────────────────────

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

    def _expand_entity(
        self,
        ent_id: int,
        nodes: list,
        edges: list,
        seen_nodes: set,
        seen_edges: set,
        max_hops: int,
        depth: int,
    ) -> None:
        if depth > max_hops:
            return
        try:
            e = self._s.get_entity_by_id(ent_id)
        except Exception:
            return
        if e is None:
            return
        raw_id = self._extract_id(e.get("id"))
        if raw_id is None:
            return
        nid = f"ent:{raw_id}"
        if nid in seen_nodes:
            return
        seen_nodes.add(nid)
        nodes.append(
            {
                "id": nid,
                "type": "entity",
                "heat": round(float(e.get("heat") or 0), 4),
                "label": e.get("name") or "",
                "name": e.get("name") or "",
                "entity_type": e.get("entity_type") or "concept",
            }
        )
        if depth >= max_hops:
            return
        try:
            rels = self._s.get_relationships_for_entity(raw_id)
        except Exception:
            rels = []
        for r in rels:
            src_id = self._extract_id(r.get("source_entity_id") or r.get("source_id"))
            tgt_id = self._extract_id(r.get("target_entity_id") or r.get("target_id"))
            if src_id is None or tgt_id is None:
                continue
            edge_key = (
                f"{min(src_id, tgt_id)}-{max(src_id, tgt_id)}-{r.get('relationship_type', '')}"
            )
            if edge_key not in seen_edges:
                seen_edges.add(edge_key)
                edges.append(
                    {
                        "source": f"ent:{src_id}",
                        "target": f"ent:{tgt_id}",
                        "type": "kg",
                        "label": r.get("relationship_type") or "",
                    }
                )
            next_id = tgt_id if src_id == raw_id else src_id
            self._expand_entity(next_id, nodes, edges, seen_nodes, seen_edges, max_hops, depth + 1)

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
            for node_id, emb_bytes in embeddings_list:
                try:
                    arr = np.frombuffer(emb_bytes, dtype=np.float32).copy()
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
                # Collect all neighbours above threshold, sorted by similarity desc
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
        """Extract numeric ID from a SurrealDB record ID (e.g. 'entity:42' → 42)."""
        if raw is None:
            return None
        if isinstance(raw, int):
            return raw
        s = str(raw)
        if ":" in s:
            s = s.rsplit(":", 1)[-1]
        try:
            return int(s)
        except (ValueError, TypeError):
            return None


# ── System metrics (no extra deps — reads /proc) ──────────────────────────────

_metrics_cache: dict = {}
_metrics_sampled_at: float = 0.0
_prev_cpu_ticks: int = 0
_prev_cpu_time: float = 0.0


def sample_system_metrics(pid: int, db_path: str) -> dict:
    """Sample system metrics from /proc and update the in-process cache."""
    global _metrics_cache, _metrics_sampled_at, _prev_cpu_ticks, _prev_cpu_time

    result: dict = dict(_metrics_cache)  # start with last known values

    try:
        clk_tck = os.sysconf("SC_CLK_TCK")
    except (AttributeError, ValueError):
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
    result["daemon_threads"] = threads

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

    # DB directory size
    try:
        db_dir = Path(db_path).expanduser()
        if db_dir.is_dir():
            size_bytes = sum(f.stat().st_size for f in db_dir.rglob("*") if f.is_file())
            result["db_size_mb"] = round(size_bytes / 1024 / 1024, 1)
        else:
            result["db_size_mb"] = 0.0
    except Exception:
        result.setdefault("db_size_mb", 0.0)

    result["sampled_at"] = time.time()
    _metrics_cache = result
    _metrics_sampled_at = time.time()
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
