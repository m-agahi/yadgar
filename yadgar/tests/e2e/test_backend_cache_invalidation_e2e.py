"""E2E correctness suite — backend cache-train INVALIDATION (issue #49).

Invalidation is the backend caching train's #1 correctness risk: a stale cache
serves a WRONG recall result. The RAM-% kill-switches protect against low-hit,
NOT against stale-while-on. This suite is that safety net — it drives the REAL
mutation path (real StorageEngine + real SurrealDB) and asserts the observable
read reflects the mutation (never stale).

Scope at this commit (branch tip c5e0f389 = Car 2). The wired namespaces are:
  - ``memory_doc`` (Car 2): {content, embedding} per memory_id, TTL(2700s) backstop
    + per-id ``invalidate(mid)`` on ``update_memory_fields`` content/embedding edit.
  - ``ce`` / ``embed`` (Car 0/1): ModelCkpt-keyed (ckpt_hash in the key) — no
    mutation-driven bust; a model swap mints new keys.
``engram_slot`` (Car 3) and ``graph`` (Car 4) are NOT wired yet — their harness +
scenarios are scaffolded as precondition-guard tests (they assert the namespace
factory is absent, so they PASS now and go RED loudly the moment those cars land,
prompting replacement with the real invalidation scenario).

THE VACUOUS-UNDER-NULLCACHE TRAP (why every positive test asserts cache stats):
A test that only asserts "the read reflects the mutation" passes trivially with
NO cache at all — a ``NullCache`` always misses → always heavy-fetches fresh DB
content. That tests "the DB returns current data", NOT "invalidation works". So
every scenario FIRST proves the cache was populated + engaged (``stats()`` hits /
size for that id), THEN mutates, THEN asserts fresh. Without the populated
precondition the assertion is hollow (#52 no-weakening).

We inject an EXPLICIT real ``Cache`` via ``storage._memory_doc_cache`` rather than
using the process-global: ``_make_memory_doc_cache`` sizes ``max_bytes`` from
RAM-% × cgroup memory, which can compute to ~0 in a test container → all-miss →
the whole suite silently vacuous. The injected instance has a generous budget +
an injectable clock so TTL expiry is deterministic.

MASTER-BASELINE DISCIPLINE (hard-won lesson): the tests in THIS file are
branch-only *by construction* — the Car 2 cache seam does not exist on master, so
they cannot run there and there is nothing to baseline. The master-baseline diff
applies to the PRE-EXISTING e2e suite (test_recall_backend_contract_e2e et al.):
before labelling any e2e failure "pre-existing", run the same e2e on a master
worktree and diff — a branch-only failure is a must-fix. See the runner note in
``scripts/e2e_master_baseline.sh``.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e

YADGAR_DIR = "/home/test/yadgar-project"


@pytest.fixture(autouse=True)
def _restore_injected_globals():
    """Restore process-global state these tests mutate — so nothing leaks into the
    pre-existing e2e suite that runs after this module (serial ``-n0``).

    The tests inject ``storage._memory_doc_cache`` (a fake-clock, fixed-budget
    Cache) via ``_inject_cache`` and set ``admin_other._st._storage``. Under a
    reused module-scoped engine, an un-restored injection would make a later
    pre-existing test read this module's cache/clock — the exact cross-module
    leak the master-baseline discipline exists to catch. This fixture snapshots
    and restores both, per test, guaranteeing zero residue.
    """
    import sys

    from yadgar.server.lifecycle import _get_storage

    storage = _get_storage()
    # Snapshot every injected cache/version seam these tests mutate — memory_doc
    # (Car 2) + engram_slot (Car 3) + graph (Car 4) + the shared ScopeVersions — so
    # none leaks into the pre-existing e2e suite that runs after this module.
    _seams = ("_memory_doc_cache", "_engram_slot_cache", "_graph_cache", "_scope_versions")
    saved = {name: (hasattr(storage, name), getattr(storage, name, None)) for name in _seams}

    admin = sys.modules.get("yadgar.server.tools.admin_other")
    saved_st_storage = getattr(admin._st, "_storage", None) if admin is not None else None

    try:
        yield
    finally:
        # Restore each seam to its pre-test state (None/attr-absent → the read path
        # falls back to the process-global default, i.e. production).
        for name, (had, val) in saved.items():
            if had:
                setattr(storage, name, val)
            elif hasattr(storage, name):
                delattr(storage, name)
        if admin is not None:
            admin._st._storage = saved_st_storage


# ---------------------------------------------------------------------------
# Harness — inject an explicit, observable memory_doc cache into the real engine
# ---------------------------------------------------------------------------


class _Clock:
    """Deterministic monotonic clock for TTL tests (advance() moves it forward)."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, secs: float) -> None:
        self.t += secs


