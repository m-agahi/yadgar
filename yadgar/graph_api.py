"""Graph API — assembles graph JSON for knowledge graph visualization.

v5.54.3: entity typed-relation edges (co_occurrence/resolved_by/caused_by;
imports/calls dropped in v5.86 Batch-2 P0.4 — code-only, empty on prose)
now included in the default /api/graph payload with role="retrieval" sourced from
EDGE_TYPES (viz_meta.py). All edges carry a `role` field.

v5.87 (C3): semantic edges removed entirely — they were lazy/off-by-default,
O(n²) KNN, and informational (redundant with the vector signal recall uses). The
legend toggle did nothing useful, so the edge type was dropped from EDGE_TYPES +
LAZY_EDGE_TYPES and its on-demand compute path (_get/_compute_semantic_edges,
_parse_embedding_vectors, _deduplicated_edges) was deleted.

v5.80 (#80 viz-fidelity-v2): role vocabulary renamed display→informational in viz_meta.
clusters[] added to get_full_graph() payload (real memory_cluster rows via
get_memory_clusters() + get_cluster_members()). memory_similarity_link edges added
(_build_similarity_link_edges) with role="informational".
"""

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
from yadgar.viz_meta import EDGE_TYPES, LAZY_EDGE_TYPES

logger = logging.getLogger(__name__)

# ── GC duration instrumentation ───────────────────────────────────────────────

_gc_start_times: dict[int, float] = {}


