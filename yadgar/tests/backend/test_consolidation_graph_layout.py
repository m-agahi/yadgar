"""viz-render-perf (Car A): precomputed graph layout hook on the consolidation cycle.

Precompute now runs UNCONDITIONALLY on the full/nightly consolidation path —
the VIZ_PRECOMPUTED_LAYOUT_ENABLED knob was removed (the plan supersedes ADR-0010's
default-OFF stance). Only a graph-signature no-op (skip when the graph shape is
unchanged) and the full/nightly-only wiring (never the light consolidate_now budget)
remain as gates.
"""

import pytest

from yadgar._shared.config import Settings
from yadgar._shared.embeddings import EmbeddingEngine
from yadgar.backend.consolidation import ConsolidationScheduler

# T2 Car E3: the layout precompute moved into the backend consolidation cycle.
from yadgar.backend.consolidation.service import _maybe_precompute_graph_layout

#: C13 — every write in this file names a project explicitly.
#: ADR-0227 deleted the derivation that used to answer for it, so a
#: dict without this key is a hard UnresolvedProjectError at insert.
_TEST_PROJECT = "m-agahi/yadgar"


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


def _settings(tmp_path, galaxy=True):
    return Settings(
        DB_PATH=str(tmp_path / "s.db"),
        VIZ_LAYOUT_ITERATIONS=10,
        VIZ_GALAXY_LAYOUT=galaxy,
    )


def _seed_graph(storage):
    """A couple of memories so GraphAPI returns a non-empty node set."""
    storage.insert_memory(
        {
            "project_id": _TEST_PROJECT,
            "content": "alpha node",
            "directory_context": "/p",
            "tags": ["t"],
            "heat": 1.0,
        }
    )
    storage.insert_memory(
        {
            "project_id": _TEST_PROJECT,
            "content": "beta node",
            "directory_context": "/p",
            "tags": ["t"],
            "heat": 0.9,
        }
    )


def test_precompute_computes_and_caches_unconditionally(tmp_path, storage):
    """No flag involved: precompute caches positions + signature over a live graph."""
    _seed_graph(storage)
    _maybe_precompute_graph_layout(storage, _settings(tmp_path))
    cached = storage.get_graph_layout_cache()
    assert cached is not None
    assert cached["signature"]
    assert cached["computed_at"]
    # every cached node has a 3-coord position
    assert len(cached["positions"]) >= 2
    for coord in cached["positions"].values():
        assert len(coord) == 3


def test_empty_graph_caches_no_positions(tmp_path, storage):
    """Empty graph → cache carries no positions → attach is a no-op (fallback contract)."""
    _maybe_precompute_graph_layout(storage, _settings(tmp_path))
    cached = storage.get_graph_layout_cache()
    # An empty graph yields a signature but zero positions; attach_cached_positions
    # short-circuits on empty positions, so nodes stay bare and the client places them.
    assert not (cached or {}).get("positions")


def test_signature_unchanged_is_noop(tmp_path, storage):
    """Second precompute with an unchanged graph does not recompute."""
    _seed_graph(storage)
    settings = _settings(tmp_path)
    _maybe_precompute_graph_layout(storage, settings)
    first = storage.get_graph_layout_cache()
    # Run again — graph shape unchanged → computed_at must be preserved.
    _maybe_precompute_graph_layout(storage, settings)
    second = storage.get_graph_layout_cache()
    assert second["signature"] == first["signature"]
    assert second["computed_at"] == first["computed_at"]


def test_light_consolidate_never_computes_layout(tmp_path, storage, embeddings):
    """force_consolidate (light path) must NOT trigger layout precompute."""
    _seed_graph(storage)
    sched = ConsolidationScheduler(storage, embeddings, _settings(tmp_path))
    sched.force_consolidate()
    assert storage.get_graph_layout_cache() is None


def test_signature_change_recomputes(tmp_path, storage):
    """Adding a node changes the signature → layout recomputes."""
    _seed_graph(storage)
    settings = _settings(tmp_path)
    _maybe_precompute_graph_layout(storage, settings)
    first = storage.get_graph_layout_cache()
    # Mutate the graph shape.
    storage.insert_memory(
        {
            "project_id": _TEST_PROJECT,
            "content": "gamma node",
            "directory_context": "/p",
            "tags": ["t"],
            "heat": 0.8,
        }
    )
    _maybe_precompute_graph_layout(storage, settings)
    second = storage.get_graph_layout_cache()
    assert second["signature"] != first["signature"]


# ── finish-viz: galaxy layout mode selection ─────────────────────────────────


def test_galaxy_is_default_mode(tmp_path, storage):
    """VIZ_GALAXY_LAYOUT defaults on → cache records layout_mode="galaxy"."""
    _seed_graph(storage)
    _maybe_precompute_graph_layout(storage, _settings(tmp_path, galaxy=True))
    cached = storage.get_graph_layout_cache()
    assert cached is not None
    assert cached["layout_mode"] == "galaxy"
    assert len(cached["positions"]) >= 2


def test_spring_mode_when_galaxy_off(tmp_path, storage):
    """VIZ_GALAXY_LAYOUT=False → spring_layout path → layout_mode="spring"."""
    _seed_graph(storage)
    _maybe_precompute_graph_layout(storage, _settings(tmp_path, galaxy=False))
    cached = storage.get_graph_layout_cache()
    assert cached is not None
    assert cached["layout_mode"] == "spring"


def test_mode_flip_recomputes(tmp_path, storage):
    """Flipping the galaxy knob recomputes even when the graph shape is unchanged."""
    _seed_graph(storage)
    _maybe_precompute_graph_layout(storage, _settings(tmp_path, galaxy=True))
    first = storage.get_graph_layout_cache()
    assert first["layout_mode"] == "galaxy"
    # Same graph, flipped mode → must recompute to spring (not a signature no-op).
    _maybe_precompute_graph_layout(storage, _settings(tmp_path, galaxy=False))
    second = storage.get_graph_layout_cache()
    assert second["layout_mode"] == "spring"


# ── Car A (ADR-0152): membership cached + signature folds version+params ─────


def test_precompute_caches_membership(tmp_path, storage):
    """Galaxy precompute stores a membership sibling ({id:{loose,arm}}) so the
    serve path can stamp loose/arm without recomputing."""
    _seed_graph(storage)
    _maybe_precompute_graph_layout(storage, _settings(tmp_path, galaxy=True))
    cached = storage.get_graph_layout_cache()
    assert cached is not None
    membership = cached.get("membership")
    assert membership, "galaxy cache must carry a membership map"
    for info in membership.values():
        assert "loose" in info and "arm" in info


def test_galaxy_param_change_recomputes(tmp_path, storage):
    """R6: changing a VIZ_GALAXY_* param recomputes even on an unchanged graph
    shape (params fold into the signature)."""
    _seed_graph(storage)
    s1 = Settings(DB_PATH=str(tmp_path / "s.db"), VIZ_GALAXY_LAYOUT=True, VIZ_GALAXY_ARMS=4)
    _maybe_precompute_graph_layout(storage, s1)
    first = storage.get_graph_layout_cache()
    s2 = Settings(DB_PATH=str(tmp_path / "s.db"), VIZ_GALAXY_LAYOUT=True, VIZ_GALAXY_ARMS=6)
    _maybe_precompute_graph_layout(storage, s2)
    second = storage.get_graph_layout_cache()
    assert second["signature"] != first["signature"]
