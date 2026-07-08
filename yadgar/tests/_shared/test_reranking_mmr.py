"""Tests for yadgar/retrieval/_reranking_mmr.py — _MMRMixin.

Covers mmr_rerank(): early-exit guards (empty, single item, None embedding),
valid-memory filtering (dim mismatch, no embedding), cosine similarity with
None embeddings, zero-norm vector branch, diversity selection via lambda,
and top_k truncation.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pytest

from yadgar._shared.retrieval._reranking_mmr import _MMRMixin

# ---------------------------------------------------------------------------
# Stub class
# ---------------------------------------------------------------------------


DIM = 4  # small dimension — keeps tests fast, dim-match logic identical


def _vec(*values: float) -> bytes:
    """Create float32 bytes from explicit values."""
    return np.array(values, dtype=np.float32).tobytes()


def _rand_vec(seed: int, dim: int = DIM) -> bytes:
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(dim).astype(np.float32)
    v = v / (np.linalg.norm(v) + 1e-9)
    return v.tobytes()


class _StubReranker(_MMRMixin):
    """Minimal host providing _storage.get_memory()."""

    def __init__(self, mem_store: dict | None = None):
        self._storage = MagicMock()
        self._mem_store: dict[str, dict] = mem_store or {}
        self._storage.get_memory.side_effect = lambda mem_id: self._mem_store.get(mem_id)


@pytest.fixture()
def reranker():
    return _StubReranker()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mem(mem_id: str, retrieval_score: float = 0.5) -> dict:
    return {"id": mem_id, "_retrieval_score": retrieval_score}


# ---------------------------------------------------------------------------
# Early-exit guard paths
# ---------------------------------------------------------------------------


class TestGuardPaths:
    def test_empty_memories_returns_empty(self, reranker):
        result = reranker.mmr_rerank([], _rand_vec(1), top_k=5)
        assert result == []

    def test_single_memory_returns_it(self, reranker):
        mems = [_mem("m1", 0.9)]
        result = reranker.mmr_rerank(mems, _rand_vec(1), top_k=5)
        assert result == mems

    def test_none_query_embedding_returns_slice(self, reranker):
        mems = [_mem("m1"), _mem("m2"), _mem("m3")]
        result = reranker.mmr_rerank(mems, None, top_k=2)
        assert result == mems[:2]

    def test_none_query_with_single_memory(self, reranker):
        """Single memory + None embedding: still returns single item."""
        mems = [_mem("m1")]
        result = reranker.mmr_rerank(mems, None, top_k=3)
        assert result == mems


# ---------------------------------------------------------------------------
# Dimension mismatch / no embedding paths
# ---------------------------------------------------------------------------


class TestDimMismatchAndMissingEmbedding:
    def test_all_dim_mismatch_returns_input_slice(self):
        """All memories have wrong-dim embeddings → valid_memories all added without emb → still returned."""
        # Memories stored with DIM*2 dimensions (will mismatch query at DIM)
        store = {
            "m1": {"embedding": _rand_vec(1, dim=DIM * 2)},
            "m2": {"embedding": _rand_vec(2, dim=DIM * 2)},
        }
        r = _StubReranker(store)
        q_emb = _rand_vec(1, dim=DIM)
        mems = [_mem("m1", 0.9), _mem("m2", 0.8)]
        # dim mismatch: emb appended as None, valid_memories populated
        result = r.mmr_rerank(mems, q_emb, top_k=2)
        assert len(result) == 2

    def test_no_embedding_in_storage_record(self):
        """Memory whose storage record has no 'embedding' key → emb=None."""
        store = {"m1": {"content": "something"}, "m2": {"content": "else"}}
        r = _StubReranker(store)
        q_emb = _rand_vec(1)
        mems = [_mem("m1", 0.9), _mem("m2", 0.8)]
        result = r.mmr_rerank(mems, q_emb, top_k=2)
        assert len(result) == 2

    def test_storage_returns_none_for_memory(self):
        """get_memory returns None → memory appended with None embedding."""
        r = _StubReranker({})  # store is empty, get_memory returns None
        q_emb = _rand_vec(1)
        mems = [_mem("missing1", 0.9), _mem("missing2", 0.8)]
        result = r.mmr_rerank(mems, q_emb, top_k=2)
        assert len(result) == 2

    def test_none_embedding_in_cosine_sim(self):
        """When a selected embedding is None, cosine_sim returns 0.0 (no crash)."""
        # First memory has a valid embedding; second has None (no embedding in store).
        # Second memory gets selected second, cosine_sim(its_emb=None, sel_emb) → 0.0.
        store = {"m1": {"embedding": _rand_vec(1)}}
        # m2 has no record → None emb
        r = _StubReranker(store)
        q_emb = _rand_vec(1)
        mems = [_mem("m1", 0.9), _mem("m2", 0.8), _mem("m3", 0.7)]
        # m3 also has no record → None emb
        result = r.mmr_rerank(mems, q_emb, top_k=3)
        assert len(result) == 3

    def test_cosine_sim_none_selected_emb(self):
        """cosine_sim(valid_emb, None) → 0.0 via the `a is None or b is None` branch.

        This covers line 48: occurs when a previously-selected memory had None
        embedding and a later candidate has a valid embedding.
        """
        # m1 has no storage record → None emb; m2 has valid emb
        # With lambda=1, m1 is selected first (highest relevance), then m2 is
        # considered: cosine_sim(m2_emb, selected_emb=None) hits line 48.
        store = {"m2": {"embedding": _rand_vec(2)}}
        r = _StubReranker(store)
        q_emb = _rand_vec(1)
        # m1 first (no storage record → None emb), m2 second
        mems = [_mem("m1", 0.9), _mem("m2", 0.8)]
        result = r.mmr_rerank(mems, q_emb, top_k=2, lambda_param=1.0)
        assert len(result) == 2

    def test_zero_norm_embedding_cosine_sim_returns_zero(self):
        """Zero-vector embedding → cosine_sim returns 0.0 via the else branch."""
        zero_emb = np.zeros(DIM, dtype=np.float32).tobytes()
        store = {
            "m1": {"embedding": zero_emb},
            "m2": {"embedding": _rand_vec(2)},
        }
        r = _StubReranker(store)
        q_emb = _rand_vec(1)
        mems = [_mem("m1", 0.9), _mem("m2", 0.8)]
        result = r.mmr_rerank(mems, q_emb, top_k=2)
        # Should not raise; zero norm goes through the `else 0.0` branch
        assert len(result) == 2


# ---------------------------------------------------------------------------
# MMR selection logic
# ---------------------------------------------------------------------------


class TestMMRSelection:
    def test_top_k_limits_output(self):
        """Result has at most top_k items."""
        store = {f"m{i}": {"embedding": _rand_vec(i + 10)} for i in range(6)}
        r = _StubReranker(store)
        q_emb = _rand_vec(0)
        mems = [_mem(f"m{i}", float(i) / 10) for i in range(6)]
        result = r.mmr_rerank(mems, q_emb, top_k=3)
        assert len(result) == 3

    def test_diversity_lambda_0_prefers_novel(self):
        """lambda=0.0 maximises diversity — second pick diverges from first."""
        # Two near-identical vectors and one orthogonal vector.
        # With lambda=0 only diversity matters; orthogonal one should be selected.
        v_a = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
        v_b = np.array([0.99, 0.1, 0.0, 0.0], dtype=np.float32)  # almost same as a
        v_c = np.array([0.0, 0.0, 1.0, 0.0], dtype=np.float32)  # orthogonal to a
        store = {
            "m1": {"embedding": v_a.tobytes()},
            "m2": {"embedding": v_b.tobytes()},
            "m3": {"embedding": v_c.tobytes()},
        }
        r = _StubReranker(store)
        # Query aligns with m1 and m2; m3 is divergent
        q_emb = v_a.tobytes()
        mems = [_mem("m1", 0.9), _mem("m2", 0.85), _mem("m3", 0.5)]
        result = r.mmr_rerank(mems, q_emb, top_k=2, lambda_param=0.0)
        ids = [m["id"] for m in result]
        # m1 selected first (all zero relevance * lambda=0, but best diversity starter)
        # m3 should be selected second over m2 since it's maximally diverse from m1
        assert "m3" in ids

    def test_diversity_lambda_1_pure_relevance(self):
        """lambda=1.0 pure relevance — result order by _retrieval_score descending."""
        store = {
            "m1": {"embedding": _rand_vec(1)},
            "m2": {"embedding": _rand_vec(2)},
            "m3": {"embedding": _rand_vec(3)},
        }
        r = _StubReranker(store)
        q_emb = _rand_vec(0)
        mems = [_mem("m1", 0.9), _mem("m2", 0.7), _mem("m3", 0.5)]
        result = r.mmr_rerank(mems, q_emb, top_k=3, lambda_param=1.0)
        # With lambda=1 and no diversity penalty, descending relevance order
        assert result[0]["id"] == "m1"
        assert result[1]["id"] == "m2"
        assert result[2]["id"] == "m3"

    def test_default_lambda_returns_correct_count(self):
        """Default lambda=0.7 runs without error and returns top_k items."""
        store = {f"m{i}": {"embedding": _rand_vec(i)} for i in range(4)}
        r = _StubReranker(store)
        q_emb = _rand_vec(99)
        mems = [_mem(f"m{i}", 0.8 - i * 0.1) for i in range(4)]
        result = r.mmr_rerank(mems, q_emb, top_k=3)
        assert len(result) == 3

    def test_top_k_larger_than_candidates_returns_all(self):
        """top_k > len(memories) → all candidates returned (min guard)."""
        store = {"m1": {"embedding": _rand_vec(1)}, "m2": {"embedding": _rand_vec(2)}}
        r = _StubReranker(store)
        q_emb = _rand_vec(0)
        mems = [_mem("m1", 0.9), _mem("m2", 0.8)]
        result = r.mmr_rerank(mems, q_emb, top_k=10)
        assert len(result) == 2

    def test_mixed_valid_invalid_embeddings(self):
        """Some memories have valid embeddings, others None — all end up in valid_memories."""
        store = {
            "m1": {"embedding": _rand_vec(1)},
            # m2 is not in store → returns None
        }
        r = _StubReranker(store)
        q_emb = _rand_vec(1)
        mems = [_mem("m1", 0.9), _mem("m2", 0.8)]
        result = r.mmr_rerank(mems, q_emb, top_k=2)
        assert len(result) == 2
        ids = {m["id"] for m in result}
        assert ids == {"m1", "m2"}

    def test_duplicate_ids_handled(self):
        """Duplicate memory IDs in the input: each is treated independently."""
        store = {"m1": {"embedding": _rand_vec(1)}}
        r = _StubReranker(store)
        q_emb = _rand_vec(1)
        mems = [_mem("m1", 0.9), _mem("m1", 0.9)]
        result = r.mmr_rerank(mems, q_emb, top_k=2)
        assert len(result) == 2

    def test_candidates_removed_after_selection(self):
        """Each memory is selected at most once (candidates.remove in loop)."""
        store = {f"m{i}": {"embedding": _rand_vec(i)} for i in range(5)}
        r = _StubReranker(store)
        q_emb = _rand_vec(0)
        mems = [_mem(f"m{i}", 0.9 - i * 0.1) for i in range(5)]
        result = r.mmr_rerank(mems, q_emb, top_k=5)
        ids = [m["id"] for m in result]
        assert len(ids) == len(set(ids))  # no duplicates

    def test_reads_in_dict_embedding_without_fetch(self):
        """v5.97 Fix 2: when the fused result dict already carries `embedding`,
        MMR reads it in-place and issues ZERO storage.get_memory calls.
        """
        emb1, emb2, emb3 = _rand_vec(1), _rand_vec(2), _rand_vec(3)
        r = _StubReranker({})  # empty store — any fetch would return None
        q_emb = _rand_vec(0)
        mems = [
            {"id": "m1", "_retrieval_score": 0.9, "embedding": emb1},
            {"id": "m2", "_retrieval_score": 0.8, "embedding": emb2},
            {"id": "m3", "_retrieval_score": 0.7, "embedding": emb3},
        ]
        result = r.mmr_rerank(mems, q_emb, top_k=3, lambda_param=1.0)
        assert r._storage.get_memory.call_count == 0, (
            f"in-dict embeddings must skip the fetch; got {r._storage.get_memory.call_count} fetches"
        )
        # pure-relevance order preserved with in-dict embeddings
        assert [m["id"] for m in result] == ["m1", "m2", "m3"]

    def test_falls_back_to_fetch_when_embedding_missing_in_dict(self):
        """A candidate without an in-dict embedding (e.g. a CE-diversity or
        comparison inject) still gets its embedding via storage.get_memory —
        preserves MMR parity for injected candidates.
        """
        emb2 = _rand_vec(2)
        store = {"m2": {"embedding": emb2}}  # only m2 fetchable
        r = _StubReranker(store)
        q_emb = _rand_vec(0)
        mems = [
            {"id": "m1", "_retrieval_score": 0.9, "embedding": _rand_vec(1)},  # in-dict
            {"id": "m2", "_retrieval_score": 0.8},  # no in-dict emb → fetch fallback
        ]
        result = r.mmr_rerank(mems, q_emb, top_k=2)
        # exactly one fetch — only for the candidate missing an in-dict embedding
        assert r._storage.get_memory.call_count == 1
        assert {m["id"] for m in result} == {"m1", "m2"}

    def test_golden_mmr_order_lambda_07(self):
        """Golden-order regression: lambda=0.7 produces a specific deterministic ranking.

        Seed-based vectors ensure reproducibility. Order captured from the original
        implementation and MUST remain identical after any refactor.
        """
        store = {f"m{i}": {"embedding": _rand_vec(i)} for i in range(5)}
        r = _StubReranker(store)
        q_emb = _rand_vec(99)
        mems = [{"id": f"m{i}", "_retrieval_score": 0.8 - i * 0.1} for i in range(5)]
        result = r.mmr_rerank(mems, q_emb, top_k=4, lambda_param=0.7)
        assert [m["id"] for m in result] == ["m0", "m1", "m3", "m2"]
