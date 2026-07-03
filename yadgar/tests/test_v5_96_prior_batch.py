"""v5.96.0 — batched prior fetch (N+1 → single query) parity + call-count tests.

get_memory_graph_priors / get_memory_cofire_priors previously issued one point-read
per candidate id (N+1). v5.96.0 collapses each to a single `WHERE id IN [...]` query
over precomputed scalar fields.  These tests guard:

1. PARITY (real store): the batched result equals the old per-id semantics for a mix
   of ids that includes present, absent/NULL, missing, and duplicate ids.  Run against
   a live StorageEngine (server mode when the `surreal` binary is on PATH, else embedded)
   so it actually exercises the SurrealQL — a mocked _q would prove nothing about the
   IN [...] construct's cross-mode validity (the whole point of the caveat).
2. ONE QUERY: the method issues exactly ONE _q call for N ids, not N (spy the client).

The strict cross-mode case is embedded (inline record-id IN lists are the reason the
codebase uses this idiom); server mode is strictly more permissive for this construct.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from yadgar import server


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("v5_96_prior_batch")
    server.init_engines(
        db_path=str(tmp_path / "test_prior_batch.db"), embedding_model="all-MiniLM-L6-v2"
    )
    yield
    server.shutdown()


@pytest.fixture()
def storage(_engines):
    from yadgar.server.lifecycle import _get_storage

    return _get_storage()


_DIR = "/tmp/test_prior_batch_proj"


def _insert_memory(storage, mid_content: str, directory: str = _DIR) -> int:
    """Insert a bare memory row via storage._q and return its id."""
    mid = storage._next_id("memory")
    storage._q(
        "CREATE type::record('memory', $id) SET "
        "content = $content, directory_context = $dir, heat = $heat",
        {"id": mid, "content": mid_content, "dir": directory, "heat": 0.5},
    )
    return mid


# ---------------------------------------------------------------------------
# Reference implementation: the OLD per-id semantics, computed independently.
# ---------------------------------------------------------------------------


def _old_graph_priors(storage, memory_ids: list[int]) -> dict[int, float]:
    """Old N+1 semantics reproduced independently for parity comparison."""
    result: dict[int, float] = {}
    for mid in memory_ids:
        rows = storage._q(
            "SELECT graph_prior FROM type::record('memory', $id) WHERE graph_prior IS NOT NONE",
            {"id": mid},
        )
        for row in rows:
            gp = row.get("graph_prior")
            if gp is not None:
                result[mid] = float(gp)
    return result


def _old_cofire_priors(storage, memory_ids: list[int]) -> dict[int, float]:
    result: dict[int, float] = {}
    for mid in memory_ids:
        rows = storage._q(
            "SELECT cofire_prior FROM type::record('memory', $id) WHERE cofire_prior IS NOT NONE",
            {"id": mid},
        )
        for row in rows:
            cp = row.get("cofire_prior")
            if cp is not None:
                result[mid] = float(cp)
    return result


# ---------------------------------------------------------------------------
# 1. PARITY — batched == old per-id, over present/absent/missing/duplicate ids
# ---------------------------------------------------------------------------


class TestGraphPriorParity:
    def test_batched_matches_old_per_id(self, storage):
        m_present = _insert_memory(storage, "has a prior")
        m_absent = _insert_memory(storage, "no prior set")  # NULL graph_prior
        storage.update_memory_graph_prior(m_present, 0.73)

        missing_id = 999_999  # never created
        ids = [m_present, m_absent, missing_id, m_present]  # includes a duplicate

        batched = storage.get_memory_graph_priors(ids)
        reference = _old_graph_priors(storage, ids)

        assert batched == reference, f"batched={batched} != old={reference}"
        assert batched == {m_present: pytest.approx(0.73)}, (
            f"only the present id should appear; got {batched}"
        )

    def test_empty_input_returns_empty(self, storage):
        assert storage.get_memory_graph_priors([]) == {}


class TestCofirePriorParity:
    def test_batched_matches_old_per_id(self, storage):
        m_present = _insert_memory(storage, "cofire present")
        m_absent = _insert_memory(storage, "cofire absent")
        storage.update_memory_cofire_prior(m_present, 0.42)

        ids = [m_present, m_absent, 888_888, m_absent]

        batched = storage.get_memory_cofire_priors(ids)
        reference = _old_cofire_priors(storage, ids)

        assert batched == reference, f"batched={batched} != old={reference}"
        assert batched == {m_present: pytest.approx(0.42)}


# ---------------------------------------------------------------------------
# 2. ONE QUERY not N — spy the storage client
# ---------------------------------------------------------------------------


class TestSingleQuery:
    def test_graph_priors_issues_one_query_for_n_ids(self):
        from yadgar.storage.memory import _MemoryMixin

        mixin = object.__new__(_MemoryMixin)
        mixin._q = MagicMock(return_value=[{"id": 1, "graph_prior": 0.5}])

        _MemoryMixin.get_memory_graph_priors(mixin, [1, 2, 3, 4, 5])

        assert mixin._q.call_count == 1, (
            f"expected exactly 1 batched query for 5 ids, got {mixin._q.call_count}"
        )

    def test_cofire_priors_issues_one_query_for_n_ids(self):
        from yadgar.storage.memory import _MemoryMixin

        mixin = object.__new__(_MemoryMixin)
        mixin._q = MagicMock(return_value=[{"id": 2, "cofire_prior": 0.9}])

        _MemoryMixin.get_memory_cofire_priors(mixin, [1, 2, 3, 4, 5])

        assert mixin._q.call_count == 1, (
            f"expected exactly 1 batched query for 5 ids, got {mixin._q.call_count}"
        )

    def test_id_is_int_sanitised_in_query(self):
        """The inlined IN-list must use int(id) — closes injection, matches get_memory."""
        from yadgar.storage.memory import _MemoryMixin

        mixin = object.__new__(_MemoryMixin)
        captured: dict = {}

        def _spy(sql, params=None):
            captured["sql"] = sql
            return []

        mixin._q = _spy
        _MemoryMixin.get_memory_graph_priors(mixin, [7, 8])

        assert "memory:7" in captured["sql"] and "memory:8" in captured["sql"]
        assert "IN [" in captured["sql"]
