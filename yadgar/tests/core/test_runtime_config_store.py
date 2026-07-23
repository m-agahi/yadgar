"""Car G1 — runtime_config storage layer + backend write admin ops (ADR-0163).

TDD red-first: tests written before implementation per project convention.

Storage layer (_RuntimeConfigMixin):
  set → get round-trip (global + per-directory)
  typed values (bool/int/str/list/dict) round-trip EXACTLY
  global vs per-dir rows for the SAME key coexist independently
  set is an upsert (second set updates, does not duplicate)
  delete is idempotent
  list_config_rows returns ALL rows (warmup) / scoped variants
  `directory IS NONE` vs `= $dir` scoping is correct (no cross-leak)

Backend admin ops (runtime_config_set / runtime_config_delete):
  registered in _ADMIN_OPS
  run_admin_op dispatch performs the storage write
"""

from __future__ import annotations

import pytest

from yadgar._shared.storage import StorageEngine

_PROJ_DIR = "/home/test/project"
_OTHER_DIR = "/home/test/other"


@pytest.fixture(scope="module")
def storage(module_storage):
    """Module-scoped shared StorageEngine; per-test data wipe via conftest."""
    return module_storage


# ---------------------------------------------------------------------------
# A. Storage layer — set/get round-trip
# ---------------------------------------------------------------------------


class TestSetGetRoundTrip:
    def test_set_get_global(self, storage: StorageEngine) -> None:
        storage.set_config_row("code_graph.enabled", True, directory=None)
        row = storage.get_config_row("code_graph.enabled", directory=None)
        assert row is not None
        assert row["key"] == "code_graph.enabled"
        assert row["directory"] is None
        assert row["value"] is True
        assert row["id"] is not None
        assert row["created_at"]
        assert row["updated_at"]

    def test_set_get_per_directory(self, storage: StorageEngine) -> None:
        storage.set_config_row("code_graph.enabled", False, directory=_PROJ_DIR)
        row = storage.get_config_row("code_graph.enabled", directory=_PROJ_DIR)
        assert row is not None
        assert row["directory"] == _PROJ_DIR
        assert row["value"] is False

    def test_get_missing_returns_none(self, storage: StorageEngine) -> None:
        assert storage.get_config_row("does.not.exist", directory=None) is None
        assert storage.get_config_row("does.not.exist", directory=_PROJ_DIR) is None


# ---------------------------------------------------------------------------
# B. Typed values round-trip exactly
# ---------------------------------------------------------------------------


class TestTypedValues:
    @pytest.mark.parametrize(
        "key,value",
        [
            ("t.bool_true", True),
            ("t.bool_false", False),
            ("t.int", 42),
            ("t.int_zero", 0),
            ("t.str", "hello world"),
            ("t.str_empty", ""),
            ("t.list", [1, "two", 3.0, True]),
            ("t.dict", {"a": 1, "nested": {"b": [True, None]}}),
        ],
    )
    def test_value_round_trips_typed(self, storage: StorageEngine, key, value) -> None:
        storage.set_config_row(key, value, directory=None)
        row = storage.get_config_row(key, directory=None)
        assert row is not None
        assert row["value"] == value
        assert type(row["value"]) is type(value)

    def test_bool_does_not_become_int_or_str(self, storage: StorageEngine) -> None:
        """Regression guard: a stored True must read back True, not 1 or 'true'."""
        storage.set_config_row("t.strict_bool", True, directory=None)
        row = storage.get_config_row("t.strict_bool", directory=None)
        assert row["value"] is True
        assert type(row["value"]) is bool


# ---------------------------------------------------------------------------
# C. Global vs per-dir coexistence + scoping isolation
# ---------------------------------------------------------------------------


