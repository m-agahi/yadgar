"""Tests for WriteGate entity-set cache (§14a).

TDD: these tests are written BEFORE the cache is implemented and must fail
until predictive_coding.py adds:
  - A TTL-based entity-set cache
  - Directory-context pre-filter on memories
  - Cache invalidation wiring

Cache contract:
  - Within TTL: get_all_entities called only once per cache lifetime
  - After TTL: cache is refreshed (get_all_entities called again)
  - Memory iteration for _compute_temporal_novelty uses only
    memories whose directory_context == the queried directory
  - Invalidate method exists and clears the cache
"""

import time
from unittest.mock import MagicMock

import pytest

from yadgar._shared.config import Settings
from yadgar._shared.predictive_coding import WriteGate

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings():
    return Settings(
        WRITE_GATE_THRESHOLD=0.4,
        PREDICTIVE_CODING_ENTITY_TTL_SECONDS=300,
    )


@pytest.fixture
def mock_storage():
    s = MagicMock()
    s.get_all_entities.return_value = [{"id": 1, "name": "MyClass", "heat": 1.0}]
    s.get_memories_for_directory.return_value = []
    s.get_all_memories_for_decay.return_value = []
    s.search_vectors.return_value = []
    s.get_entity_by_name.return_value = None
    s.get_relationships_among_entities.return_value = []
    return s


@pytest.fixture
def mock_embeddings():
    e = MagicMock()
    e.encode.return_value = None
    return e


@pytest.fixture
def mock_retriever(mock_storage, settings):
    r = MagicMock()
    r._graph = MagicMock()
    r._graph.extract_entities_typed.return_value = []
    return r


@pytest.fixture
def gate(mock_storage, mock_embeddings, mock_retriever, settings):
    return WriteGate(mock_storage, mock_embeddings, mock_retriever, settings)


# ---------------------------------------------------------------------------
# §14a-1: Cache hit within TTL — no second get_all_entities call
# ---------------------------------------------------------------------------


class TestEntityCacheHitWithinTTL:
    def test_second_call_does_not_refetch(self, gate, mock_storage):
        """Within TTL, get_all_entities must not be called twice."""
        # Call compute_surprisal twice for temporal_novelty (which calls get_all_entities)
        gate._compute_temporal_novelty("content about MyClass", "/proj")
        gate._compute_temporal_novelty("more content about MyClass", "/proj")

        # get_all_entities should be called at most once (cache hit on 2nd call)
        assert mock_storage.get_all_entities.call_count <= 1, (
            f"Expected cache hit: get_all_entities called "
            f"{mock_storage.get_all_entities.call_count} times (expected ≤ 1)"
        )

    def test_structural_novelty_shares_cache(self, gate, mock_storage):
        """_compute_structural_novelty should share the entity cache."""
        gate._compute_temporal_novelty("content about MyClass", "/proj")
        mock_storage.get_all_entities.reset_mock()

        gate._compute_structural_novelty("content about MyClass", "/proj")

        # Cache was populated; should not refetch
        assert mock_storage.get_all_entities.call_count == 0, (
            "Expected entity cache reuse across temporal and structural novelty calls"
        )


# ---------------------------------------------------------------------------
# §14a-2: Cache miss after TTL expiry — refetch
# ---------------------------------------------------------------------------


class TestEntityCacheMissAfterTTL:
    def test_refetch_after_ttl(self, mock_storage, mock_embeddings, mock_retriever):
        """After TTL expires the cache must be refreshed."""
        settings = Settings(
            WRITE_GATE_THRESHOLD=0.4,
            PREDICTIVE_CODING_ENTITY_TTL_SECONDS=1,  # 1-second TTL for fast test
        )
        g = WriteGate(mock_storage, mock_embeddings, mock_retriever, settings)

        g._compute_temporal_novelty("content about MyClass", "/proj")
        first_count = mock_storage.get_all_entities.call_count

        # Wait for TTL to expire
        time.sleep(1.1)

        g._compute_temporal_novelty("content about MyClass", "/proj")
        second_count = mock_storage.get_all_entities.call_count

        assert second_count > first_count, (
            f"Expected refetch after TTL: counts before={first_count} after={second_count}"
        )


# ---------------------------------------------------------------------------
# §14a-3: Memory iteration uses directory filter, not all_memories_for_decay
# ---------------------------------------------------------------------------


class TestDirectoryContextFilter:
    def test_temporal_novelty_uses_directory_filter(self, gate, mock_storage):
        """_compute_temporal_novelty must call get_memories_for_directory, not
        get_all_memories_for_decay, so that unrelated directories are excluded."""
        mock_storage.get_memories_for_directory.return_value = [
            {
                "id": 10,
                "content": "MyClass implementation detail",
                "directory_context": "/proj",
                "heat": 1.0,
                "created_at": "2024-01-01T10:00:00+00:00",
            }
        ]
        # Entities must include "MyClass" to trigger content match
        mock_storage.get_all_entities.return_value = [{"id": 1, "name": "MyClass", "heat": 1.0}]

        gate._compute_temporal_novelty("content about MyClass", "/proj")

        # Must have filtered memories by directory
        mock_storage.get_memories_for_directory.assert_called()
        called_dir = mock_storage.get_memories_for_directory.call_args[0][0]
        assert called_dir == "/proj", f"Expected directory '/proj', got {called_dir!r}"

    def test_get_all_memories_for_decay_not_called(self, gate, mock_storage):
        """get_all_memories_for_decay is the O(N·M) hotspot — must not be called
        from _compute_temporal_novelty after the fix."""
        gate._compute_temporal_novelty("any content", "/proj")
        assert mock_storage.get_all_memories_for_decay.call_count == 0, (
            "get_all_memories_for_decay should not be called from _compute_temporal_novelty"
        )


# ---------------------------------------------------------------------------
# §14a-4: Cache invalidation on entity add/delete
# ---------------------------------------------------------------------------


class TestCacheInvalidation:
    def test_invalidate_clears_cache(self, gate, mock_storage):
        """After invalidate_entity_cache(), the next call must refetch."""
        gate._compute_temporal_novelty("content about MyClass", "/proj")
        assert mock_storage.get_all_entities.call_count >= 1
        mock_storage.get_all_entities.reset_mock()

        # Invalidate cache
        gate.invalidate_entity_cache()

        # Next call must refetch
        gate._compute_temporal_novelty("content about MyClass again", "/proj")
        assert mock_storage.get_all_entities.call_count >= 1, (
            "Expected refetch after invalidate_entity_cache()"
        )

    def test_invalidate_method_exists(self, gate):
        """WriteGate must expose invalidate_entity_cache() for external callers."""
        assert callable(getattr(gate, "invalidate_entity_cache", None)), (
            "WriteGate must have an invalidate_entity_cache() method"
        )