def _make_injected_cache(ttl: float = 2700.0, clock=None):
    """Build a REAL memory_doc Cache with a generous budget + deterministic clock.

    Mirrors ``_make_memory_doc_cache`` (TTL invalidation, deep_copy for the mutable
    row dict) but with an explicit 8 MiB budget so it never computes to ~0 in a
    test container — the vacuous-under-NullCache guard.
    """
    from yadgar.backend.cache import TTL, Cache

    kw = {}
    if clock is not None:
        kw["clock"] = clock
    return Cache(
        name="memory_doc",
        max_bytes=8 << 20,
        invalidation=TTL(ttl),
        deep_copy=True,
        obs_tier="cold",
        **kw,
    )


def _inject_cache(storage, ttl: float = 2700.0, clock=None):
    """Attach a fresh injected cache to the real StorageEngine + return it.

    ``get_memories_by_ids`` resolves the cache via ``_resolve_memory_doc_cache``,
    which returns ``self._memory_doc_cache`` when set — so this instance is the one
    the real read path uses, and its ``.stats()`` reflect real hits/misses.
    """
    cache = _make_injected_cache(ttl=ttl, clock=clock)
    storage._memory_doc_cache = cache
    return cache


def _inject_slot_cache(storage):
    """Attach a fresh explicit ``engram_slot`` Cache + a shared ``ScopeVersions`` to
    the real StorageEngine, and return the cache.

    Car 3 keys the slot membership by ``(slot_index, slot_version)`` on a
    ``ScopeVersions``. The slot WRITE (``assign_memory_slot``) bumps that version and
    the slot READ (``get_memories_in_slot``) reads it — both via
    ``_resolve_scope_versions``, so writes + reads MUST share one instance. We inject
    an explicit generous-budget Cache (never the RAM-%-sized global, which can be ~0
    → all-miss → vacuous) so ``stats()`` reflects real hits and the anti-vacuity
    precondition is meaningful.
    """
    from yadgar.backend.cache import Cache, DataEpoch, ScopeVersions

    cache = Cache(
        name="engram_slot",
        max_bytes=8 << 20,
        invalidation=DataEpoch(),
        deep_copy=True,
        obs_tier="cold",
    )
    storage._engram_slot_cache = cache
    storage._scope_versions = ScopeVersions()
    return cache


def _inject_graph_cache(storage):
    """Attach a fresh explicit ``graph`` Cache + a shared ``ScopeVersions`` to the
    real StorageEngine, and return the cache.

    Car 4 keys per-entity adjacency by ``(entity_id, rel_types_key, entity_version)``.
    EVERY edge mutation (insert / reinforce / delete) bumps BOTH endpoints' entity
    versions via ``_bump_entity_version`` → ``_resolve_scope_versions``, and the graph
    READ (``KnowledgeGraph._get_adjacent_batch``) reads the version via the same seam
    — so the write-bump and the read MUST share one ``ScopeVersions`` instance. Reuse
    the SAME injected instance if a slot cache already installed one (both use the
    single shared version store in production).
    """
    from yadgar.backend.cache import Cache, DataEpoch, ScopeVersions

    cache = Cache(
        name="graph",
        max_bytes=8 << 20,
        invalidation=DataEpoch(),
        deep_copy=True,
        obs_tier="cold",
    )
    storage._graph_cache = cache
    if getattr(storage, "_scope_versions", None) is None:
        storage._scope_versions = ScopeVersions()
    return cache


def _seed(storage, embeddings, content: str, *, heat: float = 1.0) -> int:
    """Insert a real memory row with a real embedding; return its id."""
    emb = embeddings.encode(content)
    return storage.insert_memory(
        {
            "content": content,
            "embedding": emb,
            "directory_context": YADGAR_DIR,
            "tags": [],
            "heat": heat,
        }
    )


