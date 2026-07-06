"""Wire the recall-path data caches into the backend /metrics export.

ROOT (proven with live metrics 2026-07-06, backend 5.22.0):
  The three recall-path data caches — ``memory_doc`` (fusion build_results),
  ``engram_slot`` (engram-links rerank), ``graph`` (spreading/PPR adjacency) —
  are FULLY WIRED onto the forward-only backend recall pipeline (their seams
  ``get_memories_by_ids`` / ``get_memories_in_slot`` / ``_get_adjacent_batch``
  all resolve the registered ``Cache`` via ``_resolve_*_cache`` and fire
  get/put). They are ``obs_tier="cold"`` so each op calls
  ``yadgar.metrics.record_cache_hit/miss`` — which lands in the *core-scraped*
  ``yadgar.metrics._registry``. The backend ``/metrics`` endpoint (:8001) serves
  a DIFFERENT isolated registry (``embed_service_metrics._registry``) whose
  ``CacheStatsCollector`` HARD-CODED only ``{ce, embed}``. Result: the three
  data caches fire on every backend recall but are INVISIBLE at :8001.

This is a metrics-VISIBILITY gap, NOT a caching gap. The fix makes
``CacheStatsCollector`` enumerate the whole backend ``_REGISTRY`` so every
registered ``Cache`` (ce, embed, memory_doc, engram_slot, graph) surfaces the
generic ``yadgar_cache_{hit,miss,evictions}_total{cache=<name>}`` +
``_size_entries`` series at backend ``/metrics``.

Test layers:
  1. FIRING (anti-vacuity) — each seam, driven through its REAL method with a
     spy ``Cache`` injected, invokes get AND put (the caches DO participate on
     the recall-reachable path; not dead code).
  2. EXPORT (the fix) — once memory_doc/engram_slot/graph are registered,
     ``CacheStatsCollector.collect()`` yields their label series (RED before the
     fix: collector only emitted ce/embed).
  3. QUALITY-NEUTRAL — a spy ``Cache`` vs a ``NullCache`` produce byte-identical
     seam output (caching changes nothing but latency).
  4. COLLISION-SAFE — the collector still emits the generic names ONLY (never
     the bespoke ``yadgar_embed_*`` names) → no duplicate ``# TYPE`` at scrape.
"""

from __future__ import annotations

import struct

from yadgar.backend.cache import (
    Cache,
    CacheProtocol,
    NullCache,
    ScopeVersions,
)

# ─────────────────────────────────────────────────────────────────────────────
# Spy cache — a real Cache that records every get/put key (proves invocation).
# ─────────────────────────────────────────────────────────────────────────────


class _SpyCache:
    """Wrap a real ``Cache`` and record every get/put call.

    Delegates to a real ``Cache`` (so hit/miss/eviction accounting + byte budget
    behave exactly as prod), while capturing the call sequence so the test can
    assert the seam actually touched the cache.
    """

    def __init__(self, inner: CacheProtocol) -> None:
        self._inner = inner
        self.get_keys: list = []
        self.put_keys: list = []

    def get(self, key):  # noqa: ANN001
        self.get_keys.append(key)
        return self._inner.get(key)

    def put(self, key, value):  # noqa: ANN001
        self.put_keys.append(key)
        self._inner.put(key, value)

    def invalidate(self, key) -> None:  # noqa: ANN001
        self._inner.invalidate(key)

    def stats(self) -> dict:
        return self._inner.stats()


def _real_cache(name: str) -> Cache:
    """A small real byte-bounded Cache (not the shared registered instance)."""
    from yadgar.backend.cache import DataEpoch

    return Cache(
        name=f"test-{name}",
        max_bytes=1 << 20,
        invalidation=DataEpoch(),
        deep_copy=True,
        obs_tier="cold",
    )


def _emb_bytes(seed: int) -> bytes:
    return struct.pack("<4f", *[seed + 0.1 * i for i in range(4)])


# ─────────────────────────────────────────────────────────────────────────────
# Layer 1 — FIRING: each seam invokes get + put on the injected cache.
# ─────────────────────────────────────────────────────────────────────────────


