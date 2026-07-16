"""VIZ Batch-2 backend (car 6 of v5.86 train) — TDD RED→GREEN.

Covers:
- P0.4: imports/calls dropped from viz edge-type list + legend (resolved_by kept).
- P2.1: mem↔wiki bridge built from the reverse memory.wiki_refs field.
- P2.2: clusters report real member_count even when members are off the
        top-500-hottest heat cap (so the sidebar shows non-empty clusters).

P0.4's resolved_by extractor/handler fix is tested at the consolidation layer in
test_consolidation.py::test_process_episodes_creates_resolved_by_relationship.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from yadgar.backend.graph.graph_api import GraphAPI
from yadgar.core.viz.viz_meta import EDGE_TYPES, build_legend

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mem_row(
    mid, *, heat=1.0, wiki_refs=None, cluster_id=None, slot_index=None, last_accessed=None
):
    return {
        "id": mid,
        "content": f"mem {mid}",
        "heat": heat,
        "tags": [],
        "directory_context": "/x",
        "created_at": "2024-01-01",
        "last_accessed": last_accessed,
        "slot_index": slot_index,
        "embedding": None,
        "cluster_id": cluster_id,
        "wiki_refs": wiki_refs or [],
    }


def _wiki_row(wid, slug):
    return {
        "id": wid,
        "title": f"Page {slug}",
        "slug": slug,
        "category": "reference",
        "tags": [],
        "links": [],
        "source_memory_ids": [],  # always empty on every write path — the bug
        "embedding": None,
        "updated_at": "2024-01-01",
    }


def _make_storage(
    *,
    memory_rows=None,
    wiki_rows=None,
    clusters=None,
    cluster_members=None,
    transitions=None,
    astrocyte_processes=None,
):
    """Storage mock that routes the two _q SELECTs (memory then wiki) correctly.

    cluster_members: dict[cluster_id -> list[int member mem ids]].
    transitions: list of memory_transition rows (from_memory_id/to_memory_id/count).
    astrocyte_processes: list of astrocyte_process rows (id/domain/memory_ids).
    """
    s = MagicMock()
    mem = memory_rows or []
    wik = wiki_rows or []

    def _q_side_effect(sql, *args, **kwargs):
        if "FROM memory" in sql:
            return mem
        if "FROM wiki_page" in sql:
            return wik
        return []

    s._q.side_effect = _q_side_effect
    s.get_all_transitions.return_value = transitions or []
    s.get_all_wiki_crossrefs.return_value = []
    s.get_all_causal_edges.return_value = []
    s.get_relationships_by_types.return_value = []
    s.get_all_entities.return_value = []
    s.get_all_memory_similarity_links.return_value = []
    s.get_memory_clusters.return_value = clusters or []
    members = cluster_members or {}
    # viz-render-perf (Car A): _build_clusters_payload batches membership via a
    # single get_all_cluster_members() round-trip (was per-cluster get_cluster_members).
    s.get_cluster_members.side_effect = lambda cid: members.get(cid, [])
    s.get_all_cluster_members.return_value = members
    s.get_astrocyte_processes.return_value = astrocyte_processes or []
    return s


# ---------------------------------------------------------------------------
# P0.4 — imports/calls dropped from viz edge-type list + legend (resolved_by kept)
# ---------------------------------------------------------------------------


class TestImportsCallsDroppedFromViz:
    def test_imports_absent_from_edge_types(self):
        """`imports` is removed from EDGE_TYPES (code-only, always empty on prose)."""
        assert "imports" not in EDGE_TYPES

    def test_calls_absent_from_edge_types(self):
        """`calls` is removed from EDGE_TYPES (code-only, always empty on prose)."""
        assert "calls" not in EDGE_TYPES

    def test_resolved_by_kept_in_edge_types(self):
        """`resolved_by` stays — it's now genuinely populated (P0.4 fix)."""
        assert "resolved_by" in EDGE_TYPES

    def test_co_occurrence_and_caused_by_kept(self):
        assert "co_occurrence" in EDGE_TYPES
        assert "caused_by" in EDGE_TYPES

    def test_imports_calls_absent_from_legend(self):
        """The dynamic legend (driven by EDGE_TYPES) no longer advertises imports/calls."""
        from yadgar._shared.config import get_settings

        get_settings.cache_clear()
        settings = get_settings()
        get_settings.cache_clear()
        keys = {e["key"] for e in build_legend(settings)["edges"]}
        assert "imports" not in keys
        assert "calls" not in keys
        assert "resolved_by" in keys

    def test_entity_rel_query_excludes_imports_calls(self):
        """graph_api no longer queries storage for imports/calls relationships."""
        s = _make_storage()
        GraphAPI(s).get_full_graph()
        s.get_relationships_by_types.assert_called_once()
        queried = set(s.get_relationships_by_types.call_args[0][0])
        assert "imports" not in queried
        assert "calls" not in queried
        assert {"co_occurrence", "resolved_by", "caused_by"} <= queried


