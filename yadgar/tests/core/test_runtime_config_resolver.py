"""Car B + G2 — runtime_config cache + HTTP-forward resolver + warmup + invalidation.

ADR-0163 storage half (``_RuntimeConfigMixin``) + Car G2 cache + Car B HTTP
forward: ``config_get`` / ``config_list`` no longer touch ``_get_storage()``
in core — they forward to the backend ``get_config_row`` / ``list_config_rows``
admin ops (ADR-0078 / ADR-0200 chokepoint).

PTC resolver (config_get):
  cache HIT — a second get does NOT forward over HTTP
  cache MISS — populates the cache from the backend forward
  per-dir row OVERRIDES global for the SAME key
  global FALLBACK when no per-dir row exists
  DEFAULT when neither per-dir nor global row exists
  typed values (bool/int/str/list/dict) pass through unchanged
  forward EXCEPTION → returns default (never raises)

invalidate_config_cache():
  empties the cache — the next get re-forwards

warmup_runtime_config_cache():
  pre-populates the cache via forward — a get after warmup is a HIT (no HTTP)
  best-effort: a failing forward does not raise
"""

from __future__ import annotations

import pytest

from yadgar.core.server.tools import _runtime_config as rc

_PROJ_DIR = "/home/test/project"
_OTHER_DIR = "/home/test/other"


class _FakeBackend:
    """In-memory stand-in for the backend ``get_config_row`` admin op.

    ``rows`` maps (key, directory) → value. ``calls`` counts forward
    invocations so tests can assert PTC hits skip the HTTP round-trip.
    ``raise_on_get`` forces the fail-safe path.
    """

    def __init__(self, rows=None, raise_on_get: bool = False):
        self.rows = dict(rows or {})
        self.calls = 0
        self.list_calls = 0
        self.raise_on_get = raise_on_get

    def forward_get(self, key: str, directory: str | None) -> dict:
        self.calls += 1
        if self.raise_on_get:
            raise RuntimeError("boom")
        if (key, directory) in self.rows:
            return {
                "row": {"key": key, "directory": directory, "value": self.rows[(key, directory)]}
            }
        return {"row": None}

    def forward_list(self) -> dict:
        self.list_calls += 1
        return {"rows": [{"key": k, "directory": d, "value": v} for (k, d), v in self.rows.items()]}


@pytest.fixture
def fresh_cache(monkeypatch):
    """Empty the runtime_config cache before + after each test (no cross-bleed)."""
    rc._get_cache().clear()
    yield
    rc._get_cache().clear()


def _install(monkeypatch, backend: _FakeBackend) -> _FakeBackend:
    """Point the resolver's _forward_admin at a fake backend and return it."""

    def fake_forward(op: str, payload: dict, timeout_s: float = 30.0) -> dict:
        if op == "get_config_row":
            return backend.forward_get(payload["key"], payload.get("directory"))
        if op == "list_config_rows":
            return backend.forward_list()
        raise RuntimeError(f"unexpected op {op!r}")

    monkeypatch.setattr(rc, "_forward_admin", fake_forward)
    return backend


# ---------------------------------------------------------------------------
# A. PTC hit / miss
# ---------------------------------------------------------------------------


class TestPTC:
    def test_miss_then_hit_second_get_skips_forward(self, monkeypatch, fresh_cache):
        backend = _install(monkeypatch, _FakeBackend({("k", None): "v"}))
        assert rc.config_get("k") == "v"
        assert backend.calls == 1  # miss → one forward
        assert rc.config_get("k") == "v"
        assert backend.calls == 1  # hit → NO further forward

    def test_miss_populates_cache(self, monkeypatch, fresh_cache):
        _install(monkeypatch, _FakeBackend({("k", None): 42}))
        rc.config_get("k")
        # value is now cached under the requested (key, dir) key
        assert rc._get_cache().get(rc._cache_key("k", None)) == 42


# ---------------------------------------------------------------------------
# B. Resolution (per-dir → global → default)
# ---------------------------------------------------------------------------


class TestResolution:
    def test_per_dir_overrides_global_same_key(self, monkeypatch, fresh_cache):
        _install(
            monkeypatch,
            _FakeBackend(
                {("code_graph.enabled", None): True, ("code_graph.enabled", _PROJ_DIR): False}
            ),
        )
        assert rc.config_get("code_graph.enabled", _PROJ_DIR) is False
        # global still resolves to True for a different dir / no dir
        assert rc.config_get("code_graph.enabled", None) is True

    def test_global_fallback_when_no_per_dir_row(self, monkeypatch, fresh_cache):
        _install(monkeypatch, _FakeBackend({("k", None): "glob"}))
        # requested for a dir with no row → falls back to global
        assert rc.config_get("k", _OTHER_DIR) == "glob"

    def test_default_when_neither_exists(self, monkeypatch, fresh_cache):
        _install(monkeypatch, _FakeBackend({}))
        assert rc.config_get("missing", _PROJ_DIR, default="fallback") == "fallback"


# ---------------------------------------------------------------------------
# C. Typed pass-through
# ---------------------------------------------------------------------------


class TestTypedValues:
    @pytest.mark.parametrize(
        "value",
        [True, False, 0, 123, "str", ["a", "b"], {"x": 1}],
    )
    def test_typed_values_pass_through(self, monkeypatch, fresh_cache, value):
        _install(monkeypatch, _FakeBackend({("k", None): value}))
        assert rc.config_get("k") == value


# ---------------------------------------------------------------------------
# D. Fail-safe
# ---------------------------------------------------------------------------


class TestFailSafe:
    def test_forward_exception_returns_default_no_raise(self, monkeypatch, fresh_cache):
        _install(monkeypatch, _FakeBackend({}, raise_on_get=True))
        assert rc.config_get("k", _PROJ_DIR, default="safe") == "safe"


# ---------------------------------------------------------------------------
# E. Invalidation
# ---------------------------------------------------------------------------


class TestInvalidation:
    def test_invalidate_empties_cache_next_get_rereads(self, monkeypatch, fresh_cache):
        backend = _install(monkeypatch, _FakeBackend({("k", None): "v"}))
        rc.config_get("k")
        assert backend.calls == 1
        rc.invalidate_config_cache()
        rc.config_get("k")
        assert backend.calls == 2  # cache emptied → forward again


# ---------------------------------------------------------------------------
# F. Warmup
# ---------------------------------------------------------------------------


class TestWarmup:
    def test_warmup_prepopulates_get_is_hit(self, monkeypatch, fresh_cache):
        backend = _FakeBackend({("k", None): "v", ("k2", _PROJ_DIR): 9})
        _install(monkeypatch, backend)
        rc.warmup_runtime_config_cache(object())  # arg ignored under Car B
        # a get after warmup is a HIT — no get_config_row forward
        assert rc.config_get("k") == "v"
        assert rc.config_get("k2", _PROJ_DIR) == 9
        assert backend.calls == 0  # warmup used list_config_rows, not per-key gets
        assert backend.list_calls == 1

    def test_warmup_none_storage_noop(self, monkeypatch, fresh_cache):
        _install(monkeypatch, _FakeBackend({}))
        rc.warmup_runtime_config_cache(None)  # must not raise

    def test_warmup_failure_swallowed(self, monkeypatch, fresh_cache):
        def boom(op, payload, timeout_s=30.0):
            raise RuntimeError("boom")

        monkeypatch.setattr(rc, "_forward_admin", boom)
        rc.warmup_runtime_config_cache(object())  # must not raise