def _read(storage, ids):
    """Real cached read path (the exact seam fusion.build_results uses)."""
    return {m["id"]: m for m in storage.get_memories_by_ids(list(ids))}


def _run_fanout_recall(monkeypatch, query: str, directory: str, max_results: int = 20):
    """Drive the REAL recall path (recall → _fanout_recall → fusion.build_results →
    get_memories_by_ids). Used for the through-recall DELETE-inert scenario so the
    cache is exercised via the production entry point, not just the storage seam.
    """
    import sys

    monkeypatch.setattr("yadgar.server._detect_branch", lambda _d: "master")
    monkeypatch.setattr("yadgar.server._get_default_branch", lambda _d: "master")
    _rm = sys.modules.get("yadgar.server.tools.recall")
    if _rm is None:
        import yadgar.server.tools.recall as _rm  # noqa: PLC0415
    return _rm.recall(query=query, directory=directory, max_results=max_results)


# ===========================================================================
# memory_doc — content edit MUST invalidate (per-id evict)
# ===========================================================================


class TestMemoryDocContentEvict:
    """A content edit via the real update_memory_fields path evicts the cached id
    → the next recall reads the NEW content, never the stale cached content."""

    def test_content_edit_reflected_not_stale(self, e2e_engines):
        storage = e2e_engines["storage"]
        embeddings = e2e_engines["embeddings"]
        cache = _inject_cache(storage)

        mid = _seed(storage, embeddings, "original content marker alpha7q")

        # [1] POPULATE + prove the cache is engaged (not vacuous).
        out1 = _read(storage, [mid])
        assert out1[mid]["content"] == "original content marker alpha7q"
        assert cache.stats()["size"] == 1, "cache did not populate — suite would be vacuous"
        # a second identical read MUST be a hit (proves the cache serves this id)
        base_hits = cache.stats()["hits"]
        _read(storage, [mid])
        assert cache.stats()["hits"] == base_hits + 1, "cached id was not served on repeat read"

        # [2] REAL mutation — content edit through the wired evict path.
        from yadgar.server.tools import admin_other

        admin_other._st._storage = storage  # ensure the tool targets this engine
        admin_other.memory_update(mid, {"content": "EDITED content marker beta8w"})

        # per-id evict must have fired → this id is gone from the cache
        assert cache.get(mid) is None, "content edit did not evict the memory_doc entry (STALE)"

        # [3+4] recall again → NEW content, and the evicted id re-misses.
        misses_before = cache.stats()["misses"]
        out2 = _read(storage, [mid])
        assert out2[mid]["content"] == "EDITED content marker beta8w", (
            "cached doc masked a live content edit — STALE recall"
        )
        assert cache.stats()["misses"] == misses_before + 1, (
            "evicted id should re-miss (evict did not actually happen)"
        )

    def test_noncontent_field_edit_keeps_cache_hot(self, e2e_engines):
        """A non-content/embedding edit (e.g. is_protected) must NOT evict — else
        the cache dies under every write like the killed output cache."""
        storage = e2e_engines["storage"]
        embeddings = e2e_engines["embeddings"]
        cache = _inject_cache(storage)

        mid = _seed(storage, embeddings, "protect-me content gamma9z")
        _read(storage, [mid])
        assert cache.get(mid) is not None

        storage.update_memory_fields(mid, is_protected=1)
        assert cache.get(mid) is not None, (
            "a non-content edit evicted memory_doc — over-invalidation kills hit-rate"
        )


# ===========================================================================
# memory_doc — positive HIT path (no mutation → fast identical hit)
# ===========================================================================


class TestMemoryDocPositiveHit:
    def test_no_mutation_second_read_is_hit_and_identical(self, e2e_engines):
        storage = e2e_engines["storage"]
        embeddings = e2e_engines["embeddings"]
        cache = _inject_cache(storage)

        mid = _seed(storage, embeddings, "stable content delta1x")
        out1 = _read(storage, [mid])
        h0 = cache.stats()["hits"]
        out2 = _read(storage, [mid])  # no mutation → hit
        assert cache.stats()["hits"] == h0 + 1, "second read of unchanged id must HIT"
        # identical content + embedding bytes served from cache
        assert out2[mid]["content"] == out1[mid]["content"]
        assert out2[mid]["embedding"] == out1[mid]["embedding"]


