"""Phase-3 behavior-contract e2e tests — coverage closure (v5.75, train T5 #47).

Target:

  BC-CM1  SR transition matrix built and navigate_to() produces ranked results
          when the transition table has sufficient data (≥ _MIN_TRANSITIONS = 20).
          Drives: CognitiveMap.compute_sr_matrix() + navigate_to() via a real
          StorageEngine seeded with 21 memory rows and 20 sequential transitions.
          Decision: cognitive_map is WIRED (record_transition in recall, navigate_to
          in restoration.py / CheckpointRestore._predict_memories) and its results
          are consumed (sr_results iterated into predicted list).  STATUS → LIVE.
          Do NOT delete.

ANTI-BENDING: real assertions, no mock of the unit under test.
Same @pytest.mark.e2e discipline as phase1/phase2.
Run: make e2e  (excluded from CI pytest selection via -m 'not e2e')
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.e2e

_E2E_BRANCH = "feat/v6-t5-e2e-cogmap"


# ---------------------------------------------------------------------------
# Helpers (mirror Phase-1 / Phase-2 seeding style)
# ---------------------------------------------------------------------------


def _embed(e2e_engines, content: str) -> bytes:
    return e2e_engines["embeddings"].encode(content)


def _insert_mem(
    e2e_engines,
    content: str,
    directory: str,
    *,
    heat: float = 0.8,
    tags: list[str] | None = None,
) -> int:
    """Insert a memory with a real embedding; return the row id (seeding only)."""
    storage = e2e_engines["storage"]
    emb = _embed(e2e_engines, content)
    now = datetime.now(UTC).isoformat()
    doc = {
        "content": content,
        "embedding": emb,
        "directory_context": directory,
        "heat": heat,
        "tags": tags or [],
        "last_accessed": now,
        "created_at": now,
        "access_count": 0,
        "is_protected": False,
    }
    return storage.insert_memory(doc)


# ===========================================================================
# BC-CM1 — SR transition matrix built and navigate_to() produces results
#
# CognitiveMap.compute_sr_matrix() is exercised when:
#   (a) ≥ _MIN_TRANSITIONS = 20 transitions are present (has_sufficient_data())
#   (b) navigate_to() is called with a real query embedding
#
# Both preconditions must be met; without them the early-return paths fire
# and the matrix is never computed.  This test seeds 21 memory rows and 20
# transitions (sequential: 0→1→2→...→19→20), then asserts:
#   - has_sufficient_data() → True
#   - compute_sr_matrix() returns a non-empty square matrix
#   - navigate_to() returns a non-empty ranked list
#   - the ranked list contains (memory_id, proximity_score) tuples with
#     proximity > 0 (meaning the SR path executed, not just the fallback [])
#
# This proves BC-CM1: "SR transition matrix built" — the contract says the
# matrix is constructed and used for retrieval.  Wiring decision: LIVE (see
# module docstring).
# ===========================================================================

_MIN_TRANSITIONS = 20  # mirrors cognitive_map._MIN_TRANSITIONS


class TestBCCM1_SRTransitionMatrixBuilt:
    """BC-CM1: SR transition matrix SHALL be built when ≥20 transitions exist."""

    def _seed_memories_and_transitions(self, e2e_engines, n_mems: int = 21) -> list[int]:
        """Insert n_mems memories and n_mems-1 sequential transitions.

        Returns the list of inserted memory ids.
        """
        storage = e2e_engines["storage"]
        yadgar_dir = e2e_engines["yadgar_dir"]

        ids: list[int] = []
        for i in range(n_mems):
            mid = _insert_mem(
                e2e_engines,
                f"BC-CM1 SR transition matrix test memory {i:03d} xcm1sr{i:05d}",
                yadgar_dir,
                heat=0.5 + (i % 5) * 0.1,
            )
            ids.append(mid)

        # Seed n_mems-1 transitions (i → i+1) so total count = n_mems-1 >= 20
        # That meets the _MIN_TRANSITIONS=20 requirement with n_mems=21.
        # Each transition entry has count=1; total_count = n_mems-1 = 20.
        for i in range(len(ids) - 1):
            storage.insert_transition(
                {
                    "from_memory_id": ids[i],
                    "to_memory_id": ids[i + 1],
                    "count": 1,
                    "session_id": "e2e-bcm1",
                }
            )

        return ids

    def test_compute_sr_matrix_returns_nonzero_matrix(self, e2e_engines):
        """compute_sr_matrix() SHALL return a non-empty square matrix
        when ≥ _MIN_TRANSITIONS transitions are present.
        """
        from yadgar.cognitive_map import CognitiveMap
        from yadgar.config import Settings

        storage = e2e_engines["storage"]
        settings = Settings(DB_PATH=e2e_engines["db_path"])
        ids = self._seed_memories_and_transitions(e2e_engines, n_mems=21)

        cmap = CognitiveMap(storage, settings)

        # Pre-condition: has_sufficient_data must be True with 20 transitions
        assert cmap.has_sufficient_data(), (
            f"BC-CM1 setup: has_sufficient_data() must return True with "
            f"{len(ids) - 1} seeded transitions (need >= {_MIN_TRANSITIONS}). "
            "Check get_all_transitions() and the insert_transition() seeding above."
        )

        # Exercise the main contract — compute_sr_matrix must produce a matrix
        sr = cmap.compute_sr_matrix()

        assert sr is not None, "BC-CM1: compute_sr_matrix() must return a numpy array, not None"
        assert sr.ndim == 2, f"BC-CM1: SR matrix must be 2-D, got ndim={sr.ndim}"
        n = sr.shape[0]
        assert n > 0, (
            f"BC-CM1: SR matrix MUST be non-empty when transitions exist. Got shape={sr.shape}."
        )
        assert sr.shape == (n, n), f"BC-CM1: SR matrix must be square. Got shape={sr.shape}."
        assert not cmap._dirty, "BC-CM1: compute_sr_matrix() must clear the dirty flag"

    def test_navigate_to_returns_ranked_results(self, e2e_engines):
        """navigate_to() SHALL return a non-empty ranked list of (memory_id, proximity)
        when the SR matrix has sufficient data.

        This exercises the full forward path: compute_sr_matrix() → extract_coordinates()
        → search_vectors() → aggregate → rank.  Proves the SR result is consumable
        for predictive retrieval (the path CheckpointRestore._predict_memories uses).
        """
        from yadgar.cognitive_map import CognitiveMap
        from yadgar.config import Settings

        storage = e2e_engines["storage"]
        embeddings = e2e_engines["embeddings"]
        settings = Settings(DB_PATH=e2e_engines["db_path"])

        ids = self._seed_memories_and_transitions(e2e_engines, n_mems=21)

        cmap = CognitiveMap(storage, settings)

        # Verify pre-condition
        assert cmap.has_sufficient_data(), (
            "BC-CM1 navigate_to setup: has_sufficient_data() must be True."
        )

        # Use one of the seeded memory contents as the query
        query_content = f"BC-CM1 SR transition matrix test memory 010 xcm1sr{10:05d}"
        query_emb = embeddings.encode(query_content)
        assert query_emb is not None and len(query_emb) > 0, (
            "BC-CM1 navigate_to setup: query embedding must be non-empty."
        )

        results = cmap.navigate_to(query_emb, embeddings, top_k=5)

        assert len(results) > 0, (
            "BC-CM1: navigate_to() MUST return a non-empty ranked list when the "
            "SR matrix has sufficient data. Got []. "
            "Possible regression in extract_coordinates() / search_vectors() path. "
            f"memory ids seeded: {ids[:5]}... (first 5 of {len(ids)})"
        )

        # Each result must be (int, float) with positive proximity
        for i, (mid, proximity) in enumerate(results):
            assert isinstance(mid, int), (
                f"BC-CM1: result[{i}] memory_id must be int, got {type(mid).__name__}"
            )
            assert proximity > 0, f"BC-CM1: result[{i}] proximity must be > 0, got {proximity}"

        # Results must be sorted by proximity descending
        proximities = [p for _, p in results]
        assert proximities == sorted(proximities, reverse=True), (
            f"BC-CM1: navigate_to() results must be ranked by proximity descending. "
            f"Got: {proximities}"
        )
