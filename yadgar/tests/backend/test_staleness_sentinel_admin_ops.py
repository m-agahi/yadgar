"""T2 Car E1 — sentinel-vacuum + staleness-flag writes forward to backend /admin ops.

Two of the five core raw-write paths (plan Car E1):
- ``core/server/http.py`` sentinel vacuum ``delete_memory`` loop → backend op
  ``vacuum_stale_sentinels`` (the whole read+delete compute is stateless-over-DB).
- ``core/server/tools/admin_other.py`` validate_memory fallback
  ``update_memory_staleness`` → backend op ``update_memory_staleness``.

TDD: RED before the ops existed, GREEN with them.
"""

from __future__ import annotations

from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# update_memory_staleness op
# ---------------------------------------------------------------------------


class TestUpdateMemoryStalenessOp:
    def test_registered(self):
        from yadgar.backend.admin_exec import admin_ops

        assert "update_memory_staleness" in admin_ops()

    def test_writes_staleness_flag(self, monkeypatch):
        import yadgar._shared.runtime.state as _st
        from yadgar.backend.admin_exec.memory import update_memory_staleness

        storage = MagicMock()
        monkeypatch.setattr(_st, "_storage", storage)

        result = update_memory_staleness({"memory_id": 7, "is_stale": True})

        storage.update_memory_staleness.assert_called_once_with(7, True)
        assert result == {"memory_id": 7, "is_stale": True}


# ---------------------------------------------------------------------------
# vacuum_stale_sentinels op
# ---------------------------------------------------------------------------


class TestVacuumStaleSentinelsOp:
    def test_registered(self):
        from yadgar.backend.admin_exec import admin_ops

        assert "vacuum_stale_sentinels" in admin_ops()

    def test_deletes_stale_rows(self, monkeypatch):
        import yadgar._shared.runtime.state as _st
        from yadgar.backend.admin_exec.memory import vacuum_stale_sentinels

        storage = MagicMock()
        storage._q.return_value = [{"id": "memory:3"}, {"id": "memory:9"}]
        storage._extract_id.side_effect = lambda raw: int(str(raw).split(":")[-1])
        monkeypatch.setattr(_st, "_storage", storage)

        result = vacuum_stale_sentinels({"retention_days": 0})

        assert result["deleted"] == 2
        assert storage.delete_memory.call_count == 2
        storage.delete_memory.assert_any_call(3)
        storage.delete_memory.assert_any_call(9)

    def test_never_raises_on_query_error(self, monkeypatch):
        import yadgar._shared.runtime.state as _st
        from yadgar.backend.admin_exec.memory import vacuum_stale_sentinels

        storage = MagicMock()
        storage._q.side_effect = Exception("db gone")
        monkeypatch.setattr(_st, "_storage", storage)

        result = vacuum_stale_sentinels({"retention_days": 0})
        assert result["deleted"] == 0


# ---------------------------------------------------------------------------
# Core sides no longer write directly
# ---------------------------------------------------------------------------


class TestCoreSidesForward:
    def test_http_has_no_delete_memory_write(self):
        import inspect

        import yadgar.core.server.http as http_mod

        src = inspect.getsource(http_mod)
        assert "storage.delete_memory" not in src, (
            "sentinel vacuum must forward to the backend vacuum_stale_sentinels op"
        )

    def test_admin_other_has_no_staleness_write(self):
        import inspect

        import yadgar.core.server.tools.admin_other as admin_other_mod

        src = inspect.getsource(admin_other_mod)
        assert "storage.update_memory_staleness" not in src, (
            "validate_memory fallback must forward via the update_memory_staleness admin op"
        )


# ---------------------------------------------------------------------------
# bootstrap_project_store op (the 6th raw-write path — found by the Car F sweep)
# ---------------------------------------------------------------------------


class TestBootstrapProjectStoreOp:
    def test_registered(self):
        from yadgar.backend.admin_exec import admin_ops

        assert "bootstrap_project_store" in admin_ops()

    def test_upserts_init_and_seeds_blocks(self, monkeypatch):
        import yadgar._shared.runtime.state as _st
        from yadgar.backend.admin_exec.project import bootstrap_project_store

        storage = MagicMock()
        storage.upsert_project_init.return_value = {"id": 1, "content": "x"}
        storage.get_block.return_value = None
        monkeypatch.setattr(_st, "_storage", storage)

        result = bootstrap_project_store(
            {"resolved": "/proj/root", "content": "# init", "project_id": "test-owner/test-repo"}
        )

        # C5b: the op threads the wire ``project_id`` into the upsert verbatim.
        storage.upsert_project_init.assert_called_once_with(
            "/proj/root", "# init", project_id="test-owner/test-repo"
        )
        # Default blocks seeded (current_task + gotchas)
        assert storage.create_block.call_count == 2
        assert result == {"id": 1, "content": "x"}

    def test_core_bootstrap_project_has_no_raw_write(self):
        import inspect

        import yadgar.core.server.tools.project as project_mod

        src = inspect.getsource(project_mod.bootstrap_project)
        assert "upsert_project_init" not in src, (
            "bootstrap_project must forward its store phase via the "
            "bootstrap_project_store admin op"
        )
