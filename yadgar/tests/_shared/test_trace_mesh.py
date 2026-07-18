"""Pure-function tests for yadgar/_shared/trace_mesh.py (viz-trace-replay Car B).

Covers the simplify_trace pure logic against committed synthetic fixtures:
containment tree build, storm ×N aggregation, keep-floor, MAX_BOXES cap, lane
assignment, and the dropped-boundary flat-forest fallback (audit_anchors class).

Fixtures live under yadgar/tests/fixtures/traces/ (capture_trace shape).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from yadgar._shared import trace_mesh as tm

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "traces"


def _load(name: str) -> dict:
    return json.loads((_FIXTURES / f"{name}.json").read_text())


# ── containment tree ─────────────────────────────────────────────────────────


class TestBuildTree:
    def test_contains_start_containment_with_slack(self) -> None:
        parent = tm.Span(name="p", rel=0.0, dur=10.0, svc="yadgar-core", depth=0)
        inside = tm.Span(name="c", rel=5.0, dur=2.0, svc="yadgar-core", depth=1)
        after = tm.Span(name="d", rel=50.0, dur=1.0, svc="yadgar-core", depth=1)
        assert tm._contains(parent, inside) is True
        assert tm._contains(parent, after) is False

    def test_contains_child_may_outlive_parent(self) -> None:
        # a child that STARTS inside but ends after the parent window still counts
        parent = tm.Span(name="p", rel=0.0, dur=10.0, svc="yadgar-core", depth=0)
        outliving = tm.Span(name="c", rel=9.0, dur=100.0, svc="yadgar-core", depth=1)
        assert tm._contains(parent, outliving) is True

    def test_build_tree_reparents_by_containment(self) -> None:
        data = _load("normal_two_lane")
        root = tm.build_tree(data["spans"])
        tool = tm.find_tool_span(root, "tool.recall")
        assert tool is not None
        assert tool.name == "tool.recall"
        # the tool span owns the pipeline children (fts, vector, _q, fusion, boost)
        child_names = {c.short for c in tool.children}
        assert "_run_fts_bm25" in child_names
        assert "_collect_vector_scores" in child_names


# ── storm aggregation ────────────────────────────────────────────────────────


class TestStormAggregation:
    def test_aggregate_storms_collapses_identical_siblings(self) -> None:
        data = _load("storm_xn")
        root = tm.build_tree(data["spans"])
        # the fts stage has 10 identical _cosine_similarity children (>= STORM_MIN)
        fts = None
        for node in _walk(root):
            if node.short == "_run_fts_bm25":
                fts = node
                break
        assert fts is not None
        agg = tm.aggregate_storms(fts)
        cosine = [s for s in agg if s.short == "_cosine_similarity"]
        assert len(cosine) == 1, "10 identical siblings must collapse to ONE aggregate"
        assert cosine[0].count == 10
        # aggregate duration = sum of members
        assert cosine[0].dur == pytest.approx(40.0)

    def test_below_storm_min_not_aggregated(self) -> None:
        node = tm.Span(name="p", rel=0.0, dur=10.0, svc="yadgar-core", depth=0)
        for i in range(3):  # 3 < STORM_MIN (4)
            node.children.append(
                tm.Span(name="x.dup", rel=float(i), dur=1.0, svc="yadgar-core", depth=1)
            )
        out = tm.aggregate_storms(node)
        assert len(out) == 3
        assert all(s.count == 1 for s in out)


# ── lane assignment ──────────────────────────────────────────────────────────


class TestLane:
    def test_lane_backend_vs_core(self) -> None:
        assert tm._lane("yadgar-backend") == "backend"
        assert tm._lane("yadgar-core") == "core"
        assert tm._lane("anything-else") == "core"


# ── mesh build: normal two-lane ──────────────────────────────────────────────


class TestBuildMeshNormal:
    def test_two_lane_mesh_has_both_lanes(self) -> None:
        mesh = tm.build_mesh(_load("normal_two_lane"))
        lanes = {n["lane"] for n in mesh["nodes"]}
        assert "core" in lanes
        assert "backend" in lanes, "the SurrealDB POST + reranker should be backend lane"
        assert mesh["dropped_boundary"] is False
        assert mesh["tool"] == "tool.recall"
        assert mesh["timeline_ms"] == pytest.approx(120.0)

    def test_edges_chain_stage_nodes(self) -> None:
        mesh = tm.build_mesh(_load("normal_two_lane"))
        n = len(mesh["nodes"])
        assert len(mesh["edges"]) == max(0, n - 1)
        # edges reference consecutive s0->s1->s2 ids
        for i, e in enumerate(mesh["edges"]):
            assert e["src"] == f"s{i}"
            assert e["dst"] == f"s{i + 1}"
            assert e["order"] == i + 1

    def test_node_ids_and_rel_ms_present(self) -> None:
        mesh = tm.build_mesh(_load("normal_two_lane"))
        for i, node in enumerate(mesh["nodes"]):
            assert node["id"] == f"s{i}"
            assert isinstance(node["rel_ms"], (int, float))
            assert isinstance(node["dur_ms"], (int, float))
            assert node["label"]  # friendly label non-empty


# ── item-1: core-lane boundary + forwarder injection (forward-only recall) ────


class TestCoreBoundaryStages:
    def test_core_boundary_stages_boundary_only(self) -> None:
        # a tool span with ONLY backend descendants (no core forwarder) →
        # core_boundary_stages returns just the boundary node.
        tool = tm.Span(name="tool.recall", rel=0.0, dur=100.0, svc="yadgar-core", depth=0)
        backend = tm.Span(name="be.stage", rel=1.0, dur=98.0, svc="yadgar-backend", depth=1)
        backend.parent = tool
        tool.children.append(backend)
        out = tm.core_boundary_stages(tool)
        assert len(out) == 1
        assert out[0].svc == "yadgar-core"
        assert out[0].name == "tool.recall"
        # self-time = tool.dur - sum(core child dur); no core children → floored small
        assert out[0].dur > 0

    def test_core_boundary_stages_boundary_plus_forwarder(self) -> None:
        # boundary + a core-svc forwarder span that has a backend crossing child.
        tool = tm.Span(name="tool.recall", rel=0.0, dur=300.0, svc="yadgar-core", depth=0)
        fwd = tm.Span(
            name="yadgar._shared.storage.client._ClientMixin._q",
            rel=1.0,
            dur=297.0,
            svc="yadgar-core",
            depth=1,
        )
        fwd.parent = tool
        post = tm.Span(name="POST", rel=2.0, dur=296.0, svc="yadgar-backend", depth=2)
        post.parent = fwd
        fwd.children.append(post)
        tool.children.append(fwd)
        out = tm.core_boundary_stages(tool)
        assert len(out) == 2
        assert all(s.svc == "yadgar-core" for s in out)
        assert out[0].name == "tool.recall"
        # forwarder is the core-svc span that owns the backend crossing
        assert out[1].name.endswith("._q")

    def test_core_boundary_stages_backend_tool_no_phantom(self) -> None:
        # guard: a tool whose boundary is svc=yadgar-backend → no core node.
        tool = tm.Span(name="tool.weird", rel=0.0, dur=50.0, svc="yadgar-backend", depth=0)
        assert tm.core_boundary_stages(tool) == []

    def test_build_mesh_forward_only_recall_has_core_boundary(self) -> None:
        # THE REPRO: realistic forward-only recall (all pipeline backend-svc) must
        # now yield >=1 core-lane node (the boundary), flipping the old core=0 bug.
        mesh = tm.build_mesh(_load("forward_only_recall"))
        core_nodes = [n for n in mesh["nodes"] if n["lane"] == "core"]
        backend_nodes = [n for n in mesh["nodes"] if n["lane"] == "backend"]
        assert len(core_nodes) >= 1, "core lane must not be empty for a forward-only tool trace"
        assert core_nodes[0]["label"] == "Recall"
        assert len(backend_nodes) >= 1, "backend pipeline stages still present"
        assert mesh["dropped_boundary"] is False

    def test_build_mesh_forward_only_recall_surfaces_forwarder(self) -> None:
        mesh = tm.build_mesh(_load("forward_only_recall"))
        core_names = {n["name"] for n in mesh["nodes"] if n["lane"] == "core"}
        # the core->backend forwarder (_q hand-off) is surfaced as a core node
        assert any(n.endswith("._q") for n in core_names)

    def test_boundary_skipped_on_dropped_boundary(self) -> None:
        # audit_anchors-class flat forest: tool span dropped → no phantom boundary.
        mesh = tm.build_mesh(_load("dropped_boundary_flat_forest"))
        assert mesh["dropped_boundary"] is True
        labels = {n["label"] for n in mesh["nodes"]}
        assert "Recall" not in labels
        # tool==root; no synthetic <root> boundary node
        assert not any(n["name"] == "<root>" for n in mesh["nodes"])


# ── item-5: non-recall tool traces must not break the mesh ────────────────────


class TestNonRecallTools:
    def test_bookmark_list_empty_backend_lane(self) -> None:
        mesh = tm.build_mesh(_load("bookmark_list_core_only"))
        lanes = [n["lane"] for n in mesh["nodes"]]
        assert "backend" not in lanes, "core-only tool → backend lane empty"
        assert lanes.count("core") >= 1, "core nodes present, no raise"
        assert mesh["dropped_boundary"] is False

    def test_memorize_two_lane(self) -> None:
        mesh = tm.build_mesh(_load("memorize_two_lane"))
        lanes = {n["lane"] for n in mesh["nodes"]}
        assert "core" in lanes
        assert "backend" in lanes
        assert mesh["dropped_boundary"] is False

    def test_checkpoint_core_heavy(self) -> None:
        mesh = tm.build_mesh(_load("checkpoint_core_heavy"))
        # core-heavy: at least a core boundary, no raise
        assert any(n["lane"] == "core" for n in mesh["nodes"])
        assert mesh["dropped_boundary"] is False


# ── keep-floor ───────────────────────────────────────────────────────────────


class TestKeepFloor:
    def test_keep_floor_drops_micro_non_crossing_span(self) -> None:
        # Single-lane trace: one big stage + one sub-keep-floor micro span, no
        # lane crossing, no storm. thr = min(KEEP_FLOOR_MS, 1% of total).
        # total=1000 → thr=10ms. The 0.5ms micro span must NOT become a stage,
        # while the 200ms stage must.
        spans = [
            {"rel_ms": 0.0, "dur_ms": 1000.0, "depth": 0, "svc": "yadgar-core", "name": "tool.x"},
            {
                "rel_ms": 1.0,
                "dur_ms": 200.0,
                "depth": 1,
                "svc": "yadgar-core",
                "name": "yadgar.mod.big_stage",
            },
            {
                "rel_ms": 250.0,
                "dur_ms": 0.5,
                "depth": 1,
                "svc": "yadgar-core",
                "name": "yadgar.mod.micro_noise",
            },
        ]
        mesh = tm.build_mesh(
            {
                "label": "x",
                "tool_span": "tool.x",
                "trace_id": "t",
                "total_ms": 1000.0,
                "span_count": len(spans),
                "spans": spans,
            }
        )
        names = {n["name"] for n in mesh["nodes"]}
        assert "yadgar.mod.big_stage" in names
        assert "yadgar.mod.micro_noise" not in names


# ── dropped-boundary flat forest (audit_anchors class) ───────────────────────


class TestDroppedBoundary:
    def test_flat_forest_fallback(self) -> None:
        mesh = tm.build_mesh(_load("dropped_boundary_flat_forest"))
        assert mesh["dropped_boundary"] is True, (
            "no tool.* boundary span present → flat forest fallback"
        )
        # still produces a non-empty mesh (never 500s / never empty on real spans)
        assert len(mesh["nodes"]) >= 1
        assert mesh["timeline_ms"] == pytest.approx(500.0)

    def test_flat_forest_aggregates_cosine_storm(self) -> None:
        mesh = tm.build_mesh(_load("dropped_boundary_flat_forest"))
        cosine_nodes = [n for n in mesh["nodes"] if n["name"].endswith("_cosine_similarity")]
        # the 6 sibling cosine spans collapse into a single storm node with storm_n
        assert len(cosine_nodes) <= 1
        if cosine_nodes:
            assert cosine_nodes[0]["storm_n"] == 6


# ── MAX_BOXES cap ────────────────────────────────────────────────────────────


class TestMaxBoxesCap:
    def test_cap_never_exceeds_max_boxes(self) -> None:
        # synthesize a wide trace with many distinct kept stages
        spans = [
            {"rel_ms": 0.0, "dur_ms": 1000.0, "depth": 0, "svc": "yadgar-core", "name": "tool.big"}
        ]
        for i in range(40):
            spans.append(
                {
                    "rel_ms": float(i * 20 + 1),
                    "dur_ms": 15.0,  # >= keep-floor so each is a candidate
                    "depth": 1,
                    "svc": "yadgar-core",
                    "name": f"yadgar.mod.stage_{i}",
                }
            )
        mesh = tm.build_mesh(
            {
                "label": "big",
                "tool_span": "tool.big",
                "trace_id": "x",
                "total_ms": 1000.0,
                "span_count": len(spans),
                "spans": spans,
            }
        )
        # stage nodes must be capped (MAX_BOXES - 2 stage budget in select_stages)
        assert len(mesh["nodes"]) <= tm.MAX_BOXES


# ── graceful: empty / malformed ──────────────────────────────────────────────


class TestGraceful:
    def test_empty_spans_returns_empty_mesh(self) -> None:
        mesh = tm.build_mesh(
            {
                "label": "x",
                "tool_span": "tool.x",
                "trace_id": "t",
                "total_ms": 0.0,
                "span_count": 0,
                "spans": [],
            }
        )
        assert mesh["nodes"] == []
        assert mesh["edges"] == []
        assert mesh["timeline_ms"] == 0.0

    def test_missing_keys_do_not_raise(self) -> None:
        # a payload missing spans key entirely → empty mesh, no exception
        mesh = tm.build_mesh({})
        assert mesh["nodes"] == []


# ── helper ───────────────────────────────────────────────────────────────────


def _walk(node: tm.Span):
    yield node
    for c in node.children:
        yield from _walk(c)