class _MemoryDocHost:
    """Minimal StorageEngine host exercising get_memories_by_ids with a spy cache."""

    from yadgar.storage.client import _ClientMixin
    from yadgar.storage.memory import _MemoryMixin

    get_memories_by_ids = _MemoryMixin.get_memories_by_ids
    _resolve_memory_doc_cache = _MemoryMixin._resolve_memory_doc_cache
    _extract_id = _ClientMixin._extract_id

    def __init__(self, rows: dict[int, dict], cache: CacheProtocol) -> None:
        self._rows = rows
        self._memory_doc_cache = cache

    def _rows_to_dicts(self, rows: list[dict]) -> list[dict]:
        out = []
        for r in rows:
            d = dict(r)
            for k in (
                "embedding_model",
                "file_hash",
                "last_excitability_update",
                "original_content",
                "last_reconsolidated",
            ):
                d.setdefault(k, None)
            out.append(d)
        return out

    def _q(self, surql: str, params: dict | None = None) -> list:
        # get_memories_by_ids issues two shapes: OMIT-fresh and heavy content/embedding.
        import re

        ids = [int(m) for m in re.findall(r"memory:(\d+)", surql)]
        rows = [self._rows[i] for i in ids if i in self._rows]
        if "OMIT content, embedding" in surql:
            return [{k: v for k, v in r.items() if k not in ("content", "embedding")} for r in rows]
        # heavy fetch: id + content + embedding
        return [{"id": r["id"], "content": r["content"], "embedding": r["embedding"]} for r in rows]


def _memory_rows() -> dict[int, dict]:
    return {
        1: {"id": 1, "content": "alpha", "embedding": _emb_bytes(1), "heat": 0.9},
        2: {"id": 2, "content": "beta", "embedding": _emb_bytes(2), "heat": 0.4},
    }


def test_memory_doc_seam_fires_get_and_put():
    """get_memories_by_ids MUST get() then put() on the memory_doc cache (cold miss)."""
    spy = _SpyCache(_real_cache("memory_doc"))
    host = _MemoryDocHost(_memory_rows(), spy)

    host.get_memories_by_ids([1, 2])

    assert spy.get_keys, "memory_doc seam never called cache.get()"
    assert spy.put_keys, "memory_doc seam never called cache.put() on a cold miss"


def test_memory_doc_quality_neutral_spy_vs_null():
    """Spy-cache and NullCache produce byte-identical hydrated rows."""
    rows = _memory_rows()
    out_spy = _MemoryDocHost(dict(rows), _SpyCache(_real_cache("memory_doc"))).get_memories_by_ids(
        [1, 2]
    )
    out_null = _MemoryDocHost(dict(rows), NullCache()).get_memories_by_ids([1, 2])
    key = lambda ds: sorted(ds, key=lambda d: d["id"])  # noqa: E731
    assert key(out_spy) == key(out_null)


class _GraphHost:
    """Minimal host exercising _get_adjacent_batch with a spy graph cache."""

    from yadgar.knowledge_graph import KnowledgeGraph
    from yadgar.storage.ops import _OpsMixin

    _get_adjacent_batch = KnowledgeGraph._get_adjacent_batch
    _resolve_graph_cache = _OpsMixin._resolve_graph_cache
    _resolve_scope_versions = _OpsMixin._resolve_scope_versions

    def __init__(self, rels: list[dict], cache: CacheProtocol) -> None:
        self._rels = rels
        self._graph_cache = cache
        self._scope_versions = ScopeVersions()
        self.frontier_queries = 0

    @property
    def _storage(self):
        return self

    def get_relationships_for_frontier(
        self, entity_ids: list[int], rel_types: list[str] | None = None
    ) -> list[dict]:
        self.frontier_queries += 1
        idset = set(entity_ids)
        return [
            dict(r)
            for r in self._rels
            if r["source_entity_id"] in idset or r["target_entity_id"] in idset
        ]


def _rels() -> list[dict]:
    return [
        {"source_entity_id": 1, "target_entity_id": 2, "relationship_type": "co", "weight": 1.0},
        {"source_entity_id": 2, "target_entity_id": 3, "relationship_type": "co", "weight": 1.0},
    ]


def test_graph_seam_fires_get_and_put():
    """_get_adjacent_batch MUST get() every entity, then put() the miss subset."""
    spy = _SpyCache(_real_cache("graph"))
    host = _GraphHost(_rels(), spy)

    host._get_adjacent_batch([1, 2, 3], None)

    assert spy.get_keys, "graph seam never called cache.get()"
    assert spy.put_keys, "graph seam never called cache.put() on a cold frontier"