def _gc_callback(phase: str, info: dict) -> None:
    """Record GC collection duration into yadgar_python_gc_duration_ms histogram.

    Shutdown guard: at interpreter teardown module globals (time,
    _gc_start_times, yadgar_python_gc_duration_ms) are set to None before GC
    finishes draining callbacks.  Accessing attributes on None raises
    AttributeError — surfaced as "Exception ignored while calling GC callback"
    in journald and can cause a non-zero exit code.  Return immediately when
    any critical global is None.
    """
    # Must be first — any attribute access below this line may raise if None.
    if time is None or _gc_start_times is None:
        return
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
        """Return full graph: memory + wiki + entity nodes with typed edges.

        include_invalidated: when False (default), excludes invalidated KG edges.
        as_of (v5.29.0): ISO-8601 timestamp for point-in-time graph snapshot.

        v5.54.3: entity typed-relation edges added; all edges carry `role`
        field sourced from EDGE_TYPES. (v5.87 C3: semantic edge type removed.)
        """
        nodes: list[dict] = []
        edges: list[dict] = []

        # ── Memory nodes + slot map ───────────────────────────────────────────
        mem_ids, slot_map, wiki_refs_map = self._assemble_memory_nodes(nodes, max_memories)

        # ── Temporal edges ────────────────────────────────────────────────────
        edges.extend(self._build_temporal_edges(slot_map))

        # ── Transition edges ──────────────────────────────────────────────────
        transition_edges, weak_edges_hidden = self._build_transition_edges(mem_ids)
        edges.extend(transition_edges)

        # ── Wiki nodes ────────────────────────────────────────────────────────
        _wiki_pages, wiki_slug_to_id = self._assemble_wiki_nodes(nodes)

        # ── Wiki cross-reference edges ────────────────────────────────────────
        edges.extend(self._build_wiki_crossref_edges(wiki_slug_to_id))

        # ── Memory → Wiki edges (P2.1: reverse memory.wiki_refs bridge) ───────
        edges.extend(self._build_memory_wiki_edges(wiki_refs_map, wiki_slug_to_id))

        # ── Entity nodes (required so entity edges pass orphan filter) ────────
        self._assemble_entity_nodes(nodes)

        # ── Causal edges (PC-algorithm) ───────────────────────────────────────
        edges.extend(self._build_causal_edges(include_invalidated, as_of))

        # ── Entity typed-relation edges (v5.54.3 — retrieval-active, was invisible) ─
        edges.extend(self._build_entity_rel_edges())

        # ── Memory similarity-link edges (v5.80 — informational near-duplicate links) ─
        edges.extend(self._build_similarity_link_edges(mem_ids))

        # ── Orphan-edge filter (v5.10.9) ──────────────────────────────────────
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

        # ── Cluster payload (v5.80 — real memory_cluster rows) ────────────────
        clusters = self._build_clusters_payload(mem_ids)

        return {
            "nodes": nodes,
            "edges": filtered_edges,
            "weak_edges_hidden": weak_edges_hidden,  # F4 affordance — never silently drop DB truth
            "clusters": clusters,  # BC-VZ-R3: real memory_cluster rows (informational)
        }

    def _assemble_memory_nodes(
        self, nodes: list[dict], max_memories: int
    ) -> tuple[set[int], dict[int, list[tuple[int, str]]], dict[int, list[str]]]:
        """Fetch memory rows, append node dicts.

        Returns (mem_ids, slot_map, wiki_refs_map). wiki_refs_map is
        {mem_id: [wiki_slug, ...]} sourced from the memory.wiki_refs column —
        the reverse of wiki_page.source_memory_ids (which is always empty on
        every write path). Used by _build_memory_wiki_edges (P2.1).
        """
        try:
            memories = self._s._q(
                "SELECT id, content, heat, tags, directory_context, created_at, "
                "slot_index, embedding, cluster_id, wiki_refs FROM memory "
                "ORDER BY heat DESC LIMIT $lim",
                {"lim": max_memories},
            )
        except Exception:
            memories = []
        mem_ids: set[int] = set()
        slot_map: dict[int, list[tuple[int, str]]] = {}
        wiki_refs_map: dict[int, list[str]] = {}
        for m in memories:
            raw_id = self._extract_id(m.get("id"))
            if raw_id is None:
                continue
            node_id = f"mem:{raw_id}"
            mem_ids.add(raw_id)
            refs = m.get("wiki_refs") or []
            if refs:
                wiki_refs_map[raw_id] = [str(r) for r in refs]
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
                    # P2.3: cluster_id (column on the memory row) → viz cluster tint
                    "cluster_id": self._extract_id(m.get("cluster_id")),
                }
            )
            slot = m.get("slot_index")
            if slot is not None:
                slot_map.setdefault(int(slot), []).append((raw_id, str(m.get("created_at") or "")))
        return mem_ids, slot_map, wiki_refs_map

    def _build_temporal_edges(self, slot_map: dict[int, list[tuple[int, str]]]) -> list[dict]:
        """Build temporal edges from slot_map (memories sharing an engram slot)."""
        role = EDGE_TYPES.get("temporal", {}).get("role", "informational")
        result = []
        for _slot, members in slot_map.items():
            if len(members) > 10:
                members = sorted(members, key=lambda x: x[1], reverse=True)[:10]
            for i, (id_a, _) in enumerate(members):
                for id_b, _ in members[i + 1 :]:
                    result.append(
                        {
                            "source": f"mem:{id_a}",
                            "target": f"mem:{id_b}",
                            "type": "temporal",
                            "role": role,
                        }
                    )
        return result

    def _build_transition_edges(self, mem_ids: set[int]) -> tuple[list[dict], int]:
        """Build transition (co-recall) edges from memory_transition table.

        Returns (edges, weak_hidden) where weak_hidden is the count of count<2
        transitions that exist in the DB but are excluded from the payload.
        The caller surfaces this as 'weak_edges_hidden' in the graph response
        (F4 fidelity affordance — never silently drop DB truth).
        """
        role = EDGE_TYPES.get("transition", {}).get("role", "retrieval")
        try:
            transitions = self._s.get_all_transitions()
        except Exception:
            transitions = []
        result = []
        weak_hidden = 0
        for t in transitions:
            from_id = self._extract_id(t.get("from_memory_id"))
            to_id = self._extract_id(t.get("to_memory_id"))
            count = int(t.get("count") or 0)
            if from_id is None or to_id is None:
                continue
            if from_id not in mem_ids or to_id not in mem_ids:
                continue
            if count < 2:
                # F4: don't silently drop — track for affordance
                weak_hidden += 1
                continue
            result.append(
                {
                    "source": f"mem:{from_id}",
                    "target": f"mem:{to_id}",
                    "type": "transition",
                    "count": count,
                    "role": role,
                }
            )
        return result, weak_hidden

    def _assemble_wiki_nodes(self, nodes: list[dict]) -> tuple[list[dict], dict[str, str]]:
        """Fetch wiki pages, append node dicts, return (wiki_pages, wiki_slug_to_id)."""
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
        return wiki_pages or [], wiki_slug_to_id

    def _build_wiki_crossref_edges(self, wiki_slug_to_id: dict[str, str]) -> list[dict]:
        """Build wiki cross-reference edges from wiki_crossref table."""
        role = EDGE_TYPES.get("wiki_crossref", {}).get("role", "informational")
        try:
            crossrefs = self._s.get_all_wiki_crossrefs()
        except Exception:
            crossrefs = []
        result = []
        for cr in crossrefs:
            src = wiki_slug_to_id.get(cr.get("from_slug"))
            tgt = wiki_slug_to_id.get(cr.get("to_slug"))
            if src and tgt:
                result.append({"source": src, "target": tgt, "type": "wiki_crossref", "role": role})
        return result

    def _build_memory_wiki_edges(
        self, wiki_refs_map: dict[int, list[str]], wiki_slug_to_id: dict[str, str]
    ) -> list[dict]:
        """Build memory→wiki edges from the reverse memory.wiki_refs field (P2.1).

        v5.86 VIZ Batch-2 (P2.1): the old path read wiki_page.source_memory_ids,
        which is always empty/None on every wiki write path → the bridge was dead.
        WikiStore._link_memories writes the reverse side (memory.wiki_refs holds
        the linked page slugs), so we build the bridge from there. Slugs whose
        page isn't in the loaded node set (wiki_slug_to_id) are skipped — the
        orphan filter would drop them anyway.
        """
        role = EDGE_TYPES.get("memory_wiki", {}).get("role", "informational")
        result = []
        for mid, slugs in wiki_refs_map.items():
            for slug in slugs:
                wiki_nid = wiki_slug_to_id.get(slug)
                if wiki_nid is None:
                    continue
                result.append(
                    {
                        "source": f"mem:{mid}",
                        "target": wiki_nid,
                        "type": "memory_wiki",
                        "role": role,
                    }
                )
        return result

    def _build_causal_edges(
        self, include_invalidated: bool = False, as_of: str | None = None
    ) -> list[dict]:
        """Build PC-algorithm causal edges from causal_edge table.

        C1: filter out invalidated edges by default.
        v5.29.0: as_of parameter enables point-in-time graph snapshots.
        """
        role = EDGE_TYPES.get("causal", {}).get("role", "informational")
        try:
            causal_edges_raw = self._s.get_all_causal_edges(
                include_invalidated=include_invalidated, as_of=as_of
            )
        except Exception:
            causal_edges_raw = []
        result = []
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
                "role": role,
            }
            smid = ce.get("source_memory_id")
            if smid is not None:
                edge["source_memory_id"] = int(smid)
            result.append(edge)
        return result

    def _build_entity_rel_edges(self) -> list[dict]:
        """Build entity typed-relation edges (v5.54.3 — the big hidden capability).

        co_occurrence/resolved_by/caused_by power PPR + spreading + graph_prior
        in retrieval. Previously INVISIBLE in the viz. (v5.86 Batch-2 P0.4 dropped
        imports/calls — code-only relations, always empty on a prose corpus.)
        Uses get_relationships_by_types — avoids the PC-algorithm causal edges
        (separate path via _build_causal_edges / get_all_causal_edges).
        """
        # v5.86 VIZ Batch-2 (P0.4): imports/calls dropped — code-only relations,
        # always empty on a prose corpus, made the legend lie. resolved_by is now
        # genuinely populated (extractor emits the solution entity); kept.
        _ENTITY_REL_TYPES = ["co_occurrence", "resolved_by", "caused_by"]
        try:
            entity_rels = self._s.get_relationships_by_types(_ENTITY_REL_TYPES)
        except Exception:
            entity_rels = []
        result = []
        for rel in entity_rels:
            src_eid = self._extract_id(rel.get("source_entity_id"))
            tgt_eid = self._extract_id(rel.get("target_entity_id"))
            rel_type = rel.get("relationship_type") or ""
            if src_eid is None or tgt_eid is None or rel_type not in EDGE_TYPES:
                continue
            result.append(
                {
                    "source": f"entity:{src_eid}",
                    "target": f"entity:{tgt_eid}",
                    "type": rel_type,
                    "weight": float(rel.get("weight") or 1.0),
                    "role": EDGE_TYPES[rel_type].get("role", "retrieval"),
                }
            )
        return result

    def _build_similarity_link_edges(self, mem_ids: set[int]) -> list[dict]:
        """Build memory_similarity_link edges from CLS-phase near-duplicate links.

        v5.80 (#80 viz-fidelity-v2): first viz consumer of memory_similarity_link.
        role="informational" — structural dedup signal, not a retrieval edge.
        Only emits edges where both endpoints are in the current node set.
        """
        role = EDGE_TYPES.get("memory_similarity_link", {}).get("role", "informational")
        try:
            links = self._s.get_all_memory_similarity_links()
        except Exception:
            links = []
        result = []
        for lnk in links:
            src_id = self._extract_id(lnk.get("source_memory_id"))
            tgt_id = self._extract_id(lnk.get("target_memory_id"))
            if src_id is None or tgt_id is None:
                continue
            if src_id not in mem_ids or tgt_id not in mem_ids:
                continue
            result.append(
                {
                    "source": f"mem:{src_id}",
                    "target": f"mem:{tgt_id}",
                    "type": "memory_similarity_link",
                    "weight": float(lnk.get("weight") or 0.0),
                    "role": role,
                }
            )
        return result

    def _build_clusters_payload(self, mem_ids: set[int]) -> list[dict]:
        """Assemble clusters[] from real memory_cluster rows.

        v5.80 (#80 viz-fidelity-v2): flips memory_cluster viz-consumption from
        DORMANT to LIVE. Queries get_memory_clusters() + get_cluster_members(cid).
        member_node_ids is intersected with rendered mem_ids so the frontend can
        safely render a convex hull over present nodes.

        v5.86 VIZ Batch-2 (P2.2): each cluster also carries member_count — the REAL
        pre-intersection DB member count. Members may be off-screen (outside the
        top-heat node cap), so member_node_ids can be empty while the cluster is
        non-empty; the sidebar uses member_count so real clusters aren't shown empty.

        Returns [] if no clusters exist or storage is unavailable.
        """
        try:
            cluster_rows = self._s.get_memory_clusters()
        except Exception:
            return []
        result = []
        for cr in cluster_rows:
            raw_id = self._extract_id(cr.get("id"))
            if raw_id is None:
                continue
            try:
                member_int_ids = self._s.get_cluster_members(raw_id)
            except Exception:
                member_int_ids = []
            # Intersect with rendered node set — only emit members visible in this graph
            member_node_ids = [f"mem:{mid}" for mid in member_int_ids if mid in mem_ids]
            result.append(
                {
                    "id": raw_id,
                    "source": "memory_cluster",
                    "label": cr.get("name") or f"cluster:{raw_id}",
                    "level": int(cr.get("level") or 0),
                    "member_node_ids": member_node_ids,
                    # P2.2: REAL member count from the DB (pre-intersection). Members
                    # may be off-screen (outside the top-heat node cap) so member_node_ids
                    # can be empty while the cluster is non-empty — the sidebar needs the
                    # true count, otherwise 761/769 clusters render as empty.
                    "member_count": len(member_int_ids),
                }
            )
        return result

    @trace_span("graph_api.get_edges_by_type")
    def get_edges_by_type(
        self,
        edge_type: str,
        max_memories: int = 500,
        top_k: int = 8,
    ) -> dict:
        """On-demand edge computation for lazy edge types.

        Generic gate for any edge type in LAZY_EDGE_TYPES. v5.87 C3: that set is
        now empty (semantic, its only member, was removed) so every type returns
        the not-lazy-computed error. Kept for future lazy edge types.

        Returns {"edges": [...]} (no nodes — caller merges into existing graph).
        """
        if edge_type not in LAZY_EDGE_TYPES:
            return {"edges": [], "error": f"Edge type '{edge_type}' is not lazy-computed."}

        # v5.87 C3: LAZY_EDGE_TYPES is now empty (semantic removed) — this point
        # is unreachable. Kept as a generic gate for any future lazy edge type.
        return {"edges": []}

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