class TestScopingIsolation:
    def test_same_key_global_and_per_dir_coexist(self, storage: StorageEngine) -> None:
        storage.set_config_row("feature.x", "global-val", directory=None)
        storage.set_config_row("feature.x", "proj-val", directory=_PROJ_DIR)

        g = storage.get_config_row("feature.x", directory=None)
        p = storage.get_config_row("feature.x", directory=_PROJ_DIR)
        assert g["value"] == "global-val"
        assert p["value"] == "proj-val"

    def test_global_get_does_not_return_per_dir_row(self, storage: StorageEngine) -> None:
        storage.set_config_row("only.perdir", "here", directory=_PROJ_DIR)
        assert storage.get_config_row("only.perdir", directory=None) is None

    def test_per_dir_get_does_not_return_global_row(self, storage: StorageEngine) -> None:
        storage.set_config_row("only.global", "here", directory=None)
        assert storage.get_config_row("only.global", directory=_PROJ_DIR) is None

    def test_distinct_dirs_isolated(self, storage: StorageEngine) -> None:
        storage.set_config_row("k.iso", "proj", directory=_PROJ_DIR)
        storage.set_config_row("k.iso", "other", directory=_OTHER_DIR)
        assert storage.get_config_row("k.iso", directory=_PROJ_DIR)["value"] == "proj"
        assert storage.get_config_row("k.iso", directory=_OTHER_DIR)["value"] == "other"


# ---------------------------------------------------------------------------
# D. Upsert semantics
# ---------------------------------------------------------------------------


class TestUpsert:
    def test_set_updates_existing_no_duplicate(self, storage: StorageEngine) -> None:
        first = storage.set_config_row("up.key", "v1", directory=None)
        second = storage.set_config_row("up.key", "v2", directory=None)

        row = storage.get_config_row("up.key", directory=None)
        assert row["value"] == "v2"
        # created_at preserved across the update; id stable; exactly one row.
        assert second["id"] == first["id"]
        assert second["created_at"] == first["created_at"]
        rows = [r for r in storage.list_config_rows(None) if r["key"] == "up.key"]
        assert len(rows) == 1

    def test_set_bumps_updated_at(self, storage: StorageEngine) -> None:
        first = storage.set_config_row("up.ts", 1, directory=None)
        second = storage.set_config_row("up.ts", 2, directory=None)
        assert second["updated_at"] >= first["updated_at"]


# ---------------------------------------------------------------------------
# E. Delete is idempotent
# ---------------------------------------------------------------------------


class TestDelete:
    def test_delete_removes_row(self, storage: StorageEngine) -> None:
        storage.set_config_row("del.key", "x", directory=None)
        storage.delete_config_row("del.key", directory=None)
        assert storage.get_config_row("del.key", directory=None) is None

    def test_delete_missing_is_idempotent(self, storage: StorageEngine) -> None:
        # No error on absent row, and calling twice is fine.
        storage.delete_config_row("never.existed", directory=None)
        storage.delete_config_row("never.existed", directory=_PROJ_DIR)

    def test_delete_per_dir_does_not_touch_global(self, storage: StorageEngine) -> None:
        storage.set_config_row("del.scoped", "g", directory=None)
        storage.set_config_row("del.scoped", "p", directory=_PROJ_DIR)
        storage.delete_config_row("del.scoped", directory=_PROJ_DIR)
        assert storage.get_config_row("del.scoped", directory=_PROJ_DIR) is None
        assert storage.get_config_row("del.scoped", directory=None)["value"] == "g"


# ---------------------------------------------------------------------------
# F. list_config_rows — warmup (all) + scoped
# ---------------------------------------------------------------------------


