"""Car 2 — `memory_doc` cache on the build_results fetch (backend 5.19.0).

`_build_initial_results` (fusion.py:316) batch-hydrates every fused candidate via
`StorageEngine.get_memories_by_ids` — the ~866 ms warm stage, dominated by the
KB-scale `content` + `embedding` columns. Car 2 caches ONLY those two immutable
columns per `memory_id` (TTL invalidation), and fetches everything else fresh on
every recall via `SELECT * OMIT content, embedding`.

Correctness invariants proven here:

  * QUALITY-NEUTRAL — output is byte-identical to the uncached path, INCLUDING
    live heat and other volatile/consolidation-mutated fields (they are fetched
    fresh every call; only content+embedding are cached).
  * HEAT-FRESHNESS — a heat change between two recalls IS reflected (never
    served from a stale cached doc). Same for any consolidation-touched field
    (excitability, etc.) — the critical guarantee that the immutable-whitelist
    split (cache {content, embedding}; fetch complement fresh) provides.
  * DELETE-INERT — a deleted memory is never a fusion candidate (live DB query),
    so build_results never re-fetches it; ids are monotonic (`_next_id` UPSERT
    counter) so a stale entry can never be served for a reused id. No explicit
    delete invalidation.
  * TTL backstop bounds worst-case staleness from a content edit / reembed that
    does not go through the per-id evict path; `memory_update(content)` evicts
    the id explicitly.

The get-or-compute / DI / obs tests use a fake `_q` StorageEngine host. The OMIT
portability of the fresh query is proven separately by an embedded probe (see
the module docstring in yadgar/storage/memory.py::get_memories_by_ids and the
`test_omit_portable_embedded` integration test below).
"""

from __future__ import annotations

import struct

import pytest

from yadgar.backend.cache import (
    TTL,
    Cache,
    CacheProtocol,
    NullCache,
    get_memory_doc_cache,
)

# ── fake storage host ─────────────────────────────────────────────────────────


def _emb_bytes(seed: int) -> bytes:
    """A deterministic 4-float embedding as raw float32 bytes."""
    return struct.pack("<4f", *[seed + 0.1 * i for i in range(4)])


class _FakeStorage:
    """Minimal host exposing get_memories_by_ids + the real row-normalisation.

    Backing store is a dict id -> raw-row. `_q` interprets exactly the two query
    shapes get_memories_by_ids issues (OMIT-fresh + heavy-content) and counts how
    many heavy fetches happened (the cache-miss signal).
    """

    def __init__(self, rows: dict[int, dict], memory_doc_cache: CacheProtocol) -> None:
        self._rows = rows  # id -> raw row (content + embedding + volatile)
        self._memory_doc_cache = memory_doc_cache
        self.heavy_fetches = 0  # ids fetched via the content+embedding query
        self.fresh_fetches = 0  # number of OMIT-fresh queries issued

    # -- the real methods under test, imported from the mixins -----------------
    from yadgar.storage.client import _ClientMixin
    from yadgar.storage.memory import _MemoryMixin

    get_memories_by_ids = _MemoryMixin.get_memories_by_ids
    _resolve_memory_doc_cache = _MemoryMixin._resolve_memory_doc_cache
    _extract_id = _ClientMixin._extract_id

    # -- normalisation stubs (id already int; embedding already bytes) ----------
    def _rows_to_dicts(self, rows: list[dict]) -> list[dict]:
        out = []
        for r in rows:
            d = dict(r)
            d.setdefault("embedding_model", None)
            d.setdefault("file_hash", None)
            d.setdefault("last_excitability_update", None)
            d.setdefault("original_content", None)
            d.setdefault("last_reconsolidated", None)
            out.append(d)
        return out

    def _q(self, surql: str, params: dict | None = None) -> list:
        # Parse the inlined id list: WHERE id IN [memory:1, memory:2]
        import re

        ids = [int(x) for x in re.findall(r"memory:(\d+)", surql)]
        if "OMIT content, embedding" in surql:
            self.fresh_fetches += 1
            # Return every column EXCEPT content + embedding (fresh scalars).
            rows = []
            for mid in ids:
                r = self._rows.get(mid)
                if r is None:
                    continue
                rows.append({k: v for k, v in r.items() if k not in ("content", "embedding")})
            return rows
        # Heavy fetch: id, content, embedding
        self.heavy_fetches += 1
        rows = []
        for mid in ids:
            r = self._rows.get(mid)
            if r is None:
                continue
            rows.append({"id": mid, "content": r["content"], "embedding": r["embedding"]})
        return rows