def _sample_cpu_pct(pid: int, clk_tck: int) -> float:
    """Read /proc/<pid>/stat and return CPU% via two-sample delta against module globals."""
    global _prev_cpu_ticks, _prev_cpu_time
    with open(f"/proc/{pid}/stat") as fh:
        parts = fh.read().split()
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
    return cpu_pct


def _sample_rss_threads(pid: int) -> tuple[int, int]:
    """Read /proc/<pid>/status; return (rss_kb, threads)."""
    rss_kb = 0
    threads = 0
    with open(f"/proc/{pid}/status") as fh:
        for line in fh:
            if line.startswith("VmRSS:"):
                rss_kb = int(line.split()[1])
            elif line.startswith("Threads:"):
                threads = int(line.split()[1])
    return rss_kb, threads


def _sample_open_fds() -> int:
    """Count open file descriptors via /proc/self/fd."""
    return len(os.listdir("/proc/self/fd"))


def _sample_meminfo() -> tuple[int, int]:
    """Read /proc/meminfo; return (total_ram_kb, avail_ram_kb)."""
    total_ram_kb = avail_ram_kb = 0
    with open("/proc/meminfo") as fh:
        for line in fh:
            if line.startswith("MemTotal:"):
                total_ram_kb = int(line.split()[1])
            elif line.startswith("MemAvailable:"):
                avail_ram_kb = int(line.split()[1])
    return total_ram_kb, avail_ram_kb