# ===========================================================================
# memory_doc — DELETE-inert (deleted memory never re-served)
# ===========================================================================


class TestMemoryDocDeleteInert:
    """delete_memory does NOT evict the memory_doc cache (verified: vector.py has no
    evict, delete_memory has no evict). Safety comes from the read path itself:
    get_memories_by_ids fetches fresh scalars via `SELECT * OMIT content,embedding`;
    a deleted id yields no fresh row → `fresh is None → continue` → the row is
    dropped ENTIRELY, even though a stale content entry still sits in the cache.
    This test proves the drop does the work — not eviction."""

    def test_deleted_id_absent_though_stale_entry_lingers(self, e2e_engines):
        storage = e2e_engines["storage"]
        embeddings = e2e_engines["embeddings"]
        cache = _inject_cache(storage)

        mid = _seed(storage, embeddings, "doomed content epsilon2y")
        _read(storage, [mid])
        assert cache.get(mid) is not None  # populated

        storage.delete_memory(mid)

        # The stale content entry is DELIBERATELY still in the cache (delete is inert
        # w.r.t. the cache) — proving the read-path drop, not eviction, is the guard.
        assert cache.get(mid) is not None, (
            "precondition: delete is expected to be cache-inert at this commit"
        )

        # Yet the deleted id must be ABSENT from the read result.
        out = _read(storage, [mid])
        assert mid not in out, "deleted memory re-served from stale cache — STALE recall"

    def test_deleted_id_absent_through_full_recall(
        self, e2e_engines, monkeypatch, recall_backend_bypass
    ):
        """Same guarantee, but exercised through the REAL recall entry point
        (recall → fusion.build_results → get_memories_by_ids)."""
        storage = e2e_engines["storage"]
        embeddings = e2e_engines["embeddings"]
        _inject_cache(storage)

        token = "recalldel3k"
        keep = _seed(storage, embeddings, f"keeper memory about {token}")
        doomed = _seed(storage, embeddings, f"doomed memory about {token}")

        # warm the recall path (populates memory_doc for candidates)
        _run_fanout_recall(monkeypatch, f"memory about {token}", YADGAR_DIR)

        storage.delete_memory(doomed)

        results = _run_fanout_recall(monkeypatch, f"memory about {token}", YADGAR_DIR)
        ids = {r.get("id") for r in results}
        assert doomed not in ids, "deleted memory surfaced in recall — STALE"
        assert keep in ids, "keeper memory should still be recalled"


# ===========================================================================
# memory_doc — heat freshness (heat is NEVER cached; always fetched fresh)
# ===========================================================================


class TestMemoryDocHeatFreshness:
    """The immutable-whitelist split's load-bearing guarantee: content+embedding are
    cached, but heat / access_count / consolidation-mutated scalars are fetched
    FRESH on every read (`SELECT * OMIT content, embedding`). A real heat change
    between two reads MUST be reflected even while the content is served from cache."""

    def test_real_heat_change_reflected_while_content_cached(self, e2e_engines):
        storage = e2e_engines["storage"]
        embeddings = e2e_engines["embeddings"]
        cache = _inject_cache(storage)

        mid = _seed(storage, embeddings, "heat-tracked content zeta4v", heat=1.0)
        out1 = _read(storage, [mid])
        assert out1[mid]["heat"] == 1.0
        assert cache.get(mid) is not None  # content cached

        # Real DB heat mutation (what an access-bump / decay pass writes).
        storage.update_memory_fields(mid, heat=0.37)

        # content is still served from cache (heat is not a cached field, so this
        # edit did NOT evict) — proving heat rides the FRESH scalar query, not cache.
        assert cache.get(mid) is not None, "heat edit wrongly evicted the content cache"
        out2 = _read(storage, [mid])
        assert out2[mid]["heat"] == 0.37, "cached doc masked a live heat change (STALE heat)"
        # content unchanged + still identical bytes
        assert out2[mid]["content"] == out1[mid]["content"]


# ===========================================================================
# memory_doc — TTL backstop (deterministic clock)
# ===========================================================================


