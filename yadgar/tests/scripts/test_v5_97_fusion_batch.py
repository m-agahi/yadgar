"""v5.97.0 — batched fusion final-result fetch (N+1 → single query) parity + call-count.

`_build_initial_results` (fusion.py) previously issued one `get_memory` point-read per
fused candidate id — 52-55 serial HTTP round-trips per recall (the N+1 bottleneck the
warm-recall profile flagged, ~1100 ms). v5.97.0 collapses those into a single
`get_memories_by_ids` batch (`SELECT * FROM memory WHERE id IN [memory:N, ...]`),
mirroring the v5.96 priors template, and preserves fused ordering + the
`heat >= min_heat` filter in Python.

Tests guard:

1. PARITY (real store): `get_memories_by_ids` returns rows byte-identical to the old
   per-id `get_memory` semantics for a mix of present / absent / duplicate ids —
   including the `embedding` bytes (MMR depends on np.frombuffer(embedding) being
   byte-identical) and the five nullable setdefault fields. Run against a live
   StorageEngine (server mode if the `surreal` binary is on PATH, else embedded) so
   the `IN [...]` construct is exercised cross-mode.
2. ONE QUERY: the batch method issues exactly ONE `_q` call for N ids, not N.
3. ONE FETCH: `_build_initial_results` calls the batch fetch once, not N times.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from yadgar.backend.retrieval.fusion import _FusionMixin
from yadgar.core import server


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("v5_97_fusion_batch")
    server.init_engines(
        db_path=str(tmp_path / "test_fusion_batch.db"), embedding_model="all-MiniLM-L6-v2"
    )
    yield
    server.shutdown()


@pytest.fixture()
def storage(_engines):
    from yadgar._shared.runtime.lifecycle import _get_storage

    return _get_storage()


_DIR = "/tmp/test_fusion_batch_proj"


def _emb_bytes(seed: int, dim: int = 384) -> bytes:
    rng = np.random.default_rng(seed)
    return rng.standard_normal(dim).astype(np.float32).tobytes()


def _insert_memory(
    storage, content: str, *, heat: float = 0.5, embedding: bytes | None = None
) -> int:
    mid = storage._next_id("memory")
    if embedding is not None:
        # store embedding as a float list (SurrealDB native); _row_to_dict converts
        # back to bytes on read — mirrors the real write path.
        floats = [float(x) for x in np.frombuffer(embedding, dtype=np.float32)]
        storage._q(
            "CREATE type::record('memory', $id) SET "
            "content = $content, directory_context = $dir, heat = $heat, embedding = $emb",
            {"id": mid, "content": content, "dir": _DIR, "heat": heat, "emb": floats},
        )
    else:
        storage._q(
            "CREATE type::record('memory', $id) SET "
            "content = $content, directory_context = $dir, heat = $heat",
            {"id": mid, "content": content, "dir": _DIR, "heat": heat},
        )
    return mid


# ---------------------------------------------------------------------------
# 1. PARITY — batched get_memories_by_ids == old per-id get_memory
# ---------------------------------------------------------------------------


class TestBatchFetchParity:
    def test_batched_matches_per_id_full_row(self, storage):
        emb = _emb_bytes(1)
        m1 = _insert_memory(storage, "first", heat=0.7, embedding=emb)
        m2 = _insert_memory(storage, "second no embedding", heat=0.3)
        missing = 999_999
        ids = [m1, m2, missing, m1]  # includes a duplicate + a missing id

        batched = storage.get_memories_by_ids(ids)
        by_id = {m["id"]: m for m in batched}

        # missing id must be absent (get_memory would return None → skipped)
        assert missing not in by_id
        assert set(by_id) == {m1, m2}

        # Each returned row must equal the per-id get_memory row exactly.
        for mid in (m1, m2):
            ref = storage.get_memory(mid)
            got = by_id[mid]
            # embedding bytes must be byte-identical — MMR does np.frombuffer on it.
            assert got.get("embedding") == ref.get("embedding"), f"embedding mismatch for {mid}"
            # the five nullable setdefault fields must be reproduced
            for field in (
                "embedding_model",
                "file_hash",
                "last_excitability_update",
                "original_content",
                "last_reconsolidated",
            ):
                assert field in got, f"{field} missing from batched row {mid}"
            assert got["id"] == ref["id"] and isinstance(got["id"], int)
            assert got["content"] == ref["content"]
            assert got["heat"] == pytest.approx(ref["heat"])

        # present-with-embedding row must carry byte-identical embedding
        assert by_id[m1]["embedding"] == emb

    def test_empty_input_returns_empty(self, storage):
        assert storage.get_memories_by_ids([]) == []


# ---------------------------------------------------------------------------
# 2. ONE QUERY not N — spy the storage client
# ---------------------------------------------------------------------------


class TestSingleQuery:
    def test_bounded_queries_not_n(self):
        """Car 2 (backend 5.19.0): the batch is no longer literally ONE query — it
        is a light `SELECT * OMIT content, embedding` for all ids PLUS a heavy
        content/embedding fetch for the cache MISSES only. With a NullCache (every
        id misses ⇒ the pre-Car-2 shape) that is exactly 2 queries — still a bounded
        constant, never the N point-reads the v5.97 batch replaced."""
        from yadgar._shared.storage.memory import _MemoryMixin
        from yadgar.backend.cache import NullCache

        mixin = object.__new__(_MemoryMixin)
        mixin._q = MagicMock(return_value=[])
        mixin._rows_to_dicts = lambda rows: []
        mixin._extract_id = lambda x: x
        mixin._memory_doc_cache = NullCache()

        _MemoryMixin.get_memories_by_ids(mixin, [1, 2, 3, 4, 5])

        # fresh OMIT (returns []) ⇒ no ids to heavy-fetch ⇒ 1 query here; the point
        # is it is a small constant, never 5.
        assert mixin._q.call_count <= 2, (
            f"expected a bounded (≤2) query count for 5 ids, got {mixin._q.call_count}"
        )

    def test_full_cache_hit_issues_one_query(self):
        """On a full cache hit the heavy fetch is elided ⇒ exactly ONE (light) query."""
        from yadgar._shared.storage.memory import _MemoryMixin

        class _HitCache:
            def get(self, key):
                return {"content": "c", "embedding": b""}

            def put(self, key, value):
                pass

        mixin = object.__new__(_MemoryMixin)
        # fresh OMIT returns rows for both ids so they survive the merge
        mixin._q = MagicMock(return_value=[{"id": 7}, {"id": 8}])
        mixin._rows_to_dicts = lambda rows: rows
        mixin._extract_id = lambda x: x
        mixin._memory_doc_cache = _HitCache()

        _MemoryMixin.get_memories_by_ids(mixin, [7, 8])
        assert mixin._q.call_count == 1

    def test_ids_int_sanitised_and_inline_in_list(self):
        from yadgar._shared.storage.memory import _MemoryMixin
        from yadgar.backend.cache import NullCache

        mixin = object.__new__(_MemoryMixin)
        captured: list = []

        def _spy(sql, params=None):
            captured.append(sql)
            return []

        mixin._q = _spy
        mixin._rows_to_dicts = lambda rows: []
        mixin._extract_id = lambda x: x
        mixin._memory_doc_cache = NullCache()
        _MemoryMixin.get_memories_by_ids(mixin, [7, 8])

        joined = " ".join(captured)
        assert "memory:7" in joined and "memory:8" in joined
        assert "IN [" in joined
        # Must NOT use the parameterised `IN $ids` form (fails embedded SurrealKV).
        assert "$ids" not in joined


# ---------------------------------------------------------------------------
# 3. ONE FETCH in _build_initial_results — batch not N point-reads
# ---------------------------------------------------------------------------


class _FusionHost(_FusionMixin):
    """Minimal host exposing just what _build_initial_results touches:
    self._storage (batch fetch) and self._settings (rerank_pool knobs).
    CE is disabled so _inject_ce_diversity is never reached.
    """

    def __init__(self, storage):
        self._storage = storage
        self._settings = SimpleNamespace(RERANKER_TOP_K=10, CROSS_ENCODER_TOP_K=0)


class TestBuildInitialResultsBatched:
    def test_build_initial_results_uses_one_batch_fetch(self, storage):
        m_ids = [
            _insert_memory(storage, f"mem {i}", heat=0.5, embedding=_emb_bytes(i)) for i in range(6)
        ]

        host = _FusionHost(storage)
        # Spy: batch fetch is called once; per-id get_memory is NOT used for the loop.
        batch_spy = MagicMock(side_effect=storage.get_memories_by_ids)
        peritem_spy = MagicMock(side_effect=storage.get_memory)
        host._storage = SimpleNamespace(get_memories_by_ids=batch_spy, get_memory=peritem_spy)

        fused = [(mid, 0.9 - i * 0.05) for i, mid in enumerate(m_ids)]
        fused_scores = {mid: score for mid, score in fused}
        profile = {"cross_encoder": False, "nli": False}

        result, _seen, _use_ce = host._build_initial_results(
            fused, fused_scores, {}, profile, False, max_results=5, min_heat=0.0
        )

        assert batch_spy.call_count == 1, f"expected 1 batch fetch, got {batch_spy.call_count}"
        assert peritem_spy.call_count == 0, (
            f"expected 0 per-id get_memory calls, got {peritem_spy.call_count}"
        )
        assert {m["id"] for m in result} <= set(m_ids)
        # fused order preserved (descending score)
        ordered = [m["id"] for m in result]
        assert ordered == sorted(ordered, key=lambda mid: -fused_scores[mid])

    def test_min_heat_filter_preserved(self, storage):
        m_hot = _insert_memory(storage, "hot", heat=0.9, embedding=_emb_bytes(1))
        m_cold = _insert_memory(storage, "cold", heat=0.1, embedding=_emb_bytes(2))

        host = _FusionHost(storage)
        fused = [(m_hot, 0.9), (m_cold, 0.8)]
        fused_scores = {m_hot: 0.9, m_cold: 0.8}
        profile = {"cross_encoder": False, "nli": False}

        result, _seen, _ce = host._build_initial_results(
            fused, fused_scores, {}, profile, False, max_results=5, min_heat=0.5
        )
        ids = {m["id"] for m in result}
        assert m_hot in ids and m_cold not in ids, "min_heat filter must drop cold row"
