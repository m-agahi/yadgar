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
