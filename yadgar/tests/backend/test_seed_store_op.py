"""T2 Car E1 — seed_project's store phase forwards to the backend seed_store op.

Core keeps the host-FS half (scan_project + generate_memories + the
_project_init draft); the store phase — embedding, thermodynamic scoring,
insert_memory/update_memory_scores, old-seed deletion, _project_init upsert —
runs backend-side (census verdict #9, ADR-0078).

TDD: RED before the op existed, GREEN with it.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from yadgar._shared.config import Settings
from yadgar._shared.thermodynamics import MemoryThermodynamics


@pytest.fixture(scope="module")
def storage(module_storage):
    return module_storage


@pytest.fixture(autouse=True)
def _wire_backend_engines(storage, embeddings, tmp_path, monkeypatch):
    """seed_store resolves engines via _st — wire the test engines in."""
    import yadgar._shared.runtime.state as _st

    monkeypatch.setattr(_st, "_storage", storage)
    monkeypatch.setattr(_st, "_embeddings", embeddings)
    settings = Settings(DB_PATH=str(tmp_path / "test.db"))
    monkeypatch.setattr(_st, "_thermo", MemoryThermodynamics(storage, embeddings, settings))


def _seed_rows(storage, root):
    return storage._q(
        "SELECT id FROM memory WHERE directory_context = $dir AND '_seed' IN tags",
        {"dir": root},
    )


class TestSeedStoreOp:
    def test_registered(self):
        from yadgar.backend.admin_exec import admin_ops

        assert "seed_store" in admin_ops()

    def test_stores_memories_with_scores_and_init(self, storage):
        from yadgar.backend.admin_exec.seed import seed_store

        root = "/tmp/seedproj"
        result = seed_store(
            {
                "root": root,
                "memories": [
                    {
                        "content": "Project overview: a test project",
                        "context": root,
                        "tags": ["_seed", "overview"],
                        "base_heat": 0.9,
                    },
                    {
                        "content": "Component: api handlers",
                        "context": root,
                        "tags": ["_seed", "component"],
                        "base_heat": 0.5,
                    },
                ],
                "init_content": "# seedproj\ninit content",
            }
        )

        assert result["created"] == 2
        rows = _seed_rows(storage, root)
        assert len(rows) == 2
        mem_id = storage._extract_id(rows[0].get("id"))
        memory = storage.get_memory(mem_id)
        assert memory["heat"] > 0.0
        assert memory.get("embedding") is not None
        # _project_init upserted
        init_rows = storage._q(
            "SELECT id FROM memory WHERE directory_context = $dir AND '_project_init' IN tags",
            {"dir": root},
        )
        assert len(init_rows) == 1

    def test_reseed_replaces_old_seed_memories(self, storage):
        from yadgar.backend.admin_exec.seed import seed_store

        root = "/tmp/reseedproj"
        first = {
            "root": root,
            "memories": [
                {"content": "old overview", "context": root, "tags": ["_seed"], "base_heat": 0.9}
            ],
            "init_content": "",
        }
        seed_store(first)
        assert len(_seed_rows(storage, root)) == 1

        second = {
            "root": root,
            "memories": [
                {"content": "new overview", "context": root, "tags": ["_seed"], "base_heat": 0.9},
                {"content": "new component", "context": root, "tags": ["_seed"], "base_heat": 0.5},
            ],
            "init_content": "",
        }
        result = seed_store(second)

        assert result["created"] == 2
        assert result["replaced"] == 1
        assert len(_seed_rows(storage, root)) == 2


class TestCoreSeedForwards:
    def test_seed_project_forwards_store_phase(self, tmp_path):
        """Core seed_project scans + generates, then forwards ONE seed_store op."""
        from yadgar.core.seed import seed_project

        (tmp_path / "README.md").write_text("# demo\nA demo project.")
        (tmp_path / "main.py").write_text("print('hi')")

        with patch("yadgar.core.forward._forward_admin") as fwd:
            fwd.return_value = {"created": 3, "replaced": 0}
            result = seed_project(directory=str(tmp_path))

        fwd.assert_called_once()
        op, payload = fwd.call_args.args[0], fwd.call_args.args[1]
        assert op == "seed_store"
        assert payload["root"] == str(tmp_path)
        assert payload["memories"], "scan+generate must produce memories core-side"
        for mem in payload["memories"]:
            assert set(mem) >= {"content", "context", "tags", "base_heat"}
        assert result["stored"] is True
        assert result["created"] == 3

    def test_dry_run_does_not_forward(self, tmp_path):
        from yadgar.core.seed import seed_project

        (tmp_path / "README.md").write_text("# demo")

        with patch("yadgar.core.forward._forward_admin") as fwd:
            result = seed_project(directory=str(tmp_path), dry_run=True)

        fwd.assert_not_called()
        assert result["stored"] is False

    def test_no_direct_storage_writes_in_core_seed(self):
        import inspect

        import yadgar.core.seed._generate as gen_mod

        src = inspect.getsource(gen_mod)
        for banned in ("insert_memory", "update_memory_scores", "upsert_project_init"):
            assert banned not in src, (
                f"core seed must forward the store phase (found direct {banned!r})"
            )