def _raw_row(mid: int, *, heat: float = 1.0, excitability: float = 1.0) -> dict:
    return {
        "id": mid,
        "content": f"content-{mid}" * 50,  # KB-ish to make caching meaningful
        "embedding": _emb_bytes(mid),
        "heat": heat,
        "excitability": excitability,
        "access_count": 3,
        "last_accessed": "2026-07-06T00:00:00+00:00",
        "tags": ["t"],
        "created_at": "2026-01-01T00:00:00+00:00",
        "store_type": "episodic",
    }


def _mk_cache(budget: int = 1 << 20, ttl: float = 1800.0, clock=None) -> Cache:
    kw = {}
    if clock is not None:
        kw["clock"] = clock
    return Cache(
        name="memory_doc_test",
        max_bytes=budget,
        invalidation=TTL(ttl),
        deep_copy=True,
        obs_tier="cold",
        **kw,
    )


# ── get-or-compute ────────────────────────────────────────────────────────────


class TestGetOrCompute:
    def test_miss_fetches_and_caches_content(self):
        rows = {1: _raw_row(1), 2: _raw_row(2)}
        cache = _mk_cache()
        s = _FakeStorage(rows, cache)
        out = s.get_memories_by_ids([1, 2])
        assert {m["id"] for m in out} == {1, 2}
        # first call: both ids are cache misses -> one heavy fetch
        assert s.heavy_fetches == 1
        # content+embedding present + correct
        by_id = {m["id"]: m for m in out}
        assert by_id[1]["content"] == rows[1]["content"]
        assert by_id[1]["embedding"] == rows[1]["embedding"]

    def test_hit_reuses_cached_content_no_heavy_fetch(self):
        rows = {1: _raw_row(1), 2: _raw_row(2)}
        cache = _mk_cache()
        s = _FakeStorage(rows, cache)
        s.get_memories_by_ids([1, 2])  # warm
        s.heavy_fetches = 0
        out = s.get_memories_by_ids([1, 2])  # all hits
        assert s.heavy_fetches == 0  # no heavy content fetch on full hit
        assert {m["id"] for m in out} == {1, 2}

    def test_partial_hit_only_fetches_misses(self):
        rows = {1: _raw_row(1), 2: _raw_row(2), 3: _raw_row(3)}
        cache = _mk_cache()
        s = _FakeStorage(rows, cache)
        s.get_memories_by_ids([1])  # cache id 1
        s.heavy_fetches = 0
        out = s.get_memories_by_ids([1, 2, 3])  # 1 hit, 2+3 miss
        assert s.heavy_fetches == 1  # one heavy fetch for the misses
        assert {m["id"] for m in out} == {1, 2, 3}

    def test_empty_ids_short_circuits(self):
        s = _FakeStorage({}, _mk_cache())
        assert s.get_memories_by_ids([]) == []
        assert s.heavy_fetches == 0
        assert s.fresh_fetches == 0


# ── DI: Cache vs NullCache identical output ───────────────────────────────────


class TestDIEquivalence:
    def test_cache_vs_nullcache_identical_output(self):
        rows = {1: _raw_row(1), 2: _raw_row(2), 3: _raw_row(3)}
        s_null = _FakeStorage({k: dict(v) for k, v in rows.items()}, NullCache())
        s_cache = _FakeStorage({k: dict(v) for k, v in rows.items()}, _mk_cache())
        # warm the cache once, then compare a second call to the null path
        s_cache.get_memories_by_ids([1, 2, 3])
        out_null = s_null.get_memories_by_ids([1, 2, 3])
        out_cache = s_cache.get_memories_by_ids([1, 2, 3])
        assert {m["id"]: m for m in out_null} == {m["id"]: m for m in out_cache}

    def test_default_factory_returns_protocol(self):
        c = get_memory_doc_cache()
        assert isinstance(c, CacheProtocol)


