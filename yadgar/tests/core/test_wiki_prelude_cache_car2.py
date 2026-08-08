"""Car 2 — wiki_read / wiki_query / agent_dispatch_prelude caches (v5.113).

Three query/slug-scoped read caches whose invalidation folds the GLOBAL structural
wiki epoch (_current_epoch(None)) into the key. Every wiki write funnels through
storage.insert/update/delete_wiki_page or set_wiki_page_metadata, each of which
calls _bump_wiki_epoch() → bump_epoch(None), advancing the global generation so a
stale key becomes unreachable. This is the wiki-write-busts-read guarantee.

MODEL-FREE: no init_engines / no torch. The WikiStore + storage are faked; the
epoch bus is the real one (yadgar.server.tools._recall_shadow). Tests exercise a
real write TOOL → real read TOOL round-trip through the real epoch hook, NOT a
hand-called invalidate().
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_epoch_and_caches():
    """Isolate each test: reset the epoch bus + clear all three Car-2 caches."""
    from yadgar._shared.runtime import cache_epoch as _recall_shadow
    from yadgar.core.server.tools import dispatch_helper as dh
    from yadgar.core.server.tools import wiki as wtool

    _recall_shadow._reset_for_test()
    for cache in (wtool._wiki_read_cache, wtool._wiki_query_cache, dh._prompt_cache):
        cache.clear()
        cache.hits = cache.misses = cache.evictions = 0
    yield
    _recall_shadow._reset_for_test()
    # Do NOT pop from _REGISTRY: the caches are module singletons created at import
    # and reused across tests; popping would break a later re-import.


# ── The correctness item: a wiki write busts a cached wiki read ──────────────


class _FakeWikiStore:
    """Minimal WikiStore stand-in: read returns a page dict; delete routes through
    the REAL storage.delete_wiki_page epoch hook so the bust is genuine."""

    def __init__(self, storage):
        self._storage = storage
        self.pages = {}  # slug -> page dict

    def read_by_directory(self, slug, caller_dir):
        return dict(self.pages[slug]) if slug in self.pages else None

    def delete(self, slug):
        if slug not in self.pages:
            return False
        del self.pages[slug]
        # Route through the real storage method so its _bump_wiki_epoch fires
        # exactly as production does (page_id irrelevant to the fake).
        self._storage.delete_wiki_page(page_id=1)
        return True


class _FakeStorage:
    """Only implements the wiki mutation methods, each calling the REAL bump hook."""

    def __init__(self):
        from yadgar._shared.storage.wiki import _WikiMixin

        self._bump = _WikiMixin._bump_wiki_epoch.__get__(self)

    def delete_wiki_page(self, page_id):
        self._bump()
        return True

    def update_wiki_page(self, page_id, updates):
        self._bump()
        return True

    def set_wiki_page_metadata(self, page_id, field, value):
        self._bump()
        return True

    def insert_wiki_page(self, page, branch=None):
        self._bump()
        return 1


def _wire_fake_wiki(monkeypatch):
    import yadgar._shared.runtime.state as _state
    from yadgar.core.server.tools import wiki as wtool

    storage = _FakeStorage()
    fake = _FakeWikiStore(storage)
    monkeypatch.setattr(_state, "_wiki", fake)
    return fake, storage, wtool


def test_wiki_delete_busts_wiki_read_cache(monkeypatch, admin_backend_bypass):
    """THE STOP-CLAUSE ITEM: a wiki write (delete) must bust a cached wiki_read.

    Seed a page → wiki_read (populates cache) → wiki_delete (real epoch bump via
    storage.delete_wiki_page) → wiki_read again → must NOT serve the stale page;
    returns the not-found error because the epoch moved and the store no longer
    has it. A stale cache would wrongly return the old content."""
    fake, _storage, wtool = _wire_fake_wiki(monkeypatch)
    fake.pages["mypage"] = {"slug": "mypage", "title": "T", "content": "v1"}

    first = wtool.wiki_read("mypage", directory="/repo")
    assert first["content"] == "v1"  # miss → computed + cached

    # A second identical read is served from cache (no epoch change yet).
    assert wtool._wiki_read_cache.stats()["hits"] == 0
    _ = wtool.wiki_read("mypage", directory="/repo")
    assert wtool._wiki_read_cache.stats()["hits"] == 1, "identical read should hit cache"

    # Now DELETE via the tool — real storage hook bumps the global epoch.
    del_res = wtool.wiki_delete("mypage")
    assert del_res["deleted"] is True

    # The cached entry is now unreachable (epoch moved). Read must reflect deletion.
    after = wtool.wiki_read("mypage", directory="/repo")
    assert "error" in after, (
        "wiki_read served a STALE cached page after wiki_delete — the wiki-write-"
        "busts-read invalidation is broken"
    )


def test_wiki_update_busts_wiki_read_cache(monkeypatch):
    """A content UPDATE (via storage.update_wiki_page) busts the cached read so the
    next read sees fresh content, not the stale cached version."""
    fake, storage, wtool = _wire_fake_wiki(monkeypatch)
    fake.pages["p"] = {"slug": "p", "title": "T", "content": "old"}

    assert wtool.wiki_read("p", directory="/repo")["content"] == "old"  # cached

    # Simulate an in-place content edit + the real epoch bump the edit path fires.
    fake.pages["p"]["content"] = "new"
    storage.update_wiki_page(page_id=1, updates={"content": "new"})

    assert wtool.wiki_read("p", directory="/repo")["content"] == "new", (
        "wiki_read served stale content after an update-path epoch bump"
    )


def test_wiki_set_metadata_busts_wiki_read_cache(monkeypatch):
    """A metadata change routes through storage.set_wiki_page_metadata (NOT
    update_wiki_page) — verify that funnel also bumps + busts the read cache."""
    fake, storage, wtool = _wire_fake_wiki(monkeypatch)
    fake.pages["p"] = {"slug": "p", "title": "T", "content": "c", "category": "old"}

    assert wtool.wiki_read("p", directory="/repo")["category"] == "old"  # cached

    fake.pages["p"]["category"] = "new"
    storage.set_wiki_page_metadata(page_id=1, field="category", value="new")

    assert wtool.wiki_read("p", directory="/repo")["category"] == "new", (
        "metadata-path write did not bust the wiki_read cache"
    )


# ── wiki_read cache: hit/miss + deep-copy isolation ──────────────────────────


def test_wiki_read_hit_skips_store(monkeypatch):
    fake, _storage, wtool = _wire_fake_wiki(monkeypatch)
    calls = []
    orig = fake.read_by_directory

    def counting_read(slug, caller_dir):
        calls.append(slug)
        return orig(slug, caller_dir)

    monkeypatch.setattr(fake, "read_by_directory", counting_read)
    fake.pages["p"] = {"slug": "p", "title": "T", "content": "c"}

    wtool.wiki_read("p", directory="/repo")
    wtool.wiki_read("p", directory="/repo")
    assert len(calls) == 1, "second identical read must be served from cache"


def test_wiki_read_deep_copy_isolation(monkeypatch):
    """Mutating the returned page dict must not corrupt the cached value."""
    fake, _storage, wtool = _wire_fake_wiki(monkeypatch)
    fake.pages["p"] = {"slug": "p", "title": "T", "content": "c"}

    first = wtool.wiki_read("p", directory="/repo")
    first["content"] = "MUTATED"
    first["injected"] = True

    second = wtool.wiki_read("p", directory="/repo")  # cache hit
    assert second["content"] == "c"
    assert "injected" not in second


# ── wiki_query cache: hit/miss + deep-copy + epoch bust ──────────────────────


def _wire_fake_query(monkeypatch, results):
    import yadgar._shared.runtime.state as _state
    from yadgar.core.server.tools import wiki as wtool

    class _QStore:
        def __init__(self):
            self.calls = 0

        def query(self, query, tags, category, k):
            self.calls += 1
            return [dict(r) for r in results]

    store = _QStore()
    monkeypatch.setattr(_state, "_wiki", store)
    # is_directory_eligible: keep all rows.
    monkeypatch.setattr(wtool, "is_directory_eligible", lambda dc, d: True)
    return store, wtool


def test_wiki_query_hit_skips_store(monkeypatch):
    store, wtool = _wire_fake_query(
        monkeypatch, [{"slug": "a", "branch": None, "_retrieval_score": 0.5}]
    )
    wtool.wiki_query("q", directory="/repo")
    wtool.wiki_query("q", directory="/repo")
    assert store.calls == 1, "identical wiki_query must hit the cache (1 store call)"


def test_wiki_query_deep_copy_isolation(monkeypatch):
    store, wtool = _wire_fake_query(
        monkeypatch, [{"slug": "a", "branch": None, "_retrieval_score": 0.5}]
    )
    first = wtool.wiki_query("q", directory="/repo")
    first[0]["_retrieval_score"] = 999
    first.append({"slug": "injected"})
    second = wtool.wiki_query("q", directory="/repo")  # hit
    assert store.calls == 1
    assert second[0]["_retrieval_score"] == 0.5
    assert len(second) == 1


def test_wiki_query_epoch_bump_busts(monkeypatch):
    from yadgar._shared.runtime import cache_epoch as _recall_shadow

    store, wtool = _wire_fake_query(
        monkeypatch, [{"slug": "a", "branch": None, "_retrieval_score": 0.5}]
    )
    wtool.wiki_query("q", directory="/repo")  # miss → compute
    _recall_shadow.bump_epoch(None)  # a wiki write
    wtool.wiki_query("q", directory="/repo")  # epoch moved → recompute
    assert store.calls == 2, "wiki write epoch bump must bust the wiki_query cache"


def test_wiki_query_distinct_keys(monkeypatch):
    store, wtool = _wire_fake_query(
        monkeypatch, [{"slug": "a", "branch": None, "_retrieval_score": 0.5}]
    )
    wtool.wiki_query("q1", directory="/repo")
    wtool.wiki_query("q2", directory="/repo")
    wtool.wiki_query("q1", directory="/repo", category="c")
    assert store.calls == 3, "query text and category are distinct keys"


# ── agent_dispatch_prelude cache: pattern-static lookup + save busts ─────────


def _wire_fake_prompt(monkeypatch):
    from yadgar.core.server.tools import agent_prompts
    from yadgar.core.server.tools import dispatch_helper as dh

    calls = []

    def fake_read(slug, storage=None):
        calls.append(slug)
        return {"version": 1, "slug": slug, "content": f"PROMPT for {slug}", "tags": []}

    monkeypatch.setattr(agent_prompts, "_read_agent_prompt", fake_read)
    return calls, dh


def test_prelude_prompt_lookup_cached(monkeypatch):
    calls, dh = _wire_fake_prompt(monkeypatch)
    dh._cached_agent_prompt("fix-bug", storage=object())
    dh._cached_agent_prompt("fix-bug", storage=object())
    assert len(calls) == 1, "second lookup for same pattern must hit the cache"


def test_prelude_distinct_pattern_keys(monkeypatch):
    calls, dh = _wire_fake_prompt(monkeypatch)
    dh._cached_agent_prompt("a", storage=object())
    dh._cached_agent_prompt("b", storage=object())
    assert len(calls) == 2, "distinct patterns are distinct keys"


def test_agent_prompt_save_epoch_bump_busts_prelude(monkeypatch):
    """agent_prompt_save is a wiki write → it bumps the global wiki epoch (via
    storage insert/update_wiki_page). Simulate that bump and prove the cached
    prompt lookup for the same pattern is busted (fresh content re-read)."""
    from yadgar._shared.runtime import cache_epoch as _recall_shadow

    calls, dh = _wire_fake_prompt(monkeypatch)
    dh._cached_agent_prompt("p", storage=object())  # miss → read
    _recall_shadow.bump_epoch(None)  # agent_prompt_save's wiki write
    dh._cached_agent_prompt("p", storage=object())  # epoch moved → re-read
    assert len(calls) == 2, "agent_prompt_save epoch bump must bust the prelude cache"


def test_prelude_deep_copy_isolation(monkeypatch):
    calls, dh = _wire_fake_prompt(monkeypatch)
    first = dh._cached_agent_prompt("p", storage=object())
    first["content"] = "MUTATED"
    first["injected"] = True
    second = dh._cached_agent_prompt("p", storage=object())  # hit
    assert len(calls) == 1
    assert second["content"] == "PROMPT for agent-prompt-p"
    assert "injected" not in second


# ── the storage hook itself calls the global bump ────────────────────────────


def test_bump_wiki_epoch_advances_global_gen(monkeypatch):
    """Unit: _bump_wiki_epoch bumps the GLOBAL generation (not a per-dir key), so it
    busts every dir's key regardless of read/write directory normalization."""
    from yadgar._shared.runtime import cache_epoch as _recall_shadow
    from yadgar._shared.storage.wiki import _WikiMixin

    e_a = _recall_shadow._current_epoch("/dir/a")
    e_b = _recall_shadow._current_epoch("/dir/b")

    bump = _WikiMixin._bump_wiki_epoch.__get__(object())
    bump()

    assert _recall_shadow._current_epoch("/dir/a") == e_a + 1
    assert _recall_shadow._current_epoch("/dir/b") == e_b + 1, (
        "a wiki write must bust EVERY directory's epoch (global bump, normalization-proof)"
    )


