"""Graph edge builders — all _build_* helpers for get_full_graph (C4 split).

This mixin is merged into ``GraphAPI`` via multiple inheritance. It holds the
eight DB-query helpers that turn StorageEngine rows into visualization edge dicts.
``_extract_id`` lives in ``graph_api`` and is available via ``self._extract_id``.
``EDGE_TYPES`` / ``LAZY_EDGE_TYPES`` are the _shared contracts imported here.
"""

import logging

from yadgar._shared.contracts.viz import EDGE_TYPES
from yadgar._shared.observability.observe import observe

logger = logging.getLogger(__name__)


class GraphAPIEdgesMixin:
    """Edge-builder helpers for ``GraphAPI``."""

    @observe(tier="stage")
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

    @observe(tier="stage")
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

    @observe(tier="stage")
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

    @observe(tier="stage")
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

    @observe(tier="stage")
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

    @observe(tier="stage")
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

    @observe(tier="stage")
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

    @observe(tier="stage")
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