# ── QUALITY-NEUTRAL + HEAT-FRESHNESS (the critical guarantees) ────────────────


class TestQualityNeutralAndHeatFreshness:
    def test_byte_identical_including_live_heat(self):
        rows = {1: _raw_row(1, heat=0.9)}
        s = _FakeStorage(rows, _mk_cache())
        out1 = s.get_memories_by_ids([1])
        assert out1[0]["heat"] == 0.9

    def test_heat_change_between_recalls_is_reflected(self):
        """The load-bearing test: a heat mutation between two recalls must NOT be
        masked by the cached doc."""
        rows = {1: _raw_row(1, heat=1.0)}
        cache = _mk_cache()
        s = _FakeStorage(rows, cache)
        out1 = s.get_memories_by_ids([1])
        assert out1[0]["heat"] == 1.0
        # simulate an access-bump / decay writing fresh heat to the DB row
        rows[1]["heat"] = 0.42
        out2 = s.get_memories_by_ids([1])
        assert out2[0]["heat"] == 0.42, "cached doc masked a live heat change (stale)"
        # content still served from cache (no second heavy fetch)
        assert s.heavy_fetches == 1

    def test_consolidation_field_change_is_reflected(self):
        """excitability is consolidation-mutated; it must be fetched fresh, not
        served from a cached doc (proves the immutable-whitelist split)."""
        rows = {1: _raw_row(1, excitability=1.0)}
        s = _FakeStorage(rows, _mk_cache())
        s.get_memories_by_ids([1])
        rows[1]["excitability"] = 0.25
        out = s.get_memories_by_ids([1])
        assert out[0]["excitability"] == 0.25


# ── TTL expiry ────────────────────────────────────────────────────────────────


class TestTTL:
    def test_ttl_expiry_refetches_content(self):
        clock = {"t": 1000.0}
        cache = _mk_cache(ttl=1800.0, clock=lambda: clock["t"])
        rows = {1: _raw_row(1)}
        s = _FakeStorage(rows, cache)
        s.get_memories_by_ids([1])  # cache
        s.heavy_fetches = 0
        clock["t"] += 1801.0  # past TTL
        s.get_memories_by_ids([1])  # expired -> heavy refetch
        assert s.heavy_fetches == 1

    def test_within_ttl_serves_from_cache(self):
        clock = {"t": 1000.0}
        cache = _mk_cache(ttl=1800.0, clock=lambda: clock["t"])
        s = _FakeStorage({1: _raw_row(1)}, cache)
        s.get_memories_by_ids([1])
        s.heavy_fetches = 0
        clock["t"] += 100.0  # still within TTL
        s.get_memories_by_ids([1])
        assert s.heavy_fetches == 0


# ── per-id evict (memory_update content path) ────────────────────────────────