# ── WIRING PROOF: the REAL storage funnel bodies call _bump_wiki_epoch ────────
#
# The tests above prove the MECHANISM (epoch bump → cache busts). These prove the
# WIRING: each of the four real storage mutation methods executes _bump_wiki_epoch
# on success. Removing any `self._bump_wiki_epoch()` line from storage/wiki.py MUST
# fail one of these. They run the ACTUAL method bodies with the DB primitive (_q)
# and a handful of pre-txn helpers stubbed — no SurrealDB, no torch, no conftest.


class _RealFunnelStorage:
    """Runs the real _WikiMixin mutation bodies against stubbed DB primitives.

    Only the lowest-level side-effecting primitives are stubbed (_q + the pre-txn
    read/id/time helpers); the mutation-method bodies — including their
    self._bump_wiki_epoch() call — are the REAL code under test."""

    def _q(self, q, params=None):
        # delete_wiki_page SELECTs the slug before deleting; everything else writes.
        if q.strip().upper().startswith("SELECT"):
            return [{"id": 1, "slug": "p"}]
        return []

    def _now_iso(self):
        return "2026-07-05T00:00:00+00:00"

    def _next_id(self, table):
        return 1

    def get_max_version_for_page(self, page_id):
        return 1

    def get_wiki_page(self, page_id):
        return {"id": page_id, "slug": "p", "title": "T", "content": "old", "category": "c"}

    def _bytes_to_floats(self, b):
        return b