class TestListConfigRows:
    def test_list_all_returns_global_and_every_dir(self, storage: StorageEngine) -> None:
        """Warmup bulk-read: sentinel default returns global + every directory row."""
        storage.set_config_row("warm.a", 1, directory=None)
        storage.set_config_row("warm.b", 2, directory=_PROJ_DIR)
        storage.set_config_row("warm.c", 3, directory=_OTHER_DIR)

        all_rows = storage.list_config_rows()  # sentinel default → ALL
        by_key = {(r["key"], r["directory"]): r["value"] for r in all_rows}
        assert by_key[("warm.a", None)] == 1
        assert by_key[("warm.b", _PROJ_DIR)] == 2
        assert by_key[("warm.c", _OTHER_DIR)] == 3

    def test_list_directory_none_is_global_only(self, storage: StorageEngine) -> None:
        storage.set_config_row("scoped.g", "g", directory=None)
        storage.set_config_row("scoped.p", "p", directory=_PROJ_DIR)
        rows = storage.list_config_rows(None)
        keys = {r["key"] for r in rows}
        assert "scoped.g" in keys
        assert "scoped.p" not in keys
        assert all(r["directory"] is None for r in rows)

    def test_list_directory_scoped(self, storage: StorageEngine) -> None:
        storage.set_config_row("d.g", "g", directory=None)
        storage.set_config_row("d.p", "p", directory=_PROJ_DIR)
        rows = storage.list_config_rows(_PROJ_DIR)
        keys = {r["key"] for r in rows}
        assert "d.p" in keys
        assert "d.g" not in keys
        assert all(r["directory"] == _PROJ_DIR for r in rows)

    def test_list_values_are_decoded(self, storage: StorageEngine) -> None:
        storage.set_config_row("decode.me", {"nested": True}, directory=None)
        rows = storage.list_config_rows(None)
        row = next(r for r in rows if r["key"] == "decode.me")
        assert row["value"] == {"nested": True}


# ---------------------------------------------------------------------------
# G. Backend admin ops — registration + dispatch
# ---------------------------------------------------------------------------


@pytest.fixture
def _wire_backend_storage(storage, monkeypatch):
    """Point backend _get_storage() at the module engine so op writes land where
    the assertions read."""
    import yadgar._shared.runtime.state as _st

    monkeypatch.setattr(_st, "_storage", storage)


class TestAdminOpsRegistered:
    def test_write_ops_registered(self) -> None:
        from yadgar.backend.admin_exec import admin_ops

        ops = admin_ops()
        assert "runtime_config_set" in ops
        assert "runtime_config_delete" in ops

    def test_read_ops_not_registered(self) -> None:
        """Reads stay core (via _get_storage), mirroring blocks — no read admin op."""
        from yadgar.backend.admin_exec import admin_ops

        ops = admin_ops()
        assert "runtime_config_get" not in ops
        assert "runtime_config_list" not in ops


class TestAdminOpsDispatch:
    def test_set_op_writes(self, storage: StorageEngine, _wire_backend_storage) -> None:
        from yadgar.backend.admin_exec import run_admin_op

        result = run_admin_op(
            "runtime_config_set",
            {"key": "op.enabled", "value": True, "directory": _PROJ_DIR},
        )
        assert result.get("ok") is not False
        assert result["value"] is True
        row = storage.get_config_row("op.enabled", directory=_PROJ_DIR)
        assert row["value"] is True

    def test_set_op_global(self, storage: StorageEngine, _wire_backend_storage) -> None:
        from yadgar.backend.admin_exec import run_admin_op

        run_admin_op(
            "runtime_config_set",
            {"key": "op.g", "value": [1, 2, 3], "directory": None},
        )
        assert storage.get_config_row("op.g", directory=None)["value"] == [1, 2, 3]

    def test_delete_op_removes(self, storage: StorageEngine, _wire_backend_storage) -> None:
        from yadgar.backend.admin_exec import run_admin_op

        storage.set_config_row("op.del", "x", directory=None)
        result = run_admin_op("runtime_config_delete", {"key": "op.del", "directory": None})
        assert result.get("deleted") is True
        assert storage.get_config_row("op.del", directory=None) is None

    def test_delete_op_idempotent(self, _wire_backend_storage) -> None:
        from yadgar.backend.admin_exec import run_admin_op

        result = run_admin_op("runtime_config_delete", {"key": "op.never", "directory": None})
        assert result.get("deleted") is True
