"""Car G2 — runtime_config cache + PTC resolver + warmup + invalidation (ADR-0163).

TDD red-first: tests written before the resolver implementation per project
convention.

PTC resolver (config_get):
  cache HIT — a second get does NOT touch storage
  cache MISS — populates the cache from the durable store
  per-dir row OVERRIDES global for the SAME key
  global FALLBACK when no per-dir row exists
  DEFAULT when neither per-dir nor global row exists
  typed values (bool/int/str/list/dict) pass through unchanged
  storage EXCEPTION → returns default (never raises)

invalidate_config_cache():
  empties the cache — the next get re-reads storage

warmup_runtime_config_cache():
  pre-populates the cache — a get after warmup is a HIT (no storage call)
  best-effort: a failing/None storage does not raise
"""

from __future__ import annotations

import pytest

from yadgar.core.server.tools import _runtime_config as rc

_PROJ_DIR = "/home/test/project"
_OTHER_DIR = "/home/test/other"


class _FakeStorage:
    """In-memory stand-in for the StorageEngine runtime_config surface.

    ``rows`` maps (key, directory) → value. ``calls`` counts get_config_row
    invocations so tests can assert PTC hits skip storage. ``raise_on_get`` forces
    the fail-safe path.
    """

    def __init__(self, rows=None, raise_on_get=False):
        self.rows = dict(rows or {})
        self.calls = 0
        self.list_calls = 0
        self.raise_on_get = raise_on_get

    def get_config_row(self, key, *, directory):
        self.calls += 1
        if self.raise_on_get:
            raise RuntimeError("boom")
        if (key, directory) in self.rows:
            return {"key": key, "directory": directory, "value": self.rows[(key, directory)]}
        return None

    def list_config_rows(self, *args, **kwargs):
        self.list_calls += 1
        return [{"key": k, "directory": d, "value": v} for (k, d), v in self.rows.items()]


@pytest.fixture
def fresh_cache(monkeypatch):
    """Empty the runtime_config cache before + after each test (no cross-bleed)."""
    rc._get_cache().clear()
    yield
    rc._get_cache().clear()


def _install(monkeypatch, storage):
    """Point the resolver's _get_storage at a fake and return it."""
    monkeypatch.setattr(rc, "_get_storage", lambda: storage)
    return storage


# ---------------------------------------------------------------------------
# A. PTC hit / miss
# ---------------------------------------------------------------------------


class TestPTC:
    def test_miss_then_hit_second_get_skips_storage(self, monkeypatch, fresh_cache):
        storage = _install(monkeypatch, _FakeStorage({("k", None): "v"}))
        assert rc.config_get("k") == "v"
        assert storage.calls == 1  # miss → one storage read
        assert rc.config_get("k") == "v"
        assert storage.calls == 1  # hit → NO further storage read

    def test_miss_populates_cache(self, monkeypatch, fresh_cache):
        _install(monkeypatch, _FakeStorage({("k", None): 42}))
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
            _FakeStorage(
                {("code_graph.enabled", None): True, ("code_graph.enabled", _PROJ_DIR): False}
            ),
        )
        assert rc.config_get("code_graph.enabled", _PROJ_DIR) is False
        # global still resolves to True for a different dir / no dir
        assert rc.config_get("code_graph.enabled", None) is True

    def test_global_fallback_when_no_per_dir_row(self, monkeypatch, fresh_cache):
        _install(monkeypatch, _FakeStorage({("k", None): "glob"}))
        # requested for a dir with no row → falls back to global
        assert rc.config_get("k", _OTHER_DIR) == "glob"

    def test_default_when_neither_exists(self, monkeypatch, fresh_cache):
        _install(monkeypatch, _FakeStorage({}))
        assert rc.config_get("missing", _PROJ_DIR, default="fallback") == "fallback"

    def test_default_when_storage_none(self, monkeypatch, fresh_cache):
        _install(monkeypatch, None)
        assert rc.config_get("k", default="d") == "d"


# ---------------------------------------------------------------------------
# C. Typed pass-through
# ---------------------------------------------------------------------------


class TestTypedValues:
    @pytest.mark.parametrize(
        "value",
        [True, False, 0, 123, "str", ["a", "b"], {"x": 1}],
    )
    def test_typed_values_pass_through(self, monkeypatch, fresh_cache, value):
        _install(monkeypatch, _FakeStorage({("k", None): value}))
        assert rc.config_get("k") == value


# ---------------------------------------------------------------------------
# D. Fail-safe
# ---------------------------------------------------------------------------


class TestFailSafe:
    def test_storage_exception_returns_default_no_raise(self, monkeypatch, fresh_cache):
        _install(monkeypatch, _FakeStorage({}, raise_on_get=True))
        assert rc.config_get("k", _PROJ_DIR, default="safe") == "safe"


# ---------------------------------------------------------------------------
# E. Invalidation
# ---------------------------------------------------------------------------


class TestInvalidation:
    def test_invalidate_empties_cache_next_get_rereads(self, monkeypatch, fresh_cache):
        storage = _install(monkeypatch, _FakeStorage({("k", None): "v"}))
        rc.config_get("k")
        assert storage.calls == 1
        rc.invalidate_config_cache()
        rc.config_get("k")
        assert storage.calls == 2  # cache emptied → storage read again


# ---------------------------------------------------------------------------
# F. Warmup
# ---------------------------------------------------------------------------


class TestWarmup:
    def test_warmup_prepopulates_get_is_hit(self, monkeypatch, fresh_cache):
        storage = _FakeStorage({("k", None): "v", ("k2", _PROJ_DIR): 9})
        _install(monkeypatch, storage)
        rc.warmup_runtime_config_cache(storage)
        # a get after warmup is a HIT — no get_config_row call
        assert rc.config_get("k") == "v"
        assert rc.config_get("k2", _PROJ_DIR) == 9
        assert storage.calls == 0  # warmup used list_config_rows, not per-key gets

    def test_warmup_none_storage_noop(self, monkeypatch, fresh_cache):
        rc.warmup_runtime_config_cache(None)  # must not raise

    def test_warmup_failure_swallowed(self, monkeypatch, fresh_cache):
        class _Boom:
            def list_config_rows(self, *a, **k):
                raise RuntimeError("boom")

        rc.warmup_runtime_config_cache(_Boom())  # must not raise