def _make_real_funnel():
    """Bind the real _WikiMixin methods onto a stubbed-primitive instance."""
    from yadgar._shared.storage.wiki import _WikiMixin

    inst = _RealFunnelStorage()
    # Attach the mixin's methods (unbound) so the REAL bodies run on inst.
    inst.insert_wiki_page = _WikiMixin.insert_wiki_page.__get__(inst)
    inst.update_wiki_page = _WikiMixin.update_wiki_page.__get__(inst)
    inst.set_wiki_page_metadata = _WikiMixin.set_wiki_page_metadata.__get__(inst)
    inst.delete_wiki_page = _WikiMixin.delete_wiki_page.__get__(inst)
    inst._bump_wiki_epoch = _WikiMixin._bump_wiki_epoch.__get__(inst)
    return inst


def test_real_delete_wiki_page_bumps_epoch():
    from yadgar._shared.runtime import cache_epoch as _recall_shadow

    s = _make_real_funnel()
    before = _recall_shadow._current_epoch(None)
    assert s.delete_wiki_page(1) is True
    assert _recall_shadow._current_epoch(None) == before + 1, (
        "storage.delete_wiki_page must call _bump_wiki_epoch (else stale wiki_read)"
    )


def test_real_update_wiki_page_bumps_epoch():
    from yadgar._shared.runtime import cache_epoch as _recall_shadow

    s = _make_real_funnel()
    before = _recall_shadow._current_epoch(None)
    assert s.update_wiki_page(1, {"content": "new"}) is True
    assert _recall_shadow._current_epoch(None) == before + 1, (
        "storage.update_wiki_page must call _bump_wiki_epoch (else stale wiki_read)"
    )