def test_graph_quality_neutral_spy_vs_null():
    """Spy-cache and NullCache produce identical adjacency."""
    adj_spy = _GraphHost(_rels(), _SpyCache(_real_cache("graph")))._get_adjacent_batch(
        [1, 2, 3], None
    )
    adj_null = _GraphHost(_rels(), NullCache())._get_adjacent_batch([1, 2, 3], None)
    assert adj_spy == adj_null


# ─────────────────────────────────────────────────────────────────────────────
# Layer 2 — EXPORT (the fix): CacheStatsCollector emits the 3 data caches.
# ─────────────────────────────────────────────────────────────────────────────


def _collector_series() -> dict[str, dict[str, float]]:
    """Run CacheStatsCollector.collect() and return {metric_name: {cache: value}}.

    Keyed by the SAMPLE name (``yadgar_cache_hit_total``), not the family name —
    prometheus_client strips the ``_total`` suffix from ``CounterMetricFamily.name``
    but the emitted sample keeps it.
    """
    from yadgar.backend.embed_service_metrics import CacheStatsCollector

    out: dict[str, dict[str, float]] = {}
    for fam in CacheStatsCollector().collect():
        for sample in fam.samples:
            out.setdefault(sample.name, {})[sample.labels["cache"]] = sample.value
    return out


def test_collector_emits_all_registered_data_caches():
    """After the fix, collect() surfaces memory_doc/engram_slot/graph, not just ce/embed.

    RED before the fix: _default_backend_cache_instances() hard-coded {ce, embed}
    so the three data caches never appeared regardless of registration.
    """
    from yadgar.backend.cache import (
        get_engram_slot_cache,
        get_graph_cache,
        get_memory_doc_cache,
    )

    # Ensure the three data caches are registered (idempotent factories).
    get_memory_doc_cache()
    get_engram_slot_cache()
    get_graph_cache()

    series = _collector_series()
    hit = series.get("yadgar_cache_hit_total", {})
    size = series.get("yadgar_cache_size_entries", {})

    for name in ("memory_doc", "engram_slot", "graph"):
        assert name in hit, f"collector did not emit hit series for cache={name!r}"
        assert name in size, f"collector did not emit size series for cache={name!r}"


def test_collector_still_emits_ce_and_embed():
    """The fix must NOT drop the pre-existing ce/embed generic series."""
    # Importing embed_service registers ce/embed at module import.
    import yadgar.backend.embed_service  # noqa: F401

    series = _collector_series()
    hit = series.get("yadgar_cache_hit_total", {})
    assert "ce" in hit, "ce generic series regressed"
    assert "embed" in hit, "embed generic series regressed"


def test_collector_emits_only_generic_names_no_bespoke_collision():
    """Collector must emit ONLY yadgar_cache_* (never the bespoke yadgar_embed_* names).

    Re-yielding a statically-declared counter in the same process = duplicate
    # TYPE at scrape → Prometheus rejects the whole scrape (the line-284 guard).
    """
    from yadgar.backend.embed_service_metrics import CacheStatsCollector

    # CounterMetricFamily.name has the _total suffix stripped by prometheus_client.
    names = {fam.name for fam in CacheStatsCollector().collect()}
    assert names == {
        "yadgar_cache_hit",
        "yadgar_cache_miss",
        "yadgar_cache_evictions",
        "yadgar_cache_size_entries",
    }, f"collector emitted unexpected metric families: {names}"
    assert not any(n.startswith("yadgar_embed_") for n in names), (
        "collector must never re-yield the bespoke yadgar_embed_* counters"
    )


def test_generated_metrics_output_includes_data_caches():
    """End-to-end: generate_latest over the backend registry contains the 3 caches.

    Mirrors what a Prometheus scrape of backend :8001/metrics would see, so this
    is the DEPLOYED-path assertion for the export fix.
    """
    from prometheus_client import generate_latest

    from yadgar.backend.cache import (
        get_engram_slot_cache,
        get_graph_cache,
        get_memory_doc_cache,
    )
    from yadgar.backend.embed_service_metrics import _registry

    get_memory_doc_cache()
    get_engram_slot_cache()
    get_graph_cache()

    text = generate_latest(_registry).decode()
    for name in ("memory_doc", "engram_slot", "graph"):
        assert f'cache="{name}"' in text, (
            f"backend /metrics scrape missing cache={name!r} — export gap not closed"
        )