# ---------------------------------------------------------------------------
# P2.1 — mem↔wiki bridge from reverse memory.wiki_refs
# ---------------------------------------------------------------------------


class TestMemWikiBridge:
    def test_memory_wiki_edge_built_from_wiki_refs(self):
        """A memory whose wiki_refs holds a loaded page slug → a memory_wiki edge."""
        mem = [_mem_row(1, wiki_refs=["some-page"])]
        wik = [_wiki_row(10, "some-page")]
        s = _make_storage(memory_rows=mem, wiki_rows=wik)
        result = GraphAPI(s).get_full_graph()
        mw = [e for e in result["edges"] if e.get("type") == "memory_wiki"]
        assert mw, "expected a memory_wiki edge from memory.wiki_refs"
        assert mw[0]["source"] == "mem:1"
        assert mw[0]["target"] == "wiki:10"
        assert mw[0]["role"] == "informational"

    def test_no_edge_when_wiki_refs_empty(self):
        mem = [_mem_row(1, wiki_refs=[])]
        wik = [_wiki_row(10, "some-page")]
        s = _make_storage(memory_rows=mem, wiki_rows=wik)
        result = GraphAPI(s).get_full_graph()
        mw = [e for e in result["edges"] if e.get("type") == "memory_wiki"]
        assert mw == []

    def test_ref_to_unloaded_page_dropped(self):
        """wiki_refs slug whose page is not in the loaded set produces no edge."""
        mem = [_mem_row(1, wiki_refs=["missing-page"])]
        wik = [_wiki_row(10, "some-page")]
        s = _make_storage(memory_rows=mem, wiki_rows=wik)
        result = GraphAPI(s).get_full_graph()
        mw = [e for e in result["edges"] if e.get("type") == "memory_wiki"]
        assert mw == []


# ---------------------------------------------------------------------------
# P2.2 — cluster heat-cap: real member_count even for off-screen members
# ---------------------------------------------------------------------------


class TestClusterMemberCount:
    def test_offscreen_cluster_reports_real_member_count(self):
        """A cluster whose members are all outside the rendered node set still
        reports member_count > 0 (not dropped / not zeroed)."""
        # Rendered memory: id 1. Cluster 7's members are 900, 901 — off-screen.
        mem = [_mem_row(1)]
        clusters = [{"id": 7, "name": "offscreen", "level": 0}]
        members = {7: [900, 901, 902]}
        s = _make_storage(memory_rows=mem, clusters=clusters, cluster_members=members)
        result = GraphAPI(s).get_full_graph()
        cl = [c for c in result["clusters"] if c["id"] == 7]
        assert cl, "cluster must still be emitted even with no on-screen members"
        assert cl[0]["member_count"] == 3, "member_count must reflect the REAL DB count"
        assert cl[0]["member_node_ids"] == [], "no on-screen members → empty node id list"

    def test_onscreen_cluster_member_count_matches_db(self):
        mem = [_mem_row(1), _mem_row(2)]
        clusters = [{"id": 5, "name": "live", "level": 1}]
        members = {5: [1, 2, 3]}  # 1,2 on-screen; 3 off-screen
        s = _make_storage(memory_rows=mem, clusters=clusters, cluster_members=members)
        result = GraphAPI(s).get_full_graph()
        cl = [c for c in result["clusters"] if c["id"] == 5][0]
        assert cl["member_count"] == 3
        assert set(cl["member_node_ids"]) == {"mem:1", "mem:2"}

    def test_cluster_membership_batched_into_one_roundtrip(self):
        """viz-render-perf (Car A): membership fetched via ONE get_all_cluster_members
        call — the per-cluster get_cluster_members N+1 is gone from the payload build."""
        mem = [_mem_row(1), _mem_row(2)]
        clusters = [
            {"id": 5, "name": "a", "level": 0},
            {"id": 6, "name": "b", "level": 0},
            {"id": 7, "name": "c", "level": 0},
        ]
        members = {5: [1], 6: [2], 7: [900]}
        s = _make_storage(memory_rows=mem, clusters=clusters, cluster_members=members)
        result = GraphAPI(s).get_full_graph()
        # Exactly one batch call, zero per-cluster calls from the cluster payload build.
        assert s.get_all_cluster_members.call_count == 1
        assert s.get_cluster_members.call_count == 0
        # Semantics preserved across all three clusters.
        by_id = {c["id"]: c for c in result["clusters"]}
        assert by_id[5]["member_count"] == 1 and by_id[5]["member_node_ids"] == ["mem:1"]
        assert by_id[6]["member_count"] == 1 and by_id[6]["member_node_ids"] == ["mem:2"]
        assert by_id[7]["member_count"] == 1 and by_id[7]["member_node_ids"] == []


