"""v5.88: precomputed graph layout hook on the consolidation cycle.

Gated by VIZ_PRECOMPUTED_LAYOUT_ENABLED (default OFF) + a graph-signature
no-op so recompute only happens when the graph shape changed, and wired into
the NIGHTLY path only so it never blocks the light consolidate_now budget.
"""

import pytest

from yadgar._shared.config import Settings
from yadgar._shared.embeddings import EmbeddingEngine
from yadgar.core.consolidation import ConsolidationScheduler


@pytest.fixture(scope="module")
def storage(module_storage):
    """Module-scoped shared StorageEngine (v5.104 P1B): schema inits ONCE per
    file (was a fresh per-test engine); per-test isolation via the registered
    data-wipe in conftest._wipe_surrealdb_data."""
    return module_storage


@pytest.fixture
def embeddings():
    engine = EmbeddingEngine()
    engine._unavailable = True
    return engine


def _settings(tmp_path, enabled):
    return Settings(
        DB_PATH=str(tmp_path / "s.db"),
        VIZ_PRECOMPUTED_LAYOUT_ENABLED=enabled,
        VIZ_LAYOUT_ITERATIONS=10,
    )


def _seed_graph(storage):
    """A couple of memories so GraphAPI returns a non-empty node set."""
    storage.insert_memory(
        {"content": "alpha node", "directory_context": "/p", "tags": ["t"], "heat": 1.0}
    )
    storage.insert_memory(
        {"content": "beta node", "directory_context": "/p", "tags": ["t"], "heat": 0.9}
    )


def test_flag_off_no_layout_cached(tmp_path, storage, embeddings):
    """Default OFF: nightly cycle computes no layout, cache stays empty."""
    _seed_graph(storage)
    sched = ConsolidationScheduler(storage, embeddings, _settings(tmp_path, False))
    sched.run_nightly_consolidation()
    assert storage.get_graph_layout_cache() is None


def test_flag_on_computes_and_caches(tmp_path, storage, embeddings):
    """Flag ON + non-empty graph: nightly cycle caches positions + signature."""
    _seed_graph(storage)
    sched = ConsolidationScheduler(storage, embeddings, _settings(tmp_path, True))
    sched.run_nightly_consolidation()
    cached = storage.get_graph_layout_cache()
    assert cached is not None
    assert cached["signature"]
    assert cached["computed_at"]
    # every cached node has a 3-coord position
    assert len(cached["positions"]) >= 2
    for coord in cached["positions"].values():
        assert len(coord) == 3


def test_signature_unchanged_is_noop(tmp_path, storage, embeddings):
    """Second nightly run with an unchanged graph does not recompute."""
    _seed_graph(storage)
    sched = ConsolidationScheduler(storage, embeddings, _settings(tmp_path, True))
    sched.run_nightly_consolidation()
    first = storage.get_graph_layout_cache()
    # Run again — graph shape unchanged → computed_at must be preserved.
    sched.run_nightly_consolidation()
    second = storage.get_graph_layout_cache()
    assert second["signature"] == first["signature"]
    assert second["computed_at"] == first["computed_at"]


def test_light_consolidate_never_computes_layout(tmp_path, storage, embeddings):
    """force_consolidate (light path) must NOT trigger layout precompute."""
    _seed_graph(storage)
    sched = ConsolidationScheduler(storage, embeddings, _settings(tmp_path, True))
    sched.force_consolidate()
    assert storage.get_graph_layout_cache() is None


def test_signature_change_recomputes(tmp_path, storage, embeddings):
    """Adding a node changes the signature → layout recomputes."""
    _seed_graph(storage)
    sched = ConsolidationScheduler(storage, embeddings, _settings(tmp_path, True))
    sched.run_nightly_consolidation()
    first = storage.get_graph_layout_cache()
    # Mutate the graph shape.
    storage.insert_memory(
        {"content": "gamma node", "directory_context": "/p", "tags": ["t"], "heat": 0.8}
    )
    sched.run_nightly_consolidation()
    second = storage.get_graph_layout_cache()
    assert second["signature"] != first["signature"]
