"""Car 0 — cache observability unification (v5.109 / backend 5.14).

Behavior-neutral: this car ONLY adds observability. It introduces the shared
`record_cache_hit/miss/evict` helper on the generic `yadgar_cache_hit_total{cache=}`
family, retrofits the silent caches to call it, and dual-emits the CE/embed
backend caches onto the same generic family via a scrape-time collector.

These tests are MODEL-FREE (no CE/embed model load) — safe under the OOM
constraint. They assert:

1. The shared helper emits the generic family with the right {cache=} label.
2. The eviction + size families exist and move.
3. The backend CacheStatsCollector dual-emits {cache="ce"} / {cache="embed"}
   off the LRUCache internal ints WITHOUT re-yielding the old bespoke names
   (no duplicate `# TYPE` → clean Prometheus scrape parse).
4. The old bespoke CE/embed series stay present (behavior-neutral).
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry
from prometheus_client.parser import text_string_to_metric_families

# ── 1. shared helper emits the generic family ────────────────────────────────


def test_record_cache_hit_emits_generic_family():
    from yadgar._shared.observability import metrics

    before = metrics.yadgar_cache_hit_total.labels(cache="unittest_hit")._value.get()
    metrics.record_cache_hit("unittest_hit")
    after = metrics.yadgar_cache_hit_total.labels(cache="unittest_hit")._value.get()
    assert after == before + 1


def test_record_cache_miss_emits_generic_family():
    from yadgar._shared.observability import metrics

    before = metrics.yadgar_cache_miss_total.labels(cache="unittest_miss")._value.get()
    metrics.record_cache_miss("unittest_miss")
    after = metrics.yadgar_cache_miss_total.labels(cache="unittest_miss")._value.get()
    assert after == before + 1


def test_record_cache_evict_emits_new_evictions_family():
    from yadgar._shared.observability import metrics

    before = metrics.yadgar_cache_evictions_total.labels(cache="unittest_evict")._value.get()
    metrics.record_cache_evict("unittest_evict", 3)
    after = metrics.yadgar_cache_evictions_total.labels(cache="unittest_evict")._value.get()
    assert after == before + 3


def test_cache_size_entries_gauge_exists():
    from yadgar._shared.observability import metrics

    metrics.yadgar_cache_size_entries.labels(cache="unittest_size").set(7)
    assert metrics.yadgar_cache_size_entries.labels(cache="unittest_size")._value.get() == 7


# ── 2. retrofitted silent caches emit ────────────────────────────────────────


def test_remote_embedding_cache_emits_generic_family():
    """remote_embeddings.encode was fully silent; must now emit {cache="remote_embedding"}."""
    from yadgar._shared.embeddings.remote_embeddings import RemoteEmbeddingEngine
    from yadgar._shared.observability import metrics

    eng = RemoteEmbeddingEngine.__new__(RemoteEmbeddingEngine)
    # Minimal manual init to exercise the cache hit path without a live backend.
    import threading
    from collections import OrderedDict

    eng._query_cache = OrderedDict()
    eng._cache_lock = threading.Lock()
    eng._query_cache["hello"] = b"cached-vector"

    before = metrics.yadgar_cache_hit_total.labels(cache="remote_embedding")._value.get()
    out = eng.encode("hello")
    after = metrics.yadgar_cache_hit_total.labels(cache="remote_embedding")._value.get()
    assert out == b"cached-vector"
    assert after == before + 1


def test_rate_limit_cache_emits_hit_miss():
    """TokenBucketRateLimiter.allow: new key = miss, existing key = hit."""
    from yadgar._shared.observability import metrics
    from yadgar._shared.rate_limit import TokenBucketRateLimiter

    rl = TokenBucketRateLimiter(max_per_minute=600)
    miss0 = metrics.yadgar_cache_miss_total.labels(cache="rate_limit")._value.get()
    hit0 = metrics.yadgar_cache_hit_total.labels(cache="rate_limit")._value.get()
    rl.allow("k1")  # first: bucket created → miss
    rl.allow("k1")  # second: bucket exists → hit
    assert metrics.yadgar_cache_miss_total.labels(cache="rate_limit")._value.get() == miss0 + 1
    assert metrics.yadgar_cache_hit_total.labels(cache="rate_limit")._value.get() == hit0 + 1


def test_rules_cache_emits_hit_miss_and_evict():
    """RulesEngine.get_applicable_rules hit/miss + clear-on-mutate evict."""
    from yadgar._shared.observability import metrics
    from yadgar._shared.rules_engine import RulesEngine

    class _FakeStorage:
        def get_rules_for_scope(self, scope):
            return []

        def get_all_active_rules_by_scope(self, scope):
            return []

    eng = RulesEngine.__new__(RulesEngine)
    eng._storage = _FakeStorage()
    eng._applicable_rules_cache = {}

    miss0 = metrics.yadgar_cache_miss_total.labels(cache="rules")._value.get()
    hit0 = metrics.yadgar_cache_hit_total.labels(cache="rules")._value.get()
    evict0 = metrics.yadgar_cache_evictions_total.labels(cache="rules")._value.get()

    eng.get_applicable_rules("/tmp/x")  # miss (compute + store)
    eng.get_applicable_rules("/tmp/x")  # hit (cached)
    assert metrics.yadgar_cache_miss_total.labels(cache="rules")._value.get() == miss0 + 1
    assert metrics.yadgar_cache_hit_total.labels(cache="rules")._value.get() == hit0 + 1

    # Simulate a mutation-flush (the add_rule/delete_rule clear path).
    from yadgar._shared.observability.metrics import record_cache_evict

    record_cache_evict("rules", len(eng._applicable_rules_cache))
    eng._applicable_rules_cache.clear()
    assert metrics.yadgar_cache_evictions_total.labels(cache="rules")._value.get() == evict0 + 1


# ── 3. backend collector: dual-emit, no dup TYPE, reads internal ints ────────


def _scrape_backend_registry(registry: CollectorRegistry) -> str:
    from prometheus_client import generate_latest

    return generate_latest(registry).decode("utf-8")


def test_backend_collector_dual_emits_generic_without_dup_type():
    """The backend CacheStatsCollector must emit ONLY the new generic {cache=}
    series (not re-yield the old bespoke names) so the scrape parses cleanly."""
    from yadgar.backend import embed_service_metrics as esm

    # Drive real hits/misses on the backend LRUCache instances via the collector's
    # data source — use a fresh isolated registry to avoid double-registration.
    reg = CollectorRegistry()

    # Fabricate two LRUCache instances with known int counters.
    from yadgar.backend.cache import LRUCache

    ce = LRUCache(max_entries=4, checkpoint_hash="x")
    emb = LRUCache(max_entries=4, checkpoint_hash="x")
    ce.hits, ce.misses, ce.evictions = 5, 2, 1
    emb.hits, emb.misses, emb.evictions = 9, 4, 0

    collector = esm.CacheStatsCollector(lambda: {"ce": ce, "embed": emb})
    reg.register(collector)

    text = _scrape_backend_registry(reg)

    # (a) Scrape parses cleanly — no duplicate # TYPE lines (the collision guard).
    families = list(text_string_to_metric_families(text))
    names = [f.name for f in families]
    assert len(names) == len(set(names)), f"duplicate metric family in scrape: {names}"

    # (b) The NEW generic series exist with values read from the internal ints.
    samples = {(s.name, s.labels.get("cache")): s.value for f in families for s in f.samples}
    assert samples.get(("yadgar_cache_hit_total", "ce")) == 5
    assert samples.get(("yadgar_cache_miss_total", "ce")) == 2
    assert samples.get(("yadgar_cache_evictions_total", "ce")) == 1
    assert samples.get(("yadgar_cache_hit_total", "embed")) == 9
    assert samples.get(("yadgar_cache_miss_total", "embed")) == 4

    # (c) The collector must NOT emit the old bespoke names (those stay static in
    # the module registry, not via the collector) → no collision.
    assert "yadgar_embed_ce_cache_hits_total" not in names
    assert "yadgar_embed_embed_cache_hits_total" not in names


def test_backend_module_registry_still_has_old_bespoke_names():
    """Behavior-neutral: the OLD bespoke CE/embed counters stay declared + scraped
    from the module registry, untouched by this car."""
    from yadgar.backend import embed_service_metrics as esm

    text = _scrape_backend_registry(esm._registry)
    assert "yadgar_embed_ce_cache_hits_total" in text
    assert "yadgar_embed_embed_cache_hits_total" in text
    # And the new generic family is now ALSO present in the module registry
    # (the collector is registered on esm._registry in the module).
    assert "yadgar_cache_hit_total" in text

    # PRODUCTION-PATH proof (guards the Pass-2 "silently emit nothing" blocker):
    # assert the collector's DEFAULT data source (lazy-import embed_service) actually
    # yields the ce/embed SAMPLES — not just the # TYPE/# HELP family name, which
    # would appear even from a zero-sample yield if the lazy import failed.
    assert 'yadgar_cache_hit_total{cache="ce"}' in text, (
        "silent-nothing: backend collector produced no {cache='ce'} sample — the "
        "lazy embed_service import path failed; collector placement is broken."
    )
    assert 'yadgar_cache_hit_total{cache="embed"}' in text
    assert 'yadgar_cache_miss_total{cache="ce"}' in text
    assert 'yadgar_cache_size_entries{cache="embed"}' in text


def test_hot_cache_getter_stays_span_free_by_construction():
    """Flood-safety (v5.105 guard): the per-passage hot CE/embed cache getter
    (LRUCache.get) must NOT carry a span source — the generic {cache=} series
    come only from the scrape-time collector, never a per-get .labels().inc() or
    per-get span. This asserts the by-construction property: LRUCache.get/put have
    no @observe (they are allowlisted, not decorated), so N gets produce 0 spans."""
    import ast
    import inspect

    from yadgar.backend.cache import LRUCache

    for meth in (LRUCache.get, LRUCache.put):
        src = inspect.getsource(meth)
        tree = ast.parse(src.lstrip())
        fn = tree.body[0]
        deco_names = {
            (
                d.func.id
                if isinstance(d, ast.Call) and isinstance(d.func, ast.Name)
                else getattr(d, "id", None)
            )
            for d in fn.decorator_list
        }
        assert "observe" not in deco_names and "trace_span" not in deco_names, (
            f"LRUCache.{meth.__name__} gained a span source — per-passage hot getter "
            "must stay span-free (v5.105 flood guard)."
        )
        # And no inline record_cache_* / .labels(cache= in the hot getter body.
        assert "record_cache_" not in src, (
            f"LRUCache.{meth.__name__} must not call record_cache_* per get — "
            "the collector reads internal ints at scrape time instead."
        )
