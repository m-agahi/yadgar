"""Tests for _merge_duplicates numpy implementation (§14b).

Verifies:
  1. Functional parity with the documented algorithm (exact-content + cosine similarity)
  2. Performance: 500-memory input completes in < 100 ms (numpy matmul path)

Note: _merge_duplicates was converted to numpy in §12. These tests lock in
correctness and the perf contract.
"""

import time
from unittest.mock import MagicMock

import numpy as np

from yadgar._shared.config import Settings
from yadgar.core.consolidation import ConsolidationScheduler as ConsolidationEngine

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_unit_vec(dim: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(dim).astype(np.float32)
    return v / np.linalg.norm(v)


def _vec_to_bytes(v: np.ndarray) -> bytes:
    return v.astype(np.float32).tobytes()


def _near_duplicate_vec(base: np.ndarray, noise: float = 0.01) -> np.ndarray:
    """Return a unit vector very close to base (cosine sim > 0.95)."""
    rng = np.random.default_rng(42)
    perturbed = base + noise * rng.standard_normal(base.shape).astype(np.float32)
    return perturbed / np.linalg.norm(perturbed)


def _orthogonal_vec(base: np.ndarray, dim: int) -> np.ndarray:
    """Return a unit vector roughly orthogonal to base."""
    rng = np.random.default_rng(99)
    v = rng.standard_normal(dim).astype(np.float32)
    # Gram-Schmidt
    v -= np.dot(v, base) * base
    return v / np.linalg.norm(v)


def _make_engine_with_memories(memories: list[dict]) -> ConsolidationEngine:
    """Build a ConsolidationEngine with a mock storage returning `memories`."""
    settings = Settings(SIMILARITY_MATRIX_MAX_CANDIDATES=len(memories) + 10)
    storage = MagicMock()
    storage.get_memories_with_embeddings.return_value = memories

    deleted: list[int] = []

    def _delete(mid):
        deleted.append(mid)

    storage.delete_memory.side_effect = _delete
    storage._deleted = deleted

    embeddings = MagicMock()
    retriever = MagicMock()

    engine = ConsolidationEngine.__new__(ConsolidationEngine)
    engine._settings = settings
    engine._storage = storage
    engine._embeddings = embeddings
    engine._retriever = retriever
    return engine


# ---------------------------------------------------------------------------
# §14b-1: Functional parity — exact content duplicates
# ---------------------------------------------------------------------------


class TestMergeDuplicatesExactContent:
    def test_exact_duplicate_lower_heat_deleted(self):
        """Exact-content duplicate: keep higher-heat, delete lower-heat."""
        dim = 64
        vec = _make_unit_vec(dim, seed=1)
        memories = [
            {
                "id": 1,
                "content": "same content",
                "embedding": _vec_to_bytes(vec),
                "heat": 0.8,
            },
            {
                "id": 2,
                "content": "same content",
                "embedding": _vec_to_bytes(vec),
                "heat": 0.5,
            },
        ]
        engine = _make_engine_with_memories(memories)
        stats = {"memories_deleted": 0}
        engine._merge_duplicates(stats)

        deleted = engine._storage._deleted
        assert 2 in deleted, f"Expected id=2 (lower heat=0.5) to be deleted; deleted={deleted}"
        assert 1 not in deleted, "id=1 (higher heat=0.8) must be kept"
        assert stats["memories_deleted"] == 1

    def test_exact_duplicate_keep_hotter_when_reversed(self):
        """Exact content: later memory is hotter — it evicts the first."""
        dim = 64
        vec = _make_unit_vec(dim, seed=2)
        memories = [
            {"id": 10, "content": "abc", "embedding": _vec_to_bytes(vec), "heat": 0.3},
            {"id": 11, "content": "abc", "embedding": _vec_to_bytes(vec), "heat": 0.9},
        ]
        engine = _make_engine_with_memories(memories)
        stats = {"memories_deleted": 0}
        engine._merge_duplicates(stats)

        deleted = engine._storage._deleted
        assert 10 in deleted
        assert 11 not in deleted

    def test_no_deletion_for_distinct_content(self):
        """Distinct content: nothing should be deleted."""
        dim = 64
        v1 = _make_unit_vec(dim, seed=3)
        v2 = _make_unit_vec(dim, seed=4)
        memories = [
            {"id": 20, "content": "content A", "embedding": _vec_to_bytes(v1), "heat": 1.0},
            {"id": 21, "content": "content B", "embedding": _vec_to_bytes(v2), "heat": 1.0},
        ]
        engine = _make_engine_with_memories(memories)
        stats = {"memories_deleted": 0}
        engine._merge_duplicates(stats)

        assert stats["memories_deleted"] == 0


# ---------------------------------------------------------------------------
# §14b-2: Functional parity — embedding similarity duplicates
# ---------------------------------------------------------------------------


class TestMergeDuplicatesEmbeddingSimilarity:
    def test_near_duplicate_embedding_deleted(self):
        """Near-duplicate embeddings (cosine > 0.95): lower-heat is deleted."""
        dim = 64
        base = _make_unit_vec(dim, seed=5)
        near = _near_duplicate_vec(base, noise=0.005)

        # Verify our test setup actually creates a near-duplicate
        sim = float(np.dot(base, near))
        assert sim > 0.95, f"Test setup: expected cosine > 0.95, got {sim:.4f}"

        memories = [
            {
                "id": 30,
                "content": "unique content A",
                "embedding": _vec_to_bytes(base),
                "heat": 1.0,
            },
            {
                "id": 31,
                "content": "unique content B",  # different content, same embedding space
                "embedding": _vec_to_bytes(near),
                "heat": 0.4,
            },
        ]
        engine = _make_engine_with_memories(memories)
        stats = {"memories_deleted": 0}
        engine._merge_duplicates(stats)

        deleted = engine._storage._deleted
        assert 31 in deleted, f"Expected lower-heat near-duplicate (id=31) deleted; got {deleted}"
        assert 30 not in deleted

    def test_orthogonal_embeddings_not_deleted(self):
        """Orthogonal embeddings (cosine ≈ 0): should NOT be merged."""
        dim = 64
        base = _make_unit_vec(dim, seed=6)
        orth = _orthogonal_vec(base, dim)

        sim = abs(float(np.dot(base, orth)))
        assert sim < 0.3, f"Test setup: expected near-orthogonal, got cosine={sim:.4f}"

        memories = [
            {
                "id": 40,
                "content": "content C",
                "embedding": _vec_to_bytes(base),
                "heat": 1.0,
            },
            {
                "id": 41,
                "content": "content D",
                "embedding": _vec_to_bytes(orth),
                "heat": 1.0,
            },
        ]
        engine = _make_engine_with_memories(memories)
        stats = {"memories_deleted": 0}
        engine._merge_duplicates(stats)

        assert stats["memories_deleted"] == 0


# ---------------------------------------------------------------------------
# §14b-3: Performance — 500 memories < 100 ms
# ---------------------------------------------------------------------------


class TestMergeDuplicatesPerformance:
    def test_500_memories_under_100ms(self):
        """Numpy matmul path must process 500 memories in < 100 ms."""
        dim = 384  # real model dimension
        rng = np.random.default_rng(42)
        n = 500

        vecs = []
        for _i in range(n):
            v = rng.standard_normal(dim).astype(np.float32)
            vecs.append(v / np.linalg.norm(v))

        memories = [
            {
                "id": i,
                "content": f"memory content {i}",
                "embedding": _vec_to_bytes(vecs[i]),
                "heat": rng.random(),
            }
            for i in range(n)
        ]

        engine = _make_engine_with_memories(memories)
        stats = {"memories_deleted": 0}

        t0 = time.perf_counter()
        engine._merge_duplicates(stats)
        elapsed_ms = (time.perf_counter() - t0) * 1000

        assert elapsed_ms < 100, (
            f"_merge_duplicates took {elapsed_ms:.1f} ms for N=500 — expected < 100 ms. "
            "The numpy matmul path is required for this bound."
        )

    def test_500_memories_with_cluster_under_100ms(self):
        """500 memories with a 10-way near-duplicate cluster still < 100 ms."""
        dim = 384
        rng = np.random.default_rng(123)
        n = 500
        cluster_size = 10

        base_cluster = rng.standard_normal(dim).astype(np.float32)
        base_cluster /= np.linalg.norm(base_cluster)

        memories = []
        for i in range(n):
            if i < cluster_size:
                # Near duplicates
                v = base_cluster + 0.005 * rng.standard_normal(dim).astype(np.float32)
            else:
                v = rng.standard_normal(dim).astype(np.float32)
            v /= np.linalg.norm(v)
            memories.append(
                {
                    "id": i,
                    "content": f"content {i}",
                    "embedding": _vec_to_bytes(v),
                    "heat": rng.random(),
                }
            )

        engine = _make_engine_with_memories(memories)
        stats = {"memories_deleted": 0}

        t0 = time.perf_counter()
        engine._merge_duplicates(stats)
        elapsed_ms = (time.perf_counter() - t0) * 1000

        # Cluster members (minus the hottest) should be deleted
        assert stats["memories_deleted"] > 0, "Expected at least some cluster deletions"
        assert elapsed_ms < 100, f"Took {elapsed_ms:.1f} ms — expected < 100 ms"