class TestMemoryDocTTLBackstop:
    """The TTL is the correctness backstop for any content/embedding change that
    bypasses the per-id evict (the reembed bug below is exactly that class). After
    TTL, the entry expires and the next read heavy-refetches fresh content."""

    def test_ttl_expiry_refetches_fresh_content(self, e2e_engines):
        storage = e2e_engines["storage"]
        embeddings = e2e_engines["embeddings"]
        clock = _Clock()
        cache = _inject_cache(storage, ttl=1800.0, clock=clock)

        mid = _seed(storage, embeddings, "ttl content eta5t")
        _read(storage, [mid])
        assert cache.get(mid) is not None

        # Mutate content by a raw path that bypasses the evict (simulating the
        # evict-bypassing mutator class) — the cache still holds the OLD content...
        storage._q(
            "UPDATE type::record('memory', $id) SET content = $c",
            {"id": mid, "c": "TTL-refreshed content theta6r"},
        )
        # ...until TTL fires. Advance the clock past the TTL window.
        clock.advance(1801.0)
        out = _read(storage, [mid])
        assert out[mid]["content"] == "TTL-refreshed content theta6r", (
            "TTL did not expire the stale entry → content-change backstop broken"
        )


# ===========================================================================
# memory_doc × REEMBED — TRAIN-BLOCKER BUG, NOW FIXED (backend 5.22.0)
# ===========================================================================


class TestReembedInvalidation:
    """update_memory_embedding is the reembed path (reembed_stale / reembed_all): a
    raw embedding UPDATE that bypasses update_memory_fields' per-id evict. Before the
    fix (backend 5.22.0) it did NOT invalidate the memory_doc cache, so reembed served
    a STALE embedding for up to TTL(2700s). The fix adds
    ``_resolve_memory_doc_cache().invalidate(mid)`` after the UPDATE (vector.py). This
    test was an ``xfail(strict=True)`` asserting the CORRECT contract while the bug was
    live; it now XPASSES → converted to a normal passing test."""

    def test_reembed_embedding_change_reflected_not_stale(self, e2e_engines):
        storage = e2e_engines["storage"]
        embeddings = e2e_engines["embeddings"]
        cache = _inject_cache(storage)

        mid = _seed(storage, embeddings, "reembed content iota7e")
        out1 = _read(storage, [mid])
        old_emb = out1[mid]["embedding"]
        assert cache.get(mid) is not None, "precondition: content cached (not vacuous)"

        # Real reembed path — a DIFFERENT embedding written via update_memory_embedding
        # (the exact call reembed_stale / reembed_all make). New bytes must differ.
        new_emb = embeddings.encode("a completely different embedding source kappa8w")
        assert new_emb != old_emb
        storage.update_memory_embedding(mid, new_emb, "some-new-model-ckpt")

        # The per-id evict must have FIRED (proves invalidation, not just DB freshness):
        # the entry is gone from the cache, so the next read cannot serve the old bytes.
        assert cache.get(mid) is None, (
            "reembed did not evict the memory_doc entry — stale embedding would be served"
        )

        # CONTRACT: the next read serves the NEW embedding.
        out2 = _read(storage, [mid])
        assert out2[mid]["embedding"] == new_emb, (
            "reembed served a STALE embedding from memory_doc (train-blocker bug)"
        )
        # And the freshly-cached entry now holds the NEW embedding (re-populated on the
        # re-miss), so a subsequent read is a hit on the correct value — not stale.
        assert out2[mid]["embedding"] == _read(storage, [mid])[mid]["embedding"]


# ===========================================================================
# ce namespace — ModelCkpt-keyed (model swap busts; NOT a mutation-shape cache)
# ===========================================================================


