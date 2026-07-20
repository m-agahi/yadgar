"""T2 Car E3 — viz graph data-assembly + layout compute run backend-side.

Census verdict #11 (user, 2026-07-09): the viz HTTP server + static stay core;
the DB-heavy graph data assembly (GraphAPI) and the graph-layout compute move
behind the backend. Core /api/graph* endpoints become forwarders to the
backend POST /viz route (run_viz_op dispatch, mirroring /admin + run_admin_op).

TDD: RED before backend/viz_exec existed, GREEN with it.
"""

from __future__ import annotations

import pytest


@pytest.fixture(scope="module")
def storage(module_storage):
    return module_storage


@pytest.fixture(autouse=True)
def _wire_backend_storage(storage, monkeypatch):
    import yadgar._shared.runtime.state as _st

    monkeypatch.setattr(_st, "_storage", storage)


# ---------------------------------------------------------------------------
# run_viz_op dispatch
# ---------------------------------------------------------------------------


class TestRunVizOp:
    def test_ops_registered(self):
        from yadgar.backend.viz_exec import viz_ops

        ops = viz_ops()
        assert {"graph", "graph_stats", "graph_edges", "graph_neighborhood"} <= ops

    def test_unknown_op_raises_keyerror(self):
        from yadgar.backend.viz_exec import run_viz_op

        with pytest.raises(KeyError):
            run_viz_op("nope", {})

    def test_graph_returns_nodes_and_edges(self, storage):
        from yadgar.backend.viz_exec import run_viz_op

        storage.insert_memory(
            {
                "content": "viz node memory",
                "directory_context": "/tmp/vizproj",
                "tags": ["test"],
                "heat": 0.7,
            }
        )
        result = run_viz_op(
            "graph",
            {"max_memories": 50, "top_k": 8, "max_wiki": 10, "max_entities": 10},
        )
        assert isinstance(result.get("nodes"), list)
        assert isinstance(result.get("edges"), list)
        assert any(n.get("type") == "memory" for n in result["nodes"])

    def test_graph_stats_shape(self, storage):
        from yadgar.backend.viz_exec import run_viz_op

        result = run_viz_op("graph_stats", {})
        assert "memory_count" in result or "memories" in result or result  # non-empty dict

    def test_graph_neighborhood_shape(self, storage):
        from yadgar.backend.viz_exec import run_viz_op

        mem_id = storage.insert_memory(
            {
                "content": "hood center",
                "directory_context": "/tmp/vizproj",
                "tags": ["test"],
                "heat": 0.5,
            }
        )
        result = run_viz_op("graph_neighborhood", {"node_id": f"mem:{mem_id}", "hops": 1})
        assert isinstance(result.get("nodes"), list)
        assert isinstance(result.get("edges"), list)

    def test_graph_attaches_cached_layout_positions(self, storage):
        """A layout cache exists → nodes carry x/y/z (attach is unconditional now)."""
        from datetime import UTC, datetime

        from yadgar.backend.viz_exec import run_viz_op

        mem_id = storage.insert_memory(
            {
                "content": "positioned memory",
                "directory_context": "/tmp/vizproj",
                "tags": ["test"],
                "heat": 0.9,
            }
        )
        storage.set_graph_layout_cache(
            "sig-test",
            {f"mem:{mem_id}": [1.0, 2.0, 3.0]},
            datetime.now(UTC).isoformat(),
        )
        result = run_viz_op(
            "graph",
            {"max_memories": 50, "top_k": 8, "max_wiki": 0, "max_entities": 0},
        )

        target = [n for n in result["nodes"] if n.get("id") == f"mem:{mem_id}"]
        assert target and target[0].get("x") == 1.0 and target[0].get("z") == 3.0


# ---------------------------------------------------------------------------
# Core sides forward (no direct assembly left in core)
# ---------------------------------------------------------------------------


