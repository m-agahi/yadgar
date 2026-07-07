"""v5.54.3 — Graph viz fidelity tests.

Tests:
1. /api/graph includes entity-relation edges with type + role fields.
2. semantic edges NOT in default /api/graph payload.
3. v5.87 C3: semantic removed from EDGE_TYPES + LAZY_EDGE_TYPES; the lazy
   endpoint now gates "semantic" out (backend method dormant/unreachable).
4. All edges in default payload carry a `role` field.
5. Entity typed-relation types all represented (co_occurrence/resolved_by/caused_by).
6. EDGE_TYPES has role + default_on + lazy for all keys (including new entity types).
7. LAZY_EDGE_TYPES is empty (v5.87 C3 — semantic was its only member).
8. Role for entity types is "retrieval", for temporal/causal is "informational"
   (v5.80 #80: renamed from "display").
9. build_legend emits role + default_on + lazy per edge.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from yadgar.core.graph_api import GraphAPI
from yadgar.core.viz_meta import EDGE_TYPES, LAZY_EDGE_TYPES

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_mock(
    memory_rows=None,
    wiki_rows=None,
    causal_rows=None,
    entity_rels=None,
    entity_rows=None,
    transitions=None,
    wiki_crossrefs=None,
):
    s = MagicMock()
    s._q.return_value = memory_rows or []
    s.get_all_transitions.return_value = transitions or []
    s.get_all_wiki_crossrefs.return_value = wiki_crossrefs or []
    s.get_all_causal_edges.return_value = causal_rows or []
    s.get_relationships_by_types.return_value = entity_rels or []
    s.get_all_entities.return_value = entity_rows or []
    s.get_all_memory_similarity_links.return_value = []
    s.get_memory_clusters.return_value = []
    return s


def _entity_row(eid, name="E"):
    return {"id": eid, "name": name, "heat": 0.5, "archived": False}


def _entity_rel(src_id, tgt_id, rel_type):
    return {
        "source_entity_id": src_id,
        "target_entity_id": tgt_id,
        "relationship_type": rel_type,
        "weight": 1.0,
    }


# ---------------------------------------------------------------------------
# 1. Entity-relation edges included in default payload with type + role
# ---------------------------------------------------------------------------


class TestEntityEdgesInDefaultPayload:
    def test_co_occurrence_edge_in_payload(self):
        """co_occurrence entity relation appears in default /api/graph edges."""
        entity_rels = [_entity_rel(1, 2, "co_occurrence")]
        entity_rows = [_entity_row(1, "A"), _entity_row(2, "B")]
        s = _make_mock(entity_rels=entity_rels, entity_rows=entity_rows)
        result = GraphAPI(s).get_full_graph()
        types = [e["type"] for e in result["edges"]]
        assert "co_occurrence" in types, f"co_occurrence missing from edges: {types}"

    def test_all_entity_rel_types_in_payload(self):
        """All viz entity typed-relation types appear when present in storage.

        v5.86 VIZ Batch-2 (P0.4): imports/calls dropped (code-only, always empty
        on a prose corpus). Remaining set: co_occurrence/resolved_by/caused_by.
        """
        rel_types = ["co_occurrence", "resolved_by", "caused_by"]
        entity_rels = [_entity_rel(i, i + 10, rt) for i, rt in enumerate(rel_types, start=1)]
        entity_rows = [_entity_row(i, f"E{i}") for i in range(1, 16)]
        s = _make_mock(entity_rels=entity_rels, entity_rows=entity_rows)
        result = GraphAPI(s).get_full_graph()
        output_types = {e["type"] for e in result["edges"]}
        for rt in rel_types:
            assert rt in output_types, f"Entity relation type '{rt}' missing from edges"

    def test_entity_rel_edge_has_role_field(self):
        """Entity relation edges carry a 'role' field."""
        entity_rels = [_entity_rel(1, 2, "co_occurrence")]
        entity_rows = [_entity_row(1, "A"), _entity_row(2, "B")]
        s = _make_mock(entity_rels=entity_rels, entity_rows=entity_rows)
        result = GraphAPI(s).get_full_graph()
        co_edges = [e for e in result["edges"] if e.get("type") == "co_occurrence"]
        assert co_edges, "Expected co_occurrence edge in result"
        assert "role" in co_edges[0], "co_occurrence edge missing 'role' field"
        assert co_edges[0]["role"] == "retrieval", (
            f"co_occurrence role expected 'retrieval', got {co_edges[0]['role']}"
        )

    def test_entity_rel_edge_has_type_field(self):
        """Entity relation edges carry the correct 'type' field.

        v5.86 VIZ Batch-2 (P0.4): uses resolved_by (imports dropped from viz).
        """
        entity_rels = [_entity_rel(1, 2, "resolved_by")]
        entity_rows = [_entity_row(1, "X"), _entity_row(2, "Y")]
        s = _make_mock(entity_rels=entity_rels, entity_rows=entity_rows)
        result = GraphAPI(s).get_full_graph()
        resolved_edges = [e for e in result["edges"] if e.get("type") == "resolved_by"]
        assert resolved_edges, "Expected resolved_by edge in result"
        assert resolved_edges[0]["type"] == "resolved_by"

    def test_get_relationships_by_types_called_with_viz_types(self):
        """GraphAPI queries get_relationships_by_types with the viz entity-rel set.

        v5.86 VIZ Batch-2 (P0.4): imports/calls dropped from the queried set.
        """
        s = _make_mock()
        GraphAPI(s).get_full_graph()
        s.get_relationships_by_types.assert_called_once()
        call_args = s.get_relationships_by_types.call_args[0][0]
        expected = {"co_occurrence", "resolved_by", "caused_by"}
        assert set(call_args) == expected, f"Expected types {expected}, got {set(call_args)}"

    def test_entity_rel_edges_orphan_filtered(self):
        """Entity relation edges with endpoints not in node set are dropped."""
        # Entity rels reference IDs 99, 100 — not in entity_rows → orphaned
        entity_rels = [_entity_rel(99, 100, "co_occurrence")]
        entity_rows = []  # no entity nodes → orphan
        s = _make_mock(entity_rels=entity_rels, entity_rows=entity_rows)
        result = GraphAPI(s).get_full_graph()
        co_edges = [e for e in result["edges"] if e.get("type") == "co_occurrence"]
        assert co_edges == [], f"Expected 0 orphan co_occurrence edges, got {len(co_edges)}"


# ---------------------------------------------------------------------------
# 2. Semantic edges NOT in default payload
# ---------------------------------------------------------------------------


class TestSemanticNotInDefaultPayload:
    def test_semantic_absent_from_default_graph(self):
        """Semantic edges must NOT appear in the default /api/graph payload."""
        # Even with embeddings available, semantic should not be computed
        s = _make_mock()
        result = GraphAPI(s).get_full_graph()
        sem_edges = [e for e in result["edges"] if e.get("type") == "semantic"]
        assert sem_edges == [], (
            f"Expected 0 semantic edges in default payload (lazy path), got {len(sem_edges)}"
        )


# ---------------------------------------------------------------------------
# 3. Lazy endpoint returns semantic edges on request
# ---------------------------------------------------------------------------


class TestLazySemanticEndpoint:
    def test_get_edges_by_type_semantic_now_gated_out(self):
        """v5.87 C3: semantic was removed from LAZY_EDGE_TYPES, so the lazy
        endpoint now gates it out with an error like any non-lazy type. The
        backend semantic compute path was deleted entirely."""
        s = _make_mock()
        result = GraphAPI(s).get_edges_by_type("semantic")
        assert "error" in result, "semantic is no longer lazy → should return error"
        assert result["edges"] == []

    def test_get_edges_by_type_unknown_type_returns_error(self):
        """get_edges_by_type with a non-lazy type returns an error."""
        s = _make_mock()
        result = GraphAPI(s).get_edges_by_type("co_occurrence")
        assert "error" in result, "Non-lazy type should return error"
        assert result["edges"] == []

    def test_get_edges_by_type_temporal_returns_error(self):
        """temporal is not lazy — returns error from lazy endpoint."""
        s = _make_mock()
        result = GraphAPI(s).get_edges_by_type("temporal")
        assert "error" in result


# ---------------------------------------------------------------------------
# 4. All edges in default payload carry a `role` field
# ---------------------------------------------------------------------------


class TestAllEdgesHaveRole:
    def test_temporal_edges_have_role(self):
        """Temporal edges carry a 'role' field."""
        s = _make_mock()
        # Insert memories with slots so temporal edges appear
        # We can't easily do this with mock (slot assignment is storage-level)
        # Use a simpler check: mock _q to return slot-bearing rows
        mem_rows = [
            {
                "id": 1,
                "content": "a",
                "heat": 1.0,
                "tags": [],
                "directory_context": "/x",
                "created_at": "2024-01-01",
                "slot_index": 0,
                "embedding": None,
            },
            {
                "id": 2,
                "content": "b",
                "heat": 1.0,
                "tags": [],
                "directory_context": "/x",
                "created_at": "2024-01-01",
                "slot_index": 0,
                "embedding": None,
            },
        ]
        s._q.return_value = mem_rows
        result = GraphAPI(s).get_full_graph()
        temporal = [e for e in result["edges"] if e.get("type") == "temporal"]
        assert temporal, "Expected temporal edges from slot-sharing memories"
        for t in temporal:
            assert "role" in t, "temporal edge missing 'role' field"

    def test_transition_edges_have_role(self):
        """Transition edges carry a 'role' field."""
        mem_rows = [
            {
                "id": 1,
                "content": "a",
                "heat": 1.0,
                "tags": [],
                "directory_context": "/x",
                "created_at": "2024-01-01",
                "slot_index": None,
                "embedding": None,
            },
            {
                "id": 2,
                "content": "b",
                "heat": 1.0,
                "tags": [],
                "directory_context": "/x",
                "created_at": "2024-01-01",
                "slot_index": None,
                "embedding": None,
            },
        ]
        transitions = [{"from_memory_id": 1, "to_memory_id": 2, "count": 3}]
        s = _make_mock(memory_rows=mem_rows, transitions=transitions)
        result = GraphAPI(s).get_full_graph()
        trn = [e for e in result["edges"] if e.get("type") == "transition"]
        assert trn, "Expected transition edges"
        for t in trn:
            assert "role" in t, "transition edge missing 'role' field"
            assert t["role"] == "retrieval", f"transition role expected retrieval, got {t['role']}"

    def test_causal_edges_have_role(self):
        """Causal edges carry a 'role' field."""
        entity_rows = [_entity_row(1, "A"), _entity_row(2, "B")]
        causal_rows = [
            {
                "source_entity_id": 1,
                "target_entity_id": 2,
                "confidence": 0.9,
                "algorithm": "pc",
                "source_memory_id": None,
                "valid_until": None,
            },
        ]
        s = _make_mock(causal_rows=causal_rows, entity_rows=entity_rows)
        result = GraphAPI(s).get_full_graph()
        causal = [e for e in result["edges"] if e.get("type") == "causal"]
        assert causal, "Expected causal edges"
        for c in causal:
            assert "role" in c, "causal edge missing 'role' field"
            assert c["role"] == "informational", (
                f"causal role expected informational, got {c['role']}"
            )


# ---------------------------------------------------------------------------
# 5. EDGE_TYPES: all 11 types present with required fields
# ---------------------------------------------------------------------------


class TestEdgeTypesRegistry:
    EXPECTED_TYPES = {
        "temporal",
        "transition",
        "wiki_crossref",
        "memory_wiki",
        "causal",
        "co_occurrence",
        "resolved_by",
        "caused_by",
        "memory_similarity_link",  # v5.80 #80 viz-fidelity-v2
    }

    def test_all_edge_types_present(self):
        """EDGE_TYPES has all viz edge types.

        v5.86 VIZ Batch-2 (P0.4): imports/calls dropped (code-only, always empty
        on a prose corpus).
        v5.87 C3: semantic dropped (lazy/off, O(n²), informational — the legend
        checkbox did nothing useful).
        """
        missing = self.EXPECTED_TYPES - set(EDGE_TYPES.keys())
        assert not missing, f"EDGE_TYPES missing: {missing}"

    def test_semantic_dropped(self):
        """semantic is NOT in EDGE_TYPES (v5.87 C3 — removed the dead legend toggle)."""
        assert "semantic" not in EDGE_TYPES

    def test_imports_calls_dropped(self):
        """imports/calls are NOT in EDGE_TYPES (P0.4 — code-only, legend was lying)."""
        assert "imports" not in EDGE_TYPES
        assert "calls" not in EDGE_TYPES

    def test_all_types_have_role_field(self):
        """Every entry in EDGE_TYPES has a 'role' field."""
        for key, meta in EDGE_TYPES.items():
            assert "role" in meta, f"EDGE_TYPES['{key}'] missing 'role'"
            assert meta["role"] in ("retrieval", "informational"), (
                f"EDGE_TYPES['{key}']['role'] must be 'retrieval' or 'informational', got {meta['role']}"
            )

    def test_all_types_have_default_on_field(self):
        """Every entry in EDGE_TYPES has a 'default_on' field."""
        for key, meta in EDGE_TYPES.items():
            assert "default_on" in meta, f"EDGE_TYPES['{key}'] missing 'default_on'"

    def test_entity_rel_types_have_retrieval_role(self):
        """Entity typed-relation types have role='retrieval'."""
        entity_rel_types = ["co_occurrence", "resolved_by", "caused_by"]
        for t in entity_rel_types:
            assert EDGE_TYPES[t]["role"] == "retrieval", (
                f"Expected EDGE_TYPES['{t}']['role'] == 'retrieval'"
            )

    def test_informational_types_have_informational_role(self):
        """semantic/temporal/causal/wiki_crossref/memory_wiki have role='informational'.

        v5.80 #80: renamed from "display" to "informational".
        """
        informational_types = ["temporal", "causal", "wiki_crossref", "memory_wiki"]
        for t in informational_types:
            assert EDGE_TYPES[t]["role"] == "informational", (
                f"Expected EDGE_TYPES['{t}']['role'] == 'informational' (renamed from display in v5.80)"
            )

    def test_transition_has_retrieval_role(self):
        """transition is retrieval-active (co-recall prior, v5.54.2)."""
        assert EDGE_TYPES["transition"]["role"] == "retrieval"

    def test_semantic_not_in_registry(self):
        """semantic was removed from EDGE_TYPES (v5.87 C3)."""
        assert "semantic" not in EDGE_TYPES

    def test_non_semantic_entity_types_default_on_true(self):
        """Entity typed-relation types default ON."""
        for t in ["co_occurrence", "resolved_by", "caused_by"]:
            assert EDGE_TYPES[t]["default_on"] is True, f"Expected {t} default_on=True"


# ---------------------------------------------------------------------------
# 6. LAZY_EDGE_TYPES
# ---------------------------------------------------------------------------


class TestLazyEdgeTypes:
    def test_semantic_not_lazy(self):
        """LAZY_EDGE_TYPES no longer contains 'semantic' (v5.87 C3 — emptied)."""
        assert "semantic" not in LAZY_EDGE_TYPES
        assert len(LAZY_EDGE_TYPES) == 0

    def test_entity_types_not_lazy(self):
        """Entity typed-relation types are not lazy (stored/cheap)."""
        for t in ["co_occurrence", "resolved_by", "caused_by"]:
            assert t not in LAZY_EDGE_TYPES, f"Expected {t} not in LAZY_EDGE_TYPES"

    def test_temporal_not_lazy(self):
        """temporal is not lazy (computed from slot map in default path)."""
        assert "temporal" not in LAZY_EDGE_TYPES

    def test_transition_not_lazy(self):
        """transition is not lazy."""
        assert "transition" not in LAZY_EDGE_TYPES


# ---------------------------------------------------------------------------
# 7. build_legend emits role + default_on + lazy
# ---------------------------------------------------------------------------


class TestBuildLegendEmitsRoleFields:
    @pytest.fixture
    def settings(self):
        from yadgar._shared.config import get_settings

        get_settings.cache_clear()
        s = get_settings()
        get_settings.cache_clear()
        return s

    def test_legend_edges_have_role(self, settings):
        """build_legend edges each have a non-empty 'role' field."""
        from yadgar.core.viz_meta import build_legend

        legend = build_legend(settings)
        for edge in legend["edges"]:
            assert "role" in edge, f"legend edge '{edge['key']}' missing 'role'"
            assert edge["role"] in ("retrieval", "informational"), (
                f"legend edge '{edge['key']}' role invalid: {edge['role']}"
            )

    def test_legend_edges_have_default_on(self, settings):
        """build_legend edges each have a 'default_on' field."""
        from yadgar.core.viz_meta import build_legend

        legend = build_legend(settings)
        for edge in legend["edges"]:
            assert "default_on" in edge, f"legend edge '{edge['key']}' missing 'default_on'"

    def test_legend_edges_have_lazy(self, settings):
        """build_legend edges each have a 'lazy' field."""
        from yadgar.core.viz_meta import build_legend

        legend = build_legend(settings)
        for edge in legend["edges"]:
            assert "lazy" in edge, f"legend edge '{edge['key']}' missing 'lazy'"

    def test_legend_semantic_absent(self, settings):
        """build_legend: semantic edge is gone (v5.87 C3 — no dead legend toggle)."""
        from yadgar.core.viz_meta import build_legend

        legend = build_legend(settings)
        sem = next((e for e in legend["edges"] if e["key"] == "semantic"), None)
        assert sem is None, "semantic must not appear in the legend after v5.87 C3"

    def test_legend_co_occurrence_lazy_false(self, settings):
        """build_legend: co_occurrence edge has lazy=False."""
        from yadgar.core.viz_meta import build_legend

        legend = build_legend(settings)
        co = next((e for e in legend["edges"] if e["key"] == "co_occurrence"), None)
        assert co is not None, "co_occurrence missing from legend"
        assert co["lazy"] is False

    def test_legend_has_all_edge_types(self, settings):
        """build_legend includes all viz edge types.

        v5.86 VIZ Batch-2 (P0.4): imports/calls dropped — the legend no longer
        advertises code-only edges that are always empty on a prose corpus.
        """
        from yadgar.core.viz_meta import build_legend

        legend = build_legend(settings)
        keys = {e["key"] for e in legend["edges"]}
        expected = {
            "temporal",
            "transition",
            "wiki_crossref",
            "memory_wiki",
            "causal",
            "co_occurrence",
            "resolved_by",
            "caused_by",
            "memory_similarity_link",  # v5.80 #80 viz-fidelity-v2
        }
        missing = expected - keys
        assert not missing, f"Legend missing edge types: {missing}"
        assert "imports" not in keys
        assert "calls" not in keys
