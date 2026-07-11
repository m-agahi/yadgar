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

    def test_graph_attaches_cached_layout_positions(self, storage, monkeypatch):
        """When the layout flag is on and a cache exists, nodes carry x/y/z."""
        from datetime import UTC, datetime

        from yadgar.backend.viz_exec import run_viz_op

        monkeypatch.setenv("YADGAR_VIZ_PRECOMPUTED_LAYOUT_ENABLED", "1")
        from yadgar._shared.config import get_settings

        get_settings.cache_clear()

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
        try:
            result = run_viz_op(
                "graph",
                {"max_memories": 50, "top_k": 8, "max_wiki": 0, "max_entities": 0},
            )
        finally:
            get_settings.cache_clear()

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