class TestCoreForwards:
    def test_http_has_no_graphapi_reference(self):
        import inspect

        import yadgar.core.server.http as http_mod

        src = inspect.getsource(http_mod)
        assert "GraphAPI" not in src, (
            "/api/graph* handlers must forward to the backend /viz route (Car E3)"
        )

    def test_core_orchestrator_has_no_layout_precompute(self):
        import inspect

        import yadgar.core.consolidation.orchestrator as orch_mod

        src = inspect.getsource(orch_mod)
        assert "GraphAPI(" not in src and "compute_graph_layout(" not in src, (
            "graph-layout precompute runs in the backend consolidation cycle (Car E3)"
        )

    def test_backend_cycle_runs_layout_precompute_on_full(self, monkeypatch):
        """run_consolidation_cycle(full) must invoke the layout precompute."""
        import yadgar.backend.consolidation.service as svc

        calls = []
        monkeypatch.setattr(
            svc,
            "_maybe_precompute_graph_layout",
            lambda storage, settings: calls.append("precompute"),
        )

        class _FakeScheduler:
            def run_full_consolidation(self):
                return {"mode": "full"}

            def run_nightly_consolidation(self):
                return {"mode": "nightly"}

            def force_consolidate(self):
                return {"mode": "light"}

        monkeypatch.setattr(svc, "_get_scheduler", lambda: _FakeScheduler())

        svc.run_consolidation_cycle("light")
        assert calls == []
        svc.run_consolidation_cycle("full")
        assert calls == ["precompute"]
        svc.run_consolidation_cycle("nightly")
        assert calls == ["precompute", "precompute"]


# ---------------------------------------------------------------------------
# Car C (ADR-0152): slider server-recompute op (Option A)
# ---------------------------------------------------------------------------


class TestGraphRelayoutOp:
    """The graph_relayout op recomputes galaxy positions with per-request slider
    params and RETURNS them (positions + membership). It MUST NOT write the
    canonical singleton cache (that row is nightly/shared)."""

    def _seed(self, storage, n=6):
        for i in range(n):
            storage.insert_memory(
                {
                    "content": f"relayout node {i}",
                    "directory_context": "/tmp/relayoutproj",
                    "tags": ["test"],
                    "heat": float(i + 1) / n,
                }
            )

    def test_op_registered(self):
        from yadgar.backend.viz_exec import viz_ops

        assert "graph_relayout" in viz_ops()

    def test_returns_positions_and_membership(self, storage):
        from yadgar.backend.viz_exec import run_viz_op

        self._seed(storage)
        out = run_viz_op("graph_relayout", {"arms": 4, "spiral_pitch": 0.3, "core_density": 1.0})
        assert "positions" in out and out["positions"]
        assert "membership" in out
        for coord in out["positions"].values():
            assert len(coord) == 3
        for info in out["membership"].values():
            assert "loose" in info and "arm" in info

    def test_param_override_changes_output(self, storage):
        """A per-request param override actually changes the returned positions.

        The seeded corpus has no real multi-member clusters (all memories loose →
        core), so arms don't move anything, but core_density repacks the bulge —
        a robust discriminator that the request param reaches galaxy_layout."""
        from yadgar.backend.viz_exec import run_viz_op

        self._seed(storage)
        a = run_viz_op("graph_relayout", {"arms": 4, "spiral_pitch": 0.3, "core_density": 0.5})
        b = run_viz_op("graph_relayout", {"arms": 4, "spiral_pitch": 0.3, "core_density": 3.0})
        assert a["positions"] != b["positions"]
        # The echoed params reflect the overrides (proves they were threaded).
        assert a["core_density"] == 0.5 and b["core_density"] == 3.0

    def test_does_not_write_canonical_cache(self, storage):
        """R3: the slider recompute must NOT overwrite graph_layout_cache:current
        — one user's slider fiddle must not leak to everyone / no-op the nightly."""
        from yadgar.backend.viz_exec import run_viz_op

        self._seed(storage)
        before = storage.get_graph_layout_cache()
        run_viz_op("graph_relayout", {"arms": 6, "spiral_pitch": 0.5, "core_density": 1.5})
        after = storage.get_graph_layout_cache()
        assert before == after, "graph_relayout must not mutate the canonical cache"