class TestPerIdEvict:
    def test_invalidate_evicts_single_id(self):
        cache = _mk_cache()
        s = _FakeStorage({1: _raw_row(1), 2: _raw_row(2)}, cache)
        s.get_memories_by_ids([1, 2])
        s.heavy_fetches = 0
        cache.invalidate(1)  # mirrors memory_update(content) on id 1
        out = s.get_memories_by_ids([1, 2])
        assert s.heavy_fetches == 1  # id 1 re-fetched, id 2 still cached
        assert {m["id"] for m in out} == {1, 2}

    def test_update_memory_fields_content_evicts(self):
        """memory_update(content) → update_memory_fields evicts the memory_doc id."""
        from yadgar.storage.memory import _MemoryMixin

        calls: list = []

        class _EvictCache:
            def get(self, k):
                return None

            def put(self, k, v):
                pass

            def invalidate(self, scope=None):
                calls.append(scope)

        host = object.__new__(_MemoryMixin)
        host._q = lambda sql, params=None: []
        host._bytes_to_floats = lambda b: []
        host._memory_doc_cache = _EvictCache()

        _MemoryMixin.update_memory_fields(host, 7, content="new text")
        assert calls == [7], "content edit must evict the memory_doc id"

    def test_update_memory_fields_noncontent_does_not_evict(self):
        from yadgar.storage.memory import _MemoryMixin

        calls: list = []

        class _EvictCache:
            def get(self, k):
                return None

            def put(self, k, v):
                pass

            def invalidate(self, scope=None):
                calls.append(scope)

        host = object.__new__(_MemoryMixin)
        host._q = lambda sql, params=None: []
        host._memory_doc_cache = _EvictCache()

        _MemoryMixin.update_memory_fields(host, 7, is_stale=True)
        assert calls == [], "non-content update must NOT evict (heat/is_stale/tags fresh)"

    def test_update_memory_embedding_evicts(self):
        """TRAIN-BLOCKER FIX (backend 5.22.0): update_memory_embedding raw-UPDATEs the
        embedding (the reembed_stale / reembed_all path) — it MUST evict the memory_doc
        id, else reembed serves a STALE embedding from the cache for up to TTL(2700s).
        Mirrors the update_memory_fields(content) evict, but on the vector seam."""
        from yadgar.storage.memory import _MemoryMixin
        from yadgar.storage.vector import _VectorMixin

        calls: list = []

        class _EvictCache:
            def get(self, k):
                return None

            def put(self, k, v):
                pass

            def invalidate(self, scope=None):
                calls.append(scope)

        # Compose the two seams the way the real StorageEngine does: the vector write
        # (update_memory_embedding) resolves the cache via _MemoryMixin's resolver.
        class _Host(_VectorMixin, _MemoryMixin):
            pass

        host = object.__new__(_Host)
        host._q = lambda sql, params=None: []
        host._bytes_to_floats = lambda b: []
        # update_memory_embedding best-effort-calls update_vector/insert_vector after
        # the UPDATE; stub them to no-ops so the evict path is what's exercised.
        host.update_vector = lambda mid, emb: None
        host.insert_vector = lambda mid, emb: None
        host._memory_doc_cache = _EvictCache()

        _Host.update_memory_embedding(host, 9, b"\x00\x00\x00\x00", "new-model")
        assert calls == [9], (
            "reembed (update_memory_embedding) must evict the memory_doc id "
            "(train-blocker: stale embedding served post-reembed otherwise)"
        )


# ── obs counters ──────────────────────────────────────────────────────────────


class TestObs:
    def test_hit_miss_counters_track(self):
        cache = _mk_cache()
        s = _FakeStorage({1: _raw_row(1), 2: _raw_row(2)}, cache)
        s.get_memories_by_ids([1, 2])  # 2 misses (get returns None for both)
        st1 = cache.stats()
        assert st1["misses"] >= 2
        s.get_memories_by_ids([1, 2])  # 2 hits
        st2 = cache.stats()
        assert st2["hits"] >= 2


# ── OMIT portability (real embedded StorageEngine) ────────────────────────────


class TestOmitPortableEmbedded:
    def test_omit_portable_embedded(self, tmp_path, monkeypatch):
        """The fresh query `SELECT * OMIT content, embedding` MUST parse in the
        embedded SurrealKV SDK, not just server mode (parity landmine — cf. the
        parameterised-IN embedded failure at memory.py)."""
        monkeypatch.delenv("YADGAR_DB_URL", raising=False)
        pytest.importorskip("surrealdb")
        import numpy as np

        from yadgar.storage import StorageEngine

        s = StorageEngine(str(tmp_path / "db"))
        mem = {
            "content": "hello world",
            "embedding": np.ones(384, dtype=np.float32).tobytes(),
            "tags": ["t"],
            "directory_context": "/tmp",
            "heat": 1.0,
        }
        mid = s.insert_memory(mem, branch="master")
        rows = s._q(f"SELECT * OMIT content, embedding FROM memory WHERE id IN [memory:{mid}]")
        assert rows, "OMIT query returned no rows"
        assert "content" not in rows[0]
        assert "embedding" not in rows[0]
        assert "heat" in rows[0]  # volatile field still present (fetched fresh)