# ---------------------------------------------------------------------------
# viz-render-perf (Car A) — per-edge-type caps threaded from the call site
# ---------------------------------------------------------------------------


class TestEdgeCaps:
    def test_default_get_full_graph_passes_unlimited(self):
        """Default get_full_graph (the precompute path) forwards limit=0 to every scan."""
        s = _make_storage(memory_rows=[_mem_row(1)])
        GraphAPI(s).get_full_graph()
        assert s.get_all_transitions.call_args.kwargs.get("limit") == 0
        assert s.get_all_wiki_crossrefs.call_args.kwargs.get("limit") == 0
        assert s.get_all_causal_edges.call_args.kwargs.get("limit") == 0
        assert s.get_relationships_by_types.call_args.kwargs.get("limit") == 0
        assert s.get_all_memory_similarity_links.call_args.kwargs.get("limit") == 0

    def test_caps_threaded_to_each_scan(self):
        """EdgeCaps fields reach the matching storage scan as limit=."""
        from yadgar.backend.graph.graph_api import EdgeCaps

        s = _make_storage(memory_rows=[_mem_row(1)])
        GraphAPI(s).get_full_graph(
            edge_caps=EdgeCaps(
                transitions=3,
                wiki_crossrefs=4,
                causal_edges=5,
                relationships=6,
                similarity_links=7,
            )
        )
        assert s.get_all_transitions.call_args.kwargs["limit"] == 3
        assert s.get_all_wiki_crossrefs.call_args.kwargs["limit"] == 4
        assert s.get_all_causal_edges.call_args.kwargs["limit"] == 5
        assert s.get_relationships_by_types.call_args.kwargs["limit"] == 6
        assert s.get_all_memory_similarity_links.call_args.kwargs["limit"] == 7


# ---------------------------------------------------------------------------
# viz-rest #55 — last_accessed per memory node payload
# ---------------------------------------------------------------------------


class TestLastAccessedNodePayload:
    def test_memory_node_has_last_accessed_field(self):
        """Every memory node dict carries a last_accessed key (#55)."""
        mem = [_mem_row(1, last_accessed="2024-06-01T12:00:00Z")]
        s = _make_storage(memory_rows=mem)
        result = GraphAPI(s).get_full_graph()
        nodes = [n for n in result["nodes"] if n["id"] == "mem:1"]
        assert nodes, "memory node must be present"
        assert nodes[0]["last_accessed"] == "2024-06-01T12:00:00Z"

    def test_last_accessed_empty_string_when_absent(self):
        """A memory with no last_accessed yields '' (never KeyError / None leak)."""
        mem = [_mem_row(1, last_accessed=None)]
        s = _make_storage(memory_rows=mem)
        result = GraphAPI(s).get_full_graph()
        nodes = [n for n in result["nodes"] if n["id"] == "mem:1"]
        assert nodes[0]["last_accessed"] == ""

    def test_select_requests_last_accessed_column(self):
        """The memory SELECT includes last_accessed (else the payload can't carry it)."""
        s = _make_storage(memory_rows=[_mem_row(1)])
        GraphAPI(s).get_full_graph()
        sql = s._q.call_args_list[0][0][0]
        assert "last_accessed" in sql


# ---------------------------------------------------------------------------
# viz-rest #89 — weak-edge (count<2 transition) render toggle
# ---------------------------------------------------------------------------


def _txn(a, b, count):
    return {"from_memory_id": a, "to_memory_id": b, "count": count}


