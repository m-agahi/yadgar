"""PR #92 review round 1, B1 — a backend fault BODY is a failed read, not "unset".

Backend ``get_config_row`` answers a storage fault with HTTP 200 and
``{"ok": False, "error": ...}``. Before this fix the core resolver read
``result.get("row")`` → None → "absent", and ``config_get`` CACHED the default
for the daemon lifetime. Scenario: core starts before SurrealDB is ready, so
``tasks.ledger_disabled`` is pinned off after SurrealDB recovers and
``task_write`` keeps writing to MariaDB.

A success body always carries ``"row"`` (possibly None); a body with
``ok: False`` or without ``"row"`` is a fault and raises ``ConfigReadError``,
which is never cached.
"""

from __future__ import annotations

import pytest

from yadgar.core import task_ledger_gate as gate

_DISABLED_KEY = "tasks.ledger_disabled"


@pytest.fixture(autouse=True)
def _clean_cache():
    """Drop the runtime_config core PTC between tests (no cross-bleed)."""
    from yadgar.core.cache.cache import _REGISTRY
    from yadgar.core.server.tools import _runtime_config as rc

    _REGISTRY.pop("runtime_config", None)
    rc._cache = None
    yield
    _REGISTRY.pop("runtime_config", None)
    rc._cache = None


@pytest.fixture
def backend(monkeypatch):
    """A fake backend keyed on op + key. ``state["fault"]`` answers a fault body."""
    from yadgar.core.server.tools import _runtime_config as rc

    state: dict = {"fault": {"ok": False, "error": "surreal not ready"}, "forwards": 0}

    def fake_forward(op: str, payload: dict, timeout_s: float = 30.0) -> dict:
        assert op == "get_config_row"
        state["forwards"] += 1
        if state["fault"] is not None:
            return state["fault"]
        if payload["key"] == _DISABLED_KEY and payload["directory"] is None:
            return {"row": {"key": _DISABLED_KEY, "directory": None, "value": True}}
        return {"row": None}

    monkeypatch.setattr(rc, "_forward_admin", fake_forward)
    return state


def test_fault_body_is_not_cached_and_the_gate_recovers(backend):
    from yadgar.core.server.tools import _runtime_config as rc

    assert gate.ledger_disabled(gate.daemon_resolver) is False
    assert rc._get_cache().get(rc._cache_key(_DISABLED_KEY, None)) is None

    backend["fault"] = None
    assert gate.ledger_disabled(gate.daemon_resolver) is True
    refusal = gate.write_refusal_for("x/y")
    assert refusal is not None
    assert refusal["error"] == gate.EXTERNAL_LEDGER_ERROR


@pytest.mark.parametrize("body", [{"ok": False, "error": "boom"}, {}, {"ok": True}])
def test_fault_body_raises_config_read_error_with_raise_on_error(backend, body):
    from yadgar.core.server.tools import _runtime_config as rc

    backend["fault"] = body
    with pytest.raises(rc.ConfigReadError):
        rc.config_get("k", None, "D", raise_on_error=True)


def test_fault_body_returns_default_uncached_without_raise_on_error(backend):
    from yadgar.core.server.tools import _runtime_config as rc

    assert rc.config_get("k", None, "D") == "D"
    assert rc.config_get("k", None, "D") == "D"
    assert backend["forwards"] == 2  # not cached: the second read forwards again


def test_absent_row_is_still_an_answer_and_is_cached(backend):
    from yadgar.core.server.tools import _runtime_config as rc

    backend["fault"] = None
    assert rc.config_get("absent", None, "D") == "D"
    assert rc.config_get("absent", None, "D") == "D"
    assert backend["forwards"] == 1