def test_real_set_wiki_page_metadata_bumps_epoch():
    from yadgar._shared.runtime import cache_epoch as _recall_shadow

    s = _make_real_funnel()
    before = _recall_shadow._current_epoch(None)
    assert s.set_wiki_page_metadata(1, "category", "new") is True
    assert _recall_shadow._current_epoch(None) == before + 1, (
        "storage.set_wiki_page_metadata must call _bump_wiki_epoch (metadata funnel)"
    )


def test_real_insert_wiki_page_bumps_epoch():
    from yadgar._shared.runtime import cache_epoch as _recall_shadow

    s = _make_real_funnel()
    before = _recall_shadow._current_epoch(None)
    assert s.insert_wiki_page({"slug": "p", "title": "T", "content": "c"}) == 1
    assert _recall_shadow._current_epoch(None) == before + 1, (
        "storage.insert_wiki_page must call _bump_wiki_epoch"
    )


# ── obs metrics: cold-tier caches emit record_cache_hit/miss ─────────────────


def test_wiki_caches_emit_cold_tier_metrics(monkeypatch):
    """Car 2 caches are obs_tier='cold' → get() calls record_cache_hit/miss inline.
    (The generic emission path itself is covered by the Car 1 Cache-class tests; this
    asserts the wiki_read instance is wired to it.)"""
    import yadgar.core.cache.cache as cache_mod

    hits, misses = [], []
    monkeypatch.setattr(cache_mod, "record_cache_hit", lambda name: hits.append(name))
    monkeypatch.setattr(cache_mod, "record_cache_miss", lambda name: misses.append(name))

    fake, _storage, wtool = _wire_fake_wiki(monkeypatch)
    fake.pages["p"] = {"slug": "p", "title": "T", "content": "c"}

    wtool.wiki_read("p", directory="/repo")  # miss
    wtool.wiki_read("p", directory="/repo")  # hit
    assert "wiki_read" in misses
    assert "wiki_read" in hits