class TestCeModelCkptKeying:
    """ce is a within-request dedup + cross-request memo keyed by
    query_sha:text_sha:ckpt_sha. There is no "mutate a memory → ce goes stale"
    shape — a content edit changes text_sha (natural miss) and a model swap changes
    ckpt_sha (natural miss). This test proves the ckpt component: two Cache
    instances with different checkpoint_hash do NOT cross-serve, so a model swap can
    never return a score computed under the old model."""

    def test_ckpt_hash_partitions_keys(self):
        from yadgar.backend.cache import Cache, ModelCkpt

        key = "querysha:textsha"
        c_old = Cache(
            name="ce", max_bytes=1 << 16, invalidation=ModelCkpt(), checkpoint_hash="ckptA"
        )
        c_old.put(key, 0.91)
        assert c_old.get(key) == 0.91  # same instance, same ckpt → hit

        # A model swap = a fresh instance stamped with the new ckpt. The snapshot
        # round-trip is ckpt-gated; an in-memory instance under a new ckpt starts
        # empty → the old score is never served.
        c_new = Cache(
            name="ce", max_bytes=1 << 16, invalidation=ModelCkpt(), checkpoint_hash="ckptB"
        )
        assert c_new.get(key) is None, "ce served a score across a checkpoint change (stale model)"

    def test_ckpt_gated_snapshot_discards_on_model_swap(self, tmp_path):
        """The persisted ce snapshot is discarded when the checkpoint hash differs —
        so a restart on a new model does not reload stale scores."""
        from yadgar.backend.cache import Cache, ModelCkpt

        c = Cache(name="ce", max_bytes=1 << 16, invalidation=ModelCkpt(), checkpoint_hash="ckptA")
        c.put("q:t", 0.5)
        c.save_snapshot(str(tmp_path), "ce")

        # Same ckpt → loads.
        c_same = Cache(
            name="ce", max_bytes=1 << 16, invalidation=ModelCkpt(), checkpoint_hash="ckptA"
        )
        c_same.load_snapshot(str(tmp_path), "ce")
        assert c_same.get("q:t") == 0.5

        # Different ckpt → discarded (empty), never serves old-model scores.
        c_diff = Cache(
            name="ce", max_bytes=1 << 16, invalidation=ModelCkpt(), checkpoint_hash="ckptB"
        )
        c_diff.load_snapshot(str(tmp_path), "ce")
        assert c_diff.get("q:t") is None, "stale-model ce snapshot was loaded after a model swap"


# ===========================================================================
# engram_slot (Car 3) — REAL populate→mutate→assert-fresh against a live backend
# ===========================================================================


class TestEngramSlotInvalidation:
    """Car 3 caches ONLY the heat-free structural slot membership, keyed by
    ``(slot_index, slot_version)``. Two invalidation vectors, two mechanisms:

    - NEW member appearing (create-alloc / reslot-into) → a slot-version BUMP at
      ``assign_memory_slot`` (the cached candidate set doesn't contain the new id
      until the version moves). This scenario proves the bump fires.
    - heat→0 decay AND heat-revival (heat 0→>0) → NO bump; the ``get_memories_in_slot``
      FRESH ``heat>0`` recheck on the cached candidate ids catches both. This scenario
      proves the recheck rides live heat, so a heat-free cache never serves stale
      occupancy.

    Anti-vacuity: each asserts ``cache.stats()`` engagement (size/hit for the slot's
    key) BEFORE mutating — so a MISSING bump (new member) or a heat-filtered cache
    (heat vectors) would serve a STALE occupancy → the freshness assert FAILS.
    """

    SLOT = 3

    def test_new_member_reflected_not_stale(self, e2e_engines):
        storage = e2e_engines["storage"]
        embeddings = e2e_engines["embeddings"]
        cache = _inject_slot_cache(storage)

        m1 = _seed(storage, embeddings, "slot member alpha aa11")
        storage.assign_memory_slot(m1, self.SLOT)

        # [1] POPULATE + prove the slot cache is engaged (not vacuous).
        occ1 = storage.get_memories_in_slot(self.SLOT)
        assert {m["id"] for m in occ1} == {m1}
        assert cache.stats()["size"] == 1, "slot cache did not populate — suite vacuous"
        base_hits = cache.stats()["hits"]
        storage.get_memories_in_slot(self.SLOT)  # repeat read must HIT the cached set
        assert cache.stats()["hits"] == base_hits + 1, "cached slot set not served on repeat"

        # [2] REAL mutation — a NEW member joins the slot. This is the ONLY vector the
        # fresh recheck cannot catch (the id is absent from the cached candidate set),
        # so it MUST bump the slot version at assign_memory_slot.
        m2 = _seed(storage, embeddings, "slot member beta bb22")
        storage.assign_memory_slot(m2, self.SLOT)

        # [3] recall again → the new member appears (version bump made the old key
        # unreachable → recompute). A missing bump would serve the stale {m1} set.
        occ2 = storage.get_memories_in_slot(self.SLOT)
        assert {m["id"] for m in occ2} == {m1, m2}, (
            "new slot member not reflected — stale cached occupancy (missing version bump)"
        )

    def test_heat_zero_and_revival_reflected_not_stale(self, e2e_engines):
        storage = e2e_engines["storage"]
        embeddings = e2e_engines["embeddings"]
        cache = _inject_slot_cache(storage)

        m1 = _seed(storage, embeddings, "heat slot gamma cc33", heat=1.0)
        m2 = _seed(storage, embeddings, "heat slot delta dd44", heat=1.0)
        storage.assign_memory_slot(m1, self.SLOT)
        storage.assign_memory_slot(m2, self.SLOT)

        # [1] POPULATE — cache holds the HEAT-FREE candidate set {m1, m2}.
        occ1 = storage.get_memories_in_slot(self.SLOT)
        assert {m["id"] for m in occ1} == {m1, m2}
        cached = cache.get((self.SLOT, storage._scope_versions.version("slot", self.SLOT)))
        assert cached is not None and set(cached) == {m1, m2}, (
            "slot cache did not hold the heat-free candidate set — suite vacuous"
        )

        # [2a] heat→0 on m2 — NO slot write, NO version bump. The fresh heat>0 recheck
        # must drop m2 from the OCCUPANCY even though it lingers in the cached set.
        storage.update_memory_fields(m2, heat=0.0)
        still_cached = cache.get((self.SLOT, storage._scope_versions.version("slot", self.SLOT)))
        assert still_cached is not None and set(still_cached) == {m1, m2}, (
            "precondition: heat→0 must NOT bump the slot version (heat-free cache)"
        )
        occ2 = storage.get_memories_in_slot(self.SLOT)
        assert {m["id"] for m in occ2} == {m1}, (
            "heat→0 member still in occupancy — fresh heat>0 recheck did not fire (STALE)"
        )

        # [2b] heat-revival on m2 (0 → 0.9) — again NO bump. The candidate id is still
        # in the cached set, so the fresh recheck re-admits it: revival reflected.
        storage.update_memory_fields(m2, heat=0.9)
        occ3 = storage.get_memories_in_slot(self.SLOT)
        assert {m["id"] for m in occ3} == {m1, m2}, (
            "heat-revived member not re-admitted — heat-free cache dropped it wrongly (STALE)"
        )


