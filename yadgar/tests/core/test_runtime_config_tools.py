"""Car G3 — runtime_config MCP tools (ADR-0163).

TDD red-first: tests written before the tool module per project convention.

Four tools in ``yadgar.core.server.tools.runtime_config``:
  config_get(key, directory=None, default=None)       — resolved value (PTC read)
  config_list(directory=None)                          — effective rows (debug/read)
  config_set(key, value, scope="global", directory=None) — validate + forward + invalidate
  config_delete(key, scope="global", directory=None)  — forward + invalidate

Scope→directory mapping (matches the backend ``{key, value, directory}`` payload):
  scope="global"  → directory=None
  scope="project" → directory=<given dir> (required)

config_set / config_delete write via ``_forward_admin`` (G1 admin ops) then call
``invalidate_config_cache()``. config_get / config_list read core-side.
"""

from __future__ import annotations

import pytest

from yadgar.core.server.tools import runtime_config as tools


class _FakeStorage:
    """Stand-in for the StorageEngine runtime_config read surface."""

    def __init__(self, rows=None):
        # rows: list of {key, directory, value}
        self.rows = list(rows or [])
        self.list_calls: list = []

    def list_config_rows(self, directory="__UNSET__"):
        self.list_calls.append(directory)
        if directory == "__UNSET__":
            return list(self.rows)
        return [r for r in self.rows if r.get("directory") == directory]


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Record forwards + invalidations; keep the cache clean between tests."""
    calls: dict = {"forward": [], "invalidate": 0}

    def _fake_forward(op, payload):
        calls["forward"].append((op, payload))
        # Mirror the backend op return shapes.
        if op == "runtime_config_set":
            return {
                "key": payload["key"],
                "directory": payload.get("directory"),
                "value": payload["value"],
            }
        return {"deleted": True, "key": payload["key"]}

    def _fake_invalidate():
        calls["invalidate"] += 1

    monkeypatch.setattr(tools, "_forward_admin", _fake_forward)
    monkeypatch.setattr(tools, "invalidate_config_cache", _fake_invalidate)
    return calls


# ---------------------------------------------------------------------------
# config_get — resolver passthrough
# ---------------------------------------------------------------------------


class TestConfigGet:
    def test_returns_resolved_value(self, monkeypatch):
        monkeypatch.setattr(
            tools, "_resolver_get", lambda key, directory=None, default=None: "resolved"
        )
        assert tools.config_get("some.key") == "resolved"

    def test_passes_directory_and_default_through(self, monkeypatch):
        seen: dict = {}

        def _fake(key, directory=None, default=None):
            seen["key"] = key
            seen["directory"] = directory
            seen["default"] = default
            return default

        monkeypatch.setattr(tools, "_resolver_get", _fake)
        out = tools.config_get("k", directory="/proj", default=False)
        assert out is False
        assert seen == {"key": "k", "directory": "/proj", "default": False}


# ---------------------------------------------------------------------------
# config_list — effective rows
# ---------------------------------------------------------------------------


class TestConfigList:
    def test_lists_all_rows_when_no_directory(self, monkeypatch):
        rows = [
            {"key": "a", "directory": None, "value": 1},
            {"key": "b", "directory": "/proj", "value": 2},
        ]
        monkeypatch.setattr(tools, "_get_storage", lambda: _FakeStorage(rows))
        out = tools.config_list()
        assert {r["key"] for r in out} == {"a", "b"}

    def test_scopes_to_directory(self, monkeypatch):
        rows = [{"key": "b", "directory": "/proj", "value": 2}]
        fake = _FakeStorage(rows)
        monkeypatch.setattr(tools, "_get_storage", lambda: fake)
        out = tools.config_list(directory="/proj")
        assert out == rows
        assert fake.list_calls == ["/proj"]

    def test_empty_on_no_storage(self, monkeypatch):
        monkeypatch.setattr(tools, "_get_storage", lambda: None)
        assert tools.config_list() == []


# ---------------------------------------------------------------------------
# config_set — validate + forward + invalidate
# ---------------------------------------------------------------------------


class TestConfigSet:
    def test_global_forwards_with_null_directory(self, _isolate):
        out = tools.config_set("k", True, scope="global")
        assert _isolate["forward"] == [
            ("runtime_config_set", {"key": "k", "value": True, "directory": None})
        ]
        assert _isolate["invalidate"] == 1
        assert out["value"] is True

    def test_project_forwards_with_directory(self, _isolate):
        tools.config_set("k", 7, scope="project", directory="/proj")
        op, payload = _isolate["forward"][0]
        assert op == "runtime_config_set"
        assert payload == {"key": "k", "value": 7, "directory": "/proj"}
        assert _isolate["invalidate"] == 1

    def test_project_without_directory_rejected(self, _isolate):
        out = tools.config_set("k", 1, scope="project")
        assert out["ok"] is False
        assert _isolate["forward"] == []  # never wrote
        assert _isolate["invalidate"] == 0

    def test_invalid_scope_rejected(self, _isolate):
        out = tools.config_set("k", 1, scope="bogus")
        assert out["ok"] is False
        assert _isolate["forward"] == []

    @pytest.mark.parametrize("value", [True, 42, "text", [1, 2], {"a": 1}])
    def test_serializable_types_accepted(self, _isolate, value):
        out = tools.config_set("k", value, scope="global")
        assert out.get("ok") is not False
        assert _isolate["forward"], "expected a forward for a valid type"

    @pytest.mark.parametrize("value", [object(), b"bytes", 3.14 + 1j])
    def test_non_serializable_types_rejected(self, _isolate, value):
        out = tools.config_set("k", value, scope="global")
        assert out["ok"] is False
        assert _isolate["forward"] == []
        assert _isolate["invalidate"] == 0


# ---------------------------------------------------------------------------
# config_delete — forward + invalidate
# ---------------------------------------------------------------------------


class TestConfigDelete:
    def test_global_delete_forwards_null_directory(self, _isolate):
        out = tools.config_delete("k", scope="global")
        assert _isolate["forward"] == [("runtime_config_delete", {"key": "k", "directory": None})]
        assert _isolate["invalidate"] == 1
        assert out["deleted"] is True

    def test_project_delete_forwards_directory(self, _isolate):
        tools.config_delete("k", scope="project", directory="/proj")
        op, payload = _isolate["forward"][0]
        assert op == "runtime_config_delete"
        assert payload == {"key": "k", "directory": "/proj"}
        assert _isolate["invalidate"] == 1

    def test_project_delete_without_directory_rejected(self, _isolate):
        out = tools.config_delete("k", scope="project")
        assert out["ok"] is False
        assert _isolate["forward"] == []
        assert _isolate["invalidate"] == 0