def _sample_loadavg() -> tuple[float, float, float]:
    """Read /proc/loadavg; return (load_1m, load_5m, load_15m)."""
    with open("/proc/loadavg") as fh:
        la = fh.read().split()
    return float(la[0]), float(la[1]), float(la[2])


def _sample_db_size(storage: object, db_path: str) -> float:
    """Return db_size_mb — via storage proxy in server mode, or path walk otherwise."""
    if storage is not None:
        _db_url = getattr(storage, "_db_url", None)
        if _db_url is not None:
            try:
                size_data = storage.get_db_size()  # type: ignore[union-attr]
                size_bytes = size_data.get("db_size_bytes", 0)
                return round(size_bytes / 1024 / 1024, 1)
            except Exception:
                pass
    try:
        db_dir = Path(db_path).expanduser()
        if db_dir.is_dir():
            size_bytes = sum(f.stat().st_size for f in db_dir.rglob("*") if f.is_file())
            return round(size_bytes / 1024 / 1024, 1)
    except Exception:
        pass
    return 0.0


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

    global _metrics_cache, _metrics_sampled_at

    result: dict = dict(_metrics_cache)  # start with last known values

    try:
        clk_tck = os.sysconf("SC_CLK_TCK")
    except (AttributeError, ValueError):  # fmt: skip
        clk_tck = 100

    # CPU% (two-sample delta via /proc/<pid>/stat; Fields 13=utime, 14=stime)
    try:
        result["daemon_cpu_pct"] = _sample_cpu_pct(pid, clk_tck)
    except Exception:
        result.setdefault("daemon_cpu_pct", 0.0)

    # RSS + thread count from /proc/{pid}/status
    try:
        rss_kb, threads = _sample_rss_threads(pid)
    except Exception:
        rss_kb, threads = 0, 0
    result["daemon_rss_mb"] = round(rss_kb / 1024, 1)
    result["rss_bytes"] = rss_kb * 1024
    result["daemon_threads"] = threads

    # Open file descriptors (self — /proc/self/fd is always accessible)
    try:
        result["open_fds"] = _sample_open_fds()
    except Exception:
        result.setdefault("open_fds", 0)

    # System RAM
    try:
        total_ram_kb, avail_ram_kb = _sample_meminfo()
    except Exception:
        total_ram_kb = avail_ram_kb = 0
    result["system_ram_total_mb"] = round(total_ram_kb / 1024, 1)
    result["system_ram_available_mb"] = round(avail_ram_kb / 1024, 1)

    # Load average
    try:
        la1, la5, la15 = _sample_loadavg()
        result["load_avg_1m"] = la1
        result["load_avg_5m"] = la5
        result["load_avg_15m"] = la15
    except Exception:
        result.setdefault("load_avg_1m", 0.0)
        result.setdefault("load_avg_5m", 0.0)
        result.setdefault("load_avg_15m", 0.0)

    # DB directory size — uses storage proxy in server mode, path walk otherwise.
    _dbsize_t0 = time.time()
    result["db_size_mb"] = _sample_db_size(storage, db_path)
    # P11: observe dbsize sampling duration (non-fatal; bare call avoids cyclo branch).
    _observe_dbsize_ms((time.time() - _dbsize_t0) * 1000.0)

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
