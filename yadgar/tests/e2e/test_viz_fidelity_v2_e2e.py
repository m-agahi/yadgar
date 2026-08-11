"""BC-VZ-R1 / BC-VZ-R2 / BC-VZ-R3 e2e: viz fidelity v2 — role vocabulary, no
semantic in default payload, and real memory_cluster rows surfaced in clusters[].

Test-first (red → green):
  RED on current code because:
    (a) role vocabulary is "display" not "informational" for temporal/causal edges
    (b) clusters[] key is absent from get_full_graph() payload

GREEN after implementation:
    - viz_meta.EDGE_TYPES renames display → informational
    - graph_api.get_full_graph() emits clusters[] from real memory_cluster rows
    - graph_api.get_full_graph() emits memory_similarity_link edges with role=informational

Seeds:
  - Two memories sharing slot_index=42 (→ temporal edge; role must be "informational")
  - A transition between two memories (→ transition edge; role must be "retrieval")
  - A memory_cluster row with those memories assigned as members
  - A memory_similarity_link between two memories (→ similarity_link edge; role "informational")

v5.80 — #80 viz-fidelity-v2.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------

#: The identity every seed in this file names. C5/ADR-0227 made ``project_id``
#: mandatory at the storage write chokepoint, so a seed row that names no
#: project cannot be inserted at all. One value suffices here: the viz is a
#: god's-eye admin overlay with no project scoping on its read path, so this
#: file's subject never depends on two identities being distinguishable.
_TEST_PROJECT = "m-agahi/yadgar"


def _insert_mem(e2e_engines, content: str, heat: float = 0.5, slot_index=None) -> int:
    """Insert a memory row, optionally with slot_index. Returns integer id."""
    from datetime import UTC, datetime

    storage = e2e_engines["storage"]
    embeddings = e2e_engines["embeddings"]
    emb = embeddings.encode(content)
    now = datetime.now(UTC).isoformat()
    doc = {
        "content": content,
        "embedding": emb,
        "directory_context": e2e_engines["yadgar_dir"],
        "project_id": _TEST_PROJECT,
        "heat": heat,
        "tags": ["e2e", "viz-fidelity-v2"],
        "last_accessed": now,
        "created_at": now,
        "access_count": 0,
        "is_protected": False,
    }
    mid = storage.insert_memory(doc)
    if slot_index is not None:
        storage._q(f"UPDATE memory:{mid} SET slot_index = $si", {"si": int(slot_index)})
    return mid


def _insert_transition(e2e_engines, from_id: int, to_id: int, count: int) -> None:
    """Insert a memory_transition row (count >= 2 to pass weak-edge filter)."""
    storage = e2e_engines["storage"]
    # Use the insert_transition storage method if available, else raw _q.
    if hasattr(storage, "insert_transition"):
        storage.insert_transition(
            {"from_memory_id": from_id, "to_memory_id": to_id, "count": count}
        )
    else:
        storage._q(
            "CREATE memory_transition SET from_memory_id = $f, to_memory_id = $t, count = $c",
            {"f": from_id, "t": to_id, "c": count},
        )


def _insert_cluster(e2e_engines, name: str, level: int = 0) -> int:
    """Insert a memory_cluster row. Returns integer cluster id."""
    storage = e2e_engines["storage"]
    return storage.insert_cluster(
        {
            "name": name,
            "level": level,
            "summary": f"Test cluster: {name}",
            "member_count": 0,
        }
    )


def _assign_cluster_member(e2e_engines, memory_id: int, cluster_id: int) -> None:
    """Set cluster_id on a memory row so get_cluster_members returns it."""
    storage = e2e_engines["storage"]
    storage.update_memory_fields(memory_id, cluster_id=cluster_id)


def _insert_similarity_link(e2e_engines, mid_a: int, mid_b: int, weight: float = 0.9) -> int:
    """Insert a memory_similarity_link row. Returns integer link id."""
    storage = e2e_engines["storage"]
    return storage.insert_memory_similarity_link(mid_a, mid_b, weight)


# ---------------------------------------------------------------------------
# BC-VZ-R1 — every edge has role in {retrieval, informational};
#             temporal edges are "informational"; transition edges are "retrieval"
# ---------------------------------------------------------------------------


class TestBCVZR1_EdgeRoleVocabulary:
    """BC-VZ-R1: every edge SHALL carry role ∈ {retrieval, informational}.

    Temporal edges (informational — structural, not retrieval-active) and
    transition edges (retrieval — co-recall prior) must each carry the
    correct vocabulary value.

    RED on current code: temporal role is "display", not "informational".
    """

    def test_every_edge_has_valid_role_in_retrieval_informational(self, e2e_engines):
        """BC-VZ-R1: all edges in default payload must have role in {retrieval, informational}.

        Seeds temporal (slot-sharing) + transition edges so both role classes
        appear.  Asserts no edge has role="display" or any other invalid value.
        """
        from yadgar.backend.graph.graph_api import GraphAPI

        storage = e2e_engines["storage"]

        # Seed two memories sharing a slot → temporal edge
        mid_a = _insert_mem(e2e_engines, "vzr1 slot-a xvzr1slot unique", slot_index=99)
        mid_b = _insert_mem(e2e_engines, "vzr1 slot-b xvzr1slot unique", slot_index=99)
        # Seed a transition → retrieval edge
        _insert_transition(e2e_engines, mid_a, mid_b, count=3)

        result = GraphAPI(storage).get_full_graph()
        edges = result["edges"]

        valid_roles = {"retrieval", "informational"}
        for edge in edges:
            assert "role" in edge, f"Edge missing 'role' field: {edge}"
            assert edge["role"] in valid_roles, (
                f"BC-VZ-R1: edge role must be in {valid_roles}, got {edge['role']!r}. Edge: {edge}"
            )

    def test_transition_edges_have_retrieval_role(self, e2e_engines):
        """BC-VZ-R1: transition edges must have role='retrieval'."""
        from yadgar.backend.graph.graph_api import GraphAPI

        storage = e2e_engines["storage"]

        mid_a = _insert_mem(e2e_engines, "vzr1 trans-src xvzr1trn unique")
        mid_b = _insert_mem(e2e_engines, "vzr1 trans-tgt xvzr1trn unique")
        _insert_transition(e2e_engines, mid_a, mid_b, count=5)

        result = GraphAPI(storage).get_full_graph()
        transitions = [e for e in result["edges"] if e.get("type") == "transition"]
        assert transitions, "Expected at least one transition edge"
        for t in transitions:
            assert t["role"] == "retrieval", (
                f"BC-VZ-R1: transition edge role must be 'retrieval', got {t['role']!r}"
            )

    def test_temporal_edges_have_informational_role(self, e2e_engines):
        """BC-VZ-R1: temporal edges must have role='informational' (renamed from display).

        RED on current code — temporal role is 'display' in viz_meta.EDGE_TYPES.
        """
        from yadgar.backend.graph.graph_api import GraphAPI

        storage = e2e_engines["storage"]

        # Slot-sharing memories produce temporal edges
        mid_a = _insert_mem(e2e_engines, "vzr1 temporal-a xvzr1tmp unique", slot_index=77)
        mid_b = _insert_mem(e2e_engines, "vzr1 temporal-b xvzr1tmp unique", slot_index=77)
        _ = (mid_a, mid_b)  # inserted; slot_map driven by query

        result = GraphAPI(storage).get_full_graph()
        temporal = [e for e in result["edges"] if e.get("type") == "temporal"]
        assert temporal, "Expected temporal edges from slot-sharing memories"
        for t in temporal:
            assert t["role"] == "informational", (
                f"BC-VZ-R1: temporal edge role must be 'informational', got {t['role']!r}. "
                "Rename viz_meta.EDGE_TYPES['temporal']['role'] = 'informational'."
            )


# ---------------------------------------------------------------------------
# BC-VZ-R2 — NO semantic edge in default payload
# ---------------------------------------------------------------------------


class TestBCVZR2_NoSemanticInDefaultPayload:
    """BC-VZ-R2: semantic edges SHALL NOT appear in the default /api/graph payload.

    Already enforced since v5.54.3 (lazy path).  Locked here to prevent
    accidental regression.
    """

    def test_no_semantic_edges_in_default_payload(self, e2e_engines):
        """BC-VZ-R2: default /api/graph must never include 'semantic' typed edges."""
        from yadgar.backend.graph.graph_api import GraphAPI

        storage = e2e_engines["storage"]

        # Seed a couple of memories (potential semantic edge candidates)
        _insert_mem(e2e_engines, "vzr2 semantic candidate alpha unique xvzr2")
        _insert_mem(e2e_engines, "vzr2 semantic candidate beta unique xvzr2")

        result = GraphAPI(storage).get_full_graph()
        semantic = [e for e in result["edges"] if e.get("type") == "semantic"]
        assert semantic == [], (
            f"BC-VZ-R2: semantic edges MUST NOT appear in default payload (lazy-only). "
            f"Found {len(semantic)} semantic edge(s). "
            "Check that LAZY_EDGE_TYPES still contains 'semantic'."
        )


# ---------------------------------------------------------------------------
# BC-VZ-R3 — clusters[] in payload reflects real memory_cluster rows
# ---------------------------------------------------------------------------


class TestBCVZR3_ClusterPayload:
    """BC-VZ-R3: get_full_graph() SHALL include clusters[] from real memory_cluster rows.

    RED on current code — clusters[] key is absent from get_full_graph() payload.
    GREEN after: graph_api emits clusters from storage.get_memory_clusters() +
    storage.get_cluster_members(cid), with member_node_ids matching assigned memories.
    """

    def test_clusters_key_present_in_payload(self, e2e_engines):
        """BC-VZ-R3: payload must always carry 'clusters' key (even when empty).

        RED on current code — key is absent entirely.
        """
        from yadgar.backend.graph.graph_api import GraphAPI

        storage = e2e_engines["storage"]
        result = GraphAPI(storage).get_full_graph()
        assert "clusters" in result, (
            "BC-VZ-R3: payload MUST carry 'clusters' key. "
            "Add clusters[] assembly to graph_api.get_full_graph()."
        )
        assert isinstance(result["clusters"], list), (
            f"BC-VZ-R3: 'clusters' must be a list, got {type(result['clusters'])}"
        )

    def test_seeded_cluster_appears_in_payload_with_correct_member_ids(self, e2e_engines):
        """BC-VZ-R3: seeded memory_cluster row must appear in clusters[] with correct member_node_ids.

        Seeds a cluster, assigns two memories to it, asserts both appear
        in member_node_ids of the corresponding clusters[] entry.
        """
        from yadgar.backend.graph.graph_api import GraphAPI

        storage = e2e_engines["storage"]

        # Seed two memories as cluster members
        mid_a = _insert_mem(e2e_engines, "vzr3 cluster member alpha xvzr3 unique")
        mid_b = _insert_mem(e2e_engines, "vzr3 cluster member beta xvzr3 unique")

        # Seed a memory_cluster row
        cid = _insert_cluster(e2e_engines, "vzr3-test-cluster", level=0)

        # Assign memories to the cluster
        _assign_cluster_member(e2e_engines, mid_a, cid)
        _assign_cluster_member(e2e_engines, mid_b, cid)

        result = GraphAPI(storage).get_full_graph()

        assert "clusters" in result, "BC-VZ-R3: 'clusters' key absent from payload"
        clusters = result["clusters"]

        # Find our seeded cluster
        matching = [c for c in clusters if c.get("id") == cid]
        assert matching, (
            f"BC-VZ-R3: seeded cluster (id={cid}) must appear in clusters[]. "
            f"clusters present: {[c.get('id') for c in clusters]}"
        )
        cluster_entry = matching[0]

        assert "member_node_ids" in cluster_entry, (
            f"BC-VZ-R3: cluster entry must have 'member_node_ids'. Got: {cluster_entry}"
        )

        member_ids = set(cluster_entry["member_node_ids"])
        assert f"mem:{mid_a}" in member_ids, (
            f"BC-VZ-R3: mem:{mid_a} must appear in cluster {cid} member_node_ids. Got: {member_ids}"
        )
        assert f"mem:{mid_b}" in member_ids, (
            f"BC-VZ-R3: mem:{mid_b} must appear in cluster {cid} member_node_ids. Got: {member_ids}"
        )

    def test_cluster_entry_has_required_fields(self, e2e_engines):
        """BC-VZ-R3: each clusters[] entry must have id, source, label, level, member_node_ids."""
        from yadgar.backend.graph.graph_api import GraphAPI

        storage = e2e_engines["storage"]

        cid = _insert_cluster(e2e_engines, "vzr3-fields-test-cluster", level=1)
        mid = _insert_mem(e2e_engines, "vzr3 fields test member xvzr3fld unique")
        _assign_cluster_member(e2e_engines, mid, cid)

        result = GraphAPI(storage).get_full_graph()
        clusters = result.get("clusters", [])
        matching = [c for c in clusters if c.get("id") == cid]
        assert matching, f"BC-VZ-R3: seeded cluster id={cid} missing from clusters[]"
        entry = matching[0]

        required = ["id", "source", "label", "level", "member_node_ids"]
        for field in required:
            assert field in entry, (
                f"BC-VZ-R3: cluster entry missing required field '{field}'. Got: {entry}"
            )
        assert entry["source"] == "memory_cluster", (
            f"BC-VZ-R3: cluster source must be 'memory_cluster', got {entry['source']!r}"
        )
        assert entry["level"] == 1, (
            f"BC-VZ-R3: cluster level must match seeded value 1, got {entry['level']}"
        )


# ---------------------------------------------------------------------------
# BC-VZ-R4 — memory_similarity_link edges emitted with role="informational"
# ---------------------------------------------------------------------------


class TestBCVZR4_SimilarityLinkEdges:
    """BC-VZ-R4: memory_similarity_link rows SHALL appear as edges with role='informational'.

    RED on current code — no similarity_link edge builder exists in graph_api.
    GREEN after: _build_similarity_link_edges() added; EDGE_TYPES has
    'memory_similarity_link' key with role='informational'.
    """

    def test_seeded_similarity_link_appears_as_edge(self, e2e_engines):
        """BC-VZ-R4: seeded memory_similarity_link must appear in edges[] with type=memory_similarity_link."""
        from yadgar.backend.graph.graph_api import GraphAPI

        storage = e2e_engines["storage"]

        mid_a = _insert_mem(e2e_engines, "vzr4 simlink src xvzr4sl unique")
        mid_b = _insert_mem(e2e_engines, "vzr4 simlink tgt xvzr4sl unique")
        _insert_similarity_link(e2e_engines, mid_a, mid_b, weight=0.92)

        result = GraphAPI(storage).get_full_graph()
        sim_edges = [e for e in result["edges"] if e.get("type") == "memory_similarity_link"]
        assert sim_edges, (
            "BC-VZ-R4: seeded memory_similarity_link must appear in edges[] with "
            "type='memory_similarity_link'. Add _build_similarity_link_edges() to graph_api."
        )

        expected_src = f"mem:{min(mid_a, mid_b)}"
        expected_tgt = f"mem:{max(mid_a, mid_b)}"
        matching = [
            e
            for e in sim_edges
            if {e.get("source"), e.get("target")} == {expected_src, expected_tgt}
        ]
        assert matching, (
            f"BC-VZ-R4: expected similarity_link edge ({expected_src} ↔ {expected_tgt}). "
            f"sim_edges found: {sim_edges[:3]}"
        )

    def test_similarity_link_edge_has_informational_role(self, e2e_engines):
        """BC-VZ-R4: memory_similarity_link edges must have role='informational'."""
        from yadgar.backend.graph.graph_api import GraphAPI

        storage = e2e_engines["storage"]

        mid_a = _insert_mem(e2e_engines, "vzr4 role-check src xvzr4rc unique")
        mid_b = _insert_mem(e2e_engines, "vzr4 role-check tgt xvzr4rc unique")
        _insert_similarity_link(e2e_engines, mid_a, mid_b, weight=0.85)

        result = GraphAPI(storage).get_full_graph()
        sim_edges = [e for e in result["edges"] if e.get("type") == "memory_similarity_link"]
        assert sim_edges, "BC-VZ-R4: no memory_similarity_link edges in payload"
        for e in sim_edges:
            assert e.get("role") == "informational", (
                f"BC-VZ-R4: similarity_link edge must have role='informational', got {e.get('role')!r}"
            )