class TestWeakEdgeToggle:
    def test_weak_edges_hidden_by_default(self):
        """count<2 transitions are excluded by default; counted in weak_edges_hidden."""
        mem = [_mem_row(1), _mem_row(2)]
        txns = [_txn(1, 2, 1)]  # weak (count=1)
        s = _make_storage(memory_rows=mem, transitions=txns)
        result = GraphAPI(s).get_full_graph()
        te = [e for e in result["edges"] if e.get("type") == "transition"]
        assert te == [], "weak edge must be hidden by default"
        assert result["weak_edges_hidden"] == 1

    def test_include_weak_renders_weak_edges(self):
        """include_weak=True renders the count<2 edge; weak_edges_hidden still counts it."""
        from yadgar.backend.graph.graph_api import EdgeCaps

        mem = [_mem_row(1), _mem_row(2)]
        txns = [_txn(1, 2, 1)]
        s = _make_storage(memory_rows=mem, transitions=txns)
        result = GraphAPI(s).get_full_graph(edge_caps=EdgeCaps(include_weak=True))
        te = [e for e in result["edges"] if e.get("type") == "transition"]
        assert len(te) == 1, "weak edge must render when include_weak is on"
        assert te[0]["source"] == "mem:1"
        assert te[0]["target"] == "mem:2"
        assert te[0]["count"] == 1
        # F4 affordance count is independent of the render toggle
        assert result["weak_edges_hidden"] == 1

    def test_strong_edges_render_regardless(self):
        """count>=2 transitions always render (both default and include_weak)."""
        mem = [_mem_row(1), _mem_row(2)]
        txns = [_txn(1, 2, 5)]
        s = _make_storage(memory_rows=mem, transitions=txns)
        default = GraphAPI(s).get_full_graph()
        assert len([e for e in default["edges"] if e.get("type") == "transition"]) == 1
        assert default["weak_edges_hidden"] == 0


# ---------------------------------------------------------------------------
# viz-rest #14 — astrocyte_domain as a cluster source
# ---------------------------------------------------------------------------


class TestAstrocyteClusterSource:
    def test_astrocyte_process_surfaces_as_cluster(self):
        """A populated astrocyte process appears as source=astrocyte_domain."""
        mem = [_mem_row(1), _mem_row(2)]
        procs = [{"id": 3, "domain": "errors", "memory_ids": [1, 2]}]
        s = _make_storage(memory_rows=mem, astrocyte_processes=procs)
        result = GraphAPI(s).get_full_graph()
        astro = [c for c in result["clusters"] if c["source"] == "astrocyte_domain"]
        assert astro, "expected an astrocyte_domain cluster"
        assert astro[0]["label"] == "errors"
        assert astro[0]["id"] == "astro:3"
        assert set(astro[0]["member_node_ids"]) == {"mem:1", "mem:2"}
        assert astro[0]["member_count"] == 2

    def test_astrocyte_member_intersected_with_rendered_nodes(self):
        """member_count is the pre-intersection DB count; member_node_ids the intersection."""
        mem = [_mem_row(1)]  # only mem:1 rendered
        procs = [{"id": 3, "domain": "decisions", "memory_ids": [1, 900, 901]}]
        s = _make_storage(memory_rows=mem, astrocyte_processes=procs)
        result = GraphAPI(s).get_full_graph()
        astro = [c for c in result["clusters"] if c["source"] == "astrocyte_domain"]
        assert astro[0]["member_node_ids"] == ["mem:1"]
        assert astro[0]["member_count"] == 3

    def test_no_astrocyte_processes_no_astrocyte_clusters(self):
        """Empty astrocyte data → no astrocyte_domain clusters (memory_cluster unaffected)."""
        mem = [_mem_row(1)]
        s = _make_storage(memory_rows=mem, astrocyte_processes=[])
        result = GraphAPI(s).get_full_graph()
        assert [c for c in result["clusters"] if c["source"] == "astrocyte_domain"] == []

    def test_astrocyte_coexists_with_memory_cluster(self):
        """Both cluster sources appear together."""
        mem = [_mem_row(1)]
        clusters = [{"id": 7, "name": "mc", "level": 0}]
        members = {7: [1]}
        procs = [{"id": 3, "domain": "code-patterns", "memory_ids": [1]}]
        s = _make_storage(
            memory_rows=mem,
            clusters=clusters,
            cluster_members=members,
            astrocyte_processes=procs,
        )
        result = GraphAPI(s).get_full_graph()
        sources = {c["source"] for c in result["clusters"]}
        assert sources == {"memory_cluster", "astrocyte_domain"}