# ===========================================================================
# graph (Car 4) — REAL populate→mutate→assert-fresh against a live backend
# ===========================================================================


def _adjacent_ids(kg, eid):
    """Neighbour entity-id set for ``eid`` via the cached graph read seam
    (``KnowledgeGraph._get_adjacent_batch``, the exact fan-out PPR + spreading use)."""
    adj = kg._get_adjacent_batch([eid], None)
    return {n["entity_id"] for n in adj.get(eid, [])}


def _adjacent_weights(kg, eid):
    """Neighbour entity-id → weight map (weight is IN the cached adjacency)."""
    adj = kg._get_adjacent_batch([eid], None)
    return {n["entity_id"]: n["weight"] for n in adj.get(eid, [])}


class TestGraphInvalidation:
    """Car 4 caches PURE-STRUCTURAL per-entity adjacency, keyed by
    ``(entity_id, rel_types_key, entity_version)``. There is NO fresh recheck (unlike
    Car 3) — the cached neighbour list is served whole — so EVERY edge mutation
    (insert / reinforce-weight / DELETE) MUST bump BOTH endpoint entity versions or a
    stale adjacency survives. This is the hardest invalidation; DELETE is NOT inert.

    Each scenario: populate a source entity's adjacency (proving cache engagement via
    ``stats()`` — anti-vacuity), mutate an edge, re-read, and assert the observable
    adjacency reflects the mutation (never stale). A mutation that failed to bump the
    endpoints would serve the old cached neighbour list → the assert FAILS.
    """

    def _entity(self, storage, name):
        return storage.insert_entity({"name": name, "type": "concept"})

    def _kg(self):
        from yadgar.config import get_settings
        from yadgar.knowledge_graph import KnowledgeGraph
        from yadgar.server.lifecycle import _get_storage

        return KnowledgeGraph(_get_storage(), get_settings())

    def test_new_edge_reflected_not_stale(self, e2e_engines):
        storage = e2e_engines["storage"]
        cache = _inject_graph_cache(storage)
        kg = self._kg()

        a = self._entity(storage, "graph-A-ee55")
        b = self._entity(storage, "graph-B-ff66")
        storage.insert_relationship(
            {"source_entity_id": a, "target_entity_id": b, "relationship_type": "related_to"}
        )

        # [1] POPULATE a's adjacency + prove the graph cache is engaged (not vacuous).
        assert _adjacent_ids(kg, a) == {b}
        assert cache.stats()["size"] >= 1, "graph cache did not populate — suite vacuous"
        base_hits = cache.stats()["hits"]
        _adjacent_ids(kg, a)  # repeat read must HIT the cached adjacency
        assert cache.stats()["hits"] == base_hits + 1, "cached adjacency not served on repeat"

        # [2] REAL mutation — a NEW edge a→c. insert_relationship bumps both endpoints.
        c = self._entity(storage, "graph-C-gg77")
        storage.insert_relationship(
            {"source_entity_id": a, "target_entity_id": c, "relationship_type": "related_to"}
        )

        # [3] re-read → c appears (version bump made a's old key unreachable). A missing
        # bump would serve the stale {b} adjacency.
        assert _adjacent_ids(kg, a) == {b, c}, (
            "new edge not reflected — stale cached adjacency (missing endpoint bump)"
        )

    def test_reinforce_weight_reflected_not_stale(self, e2e_engines):
        storage = e2e_engines["storage"]
        cache = _inject_graph_cache(storage)
        kg = self._kg()

        a = self._entity(storage, "graph-W-A-hh88")
        b = self._entity(storage, "graph-W-B-ii99")
        rid = storage.insert_relationship(
            {
                "source_entity_id": a,
                "target_entity_id": b,
                "relationship_type": "related_to",
                "weight": 1.0,
            }
        )

        # [1] POPULATE — weight 1.0 is IN the cached adjacency.
        assert _adjacent_weights(kg, a) == {b: 1.0}
        assert cache.stats()["size"] >= 1, "graph cache did not populate — suite vacuous"
        h0 = cache.stats()["hits"]
        _adjacent_weights(kg, a)
        assert cache.stats()["hits"] == h0 + 1, "cached weighted adjacency not served on repeat"

        # [2] REAL mutation — reinforce bumps the weight (+2.5). No fresh recheck to
        # catch it → reinforce_relationship MUST bump both endpoints.
        storage.reinforce_relationship(rid, weight_increase=2.5)

        # [3] re-read → the NEW weight is served, never the stale 1.0.
        assert _adjacent_weights(kg, a) == {b: 3.5}, (
            "reinforced weight not reflected — stale cached weight (missing endpoint bump)"
        )

    def test_delete_edge_reflected_not_stale(self, e2e_engines):
        """DELETE is the hardest case: pure-structural cache means a deleted edge
        survives in cached adjacency unless the delete bumps both endpoints. The
        endpoints are resolved BEFORE the row vanishes (entity.py delete_relationship)."""
        storage = e2e_engines["storage"]
        cache = _inject_graph_cache(storage)
        kg = self._kg()

        a = self._entity(storage, "graph-D-A-jj00")
        b = self._entity(storage, "graph-D-B-kk11")
        c = self._entity(storage, "graph-D-C-ll22")
        storage.insert_relationship(
            {"source_entity_id": a, "target_entity_id": b, "relationship_type": "related_to"}
        )
        rid_ac = storage.insert_relationship(
            {"source_entity_id": a, "target_entity_id": c, "relationship_type": "related_to"}
        )

        # [1] POPULATE a's adjacency {b, c} + prove engagement.
        assert _adjacent_ids(kg, a) == {b, c}
        assert cache.stats()["size"] >= 1, "graph cache did not populate — suite vacuous"
        h0 = cache.stats()["hits"]
        _adjacent_ids(kg, a)
        assert cache.stats()["hits"] == h0 + 1, "cached adjacency not served on repeat"

        # [2] REAL mutation — DELETE a→c. Not inert: delete_relationship must bump both
        # endpoints (resolved before the DELETE) or the removed edge lingers in cache.
        storage.delete_relationship(rid_ac)

        # [3] re-read → c is GONE from a's adjacency. A delete that failed to bump would
        # serve the stale {b, c} set.
        assert _adjacent_ids(kg, a) == {b}, (
            "deleted edge still in adjacency — stale cached graph (delete did not bump endpoints)"
        )
