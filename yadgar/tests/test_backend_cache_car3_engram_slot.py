"""Car 3 — `engram_slot` cache on get_memories_in_slot (backend 5.20.0).

`_rerank_engram_links` (reranking.py:252) enriches every recall result with its
temporal links: `get_temporally_linked(memory_id)` (engram.py:128) →
`StorageEngine.get_memories_in_slot(slot_index)` (ops.py:420), the ~375 ms
engram-links stage. The slot query is `SELECT * FROM memory WHERE slot_index=$si
AND heat>0 ORDER BY created_at`.

Car 3 caches the STRUCTURAL slot membership only — the ordered candidate ids for
a slot (`slot → [id, …]`, created_at order) — and re-verifies `slot_index=$si AND
heat>0` FRESH on every read against the cached candidate ids. This is Car 2's
"cache structural, fetch volatile fresh" rule applied to slots.

Why the fresh recheck makes version-in-key CLEAN (Branch B resolved):

  * `get_memories_in_slot`'s `heat>0` filter means a memory's membership depends
    on BOTH `slot_index` (structural) AND `heat>0` (volatile — a live row's heat
    is set to 0 by pure decay, heat_decay.py:127-128, with NO slot write; the row
    stays alive, cold_retention.py). A global-epoch or naive version-in-key on
    slot writes would MISS the heat→0 transition → stale membership.
  * By caching the heat-FREE candidate id set and re-applying `heat>0` fresh at
    read, these vectors become INERT (no version bump needed):
      - heat→0 decay      → fresh filter drops it.
      - delete            → fresh query finds no row.
      - reslot-away       → fresh `slot_index=$si` no longer matches → dropped.
    The ONLY vector version-in-key must cover is a NEW member APPEARING in the
    slot (create-alloc or reslot-into) — a joined id isn't in the cached set, so
    it stays invisible until the slot's version bumps. That bump happens at the
    single `slot_index`-write choke point: `assign_memory_slot` (ops.py:412),
    routed to by both allocate (engram.py:93) and rebalance (engram.py:177).

version-in-key: the cache key is `(slot_index, slot_version)`; `slot_version` is
read from the per-scope `ScopeVersions` map (in-process module-global on the
backend `StorageEngine` — slot writes and the slot read share ONE process, so no
cross-service header). A bump makes the old `(slot, v)` key unreachable → the new
member's arrival is reflected on the next recall. Reusable for Car 4 (graph;
per-entity, pure-structural so no fresh-recheck needed).
"""

from __future__ import annotations

from yadgar.backend.cache import (
    Cache,
    CacheProtocol,
    NullCache,
    ScopeVersions,
    get_engram_slot_cache,
)

# ── fake storage host ─────────────────────────────────────────────────────────


class _FakeStorage:
    """Minimal host exposing get_memories_in_slot + assign_memory_slot + delete.

    Backing store is a dict id -> raw memory row (slot_index + heat + created_at).
    `_q` interprets exactly the two slot query shapes get_memories_in_slot issues:
    the full-slot scan (cold path) and the id-restricted fresh recheck (warm path).
    `_slot_queries` counts full-slot scans (the cache-miss signal).
    """

    def __init__(self, rows: dict[int, dict], engram_slot_cache: CacheProtocol) -> None:
        self._rows = rows  # id -> {slot_index, heat, created_at, content, ...}
        self._engram_slot_cache = engram_slot_cache
        self._scope_versions = ScopeVersions()
        self.full_slot_scans = 0  # unrestricted WHERE slot_index=$si scans
        self.recheck_queries = 0  # id-restricted fresh rechecks (warm hits)

    from yadgar.storage.client import _ClientMixin
    from yadgar.storage.ops import _OpsMixin

    get_memories_in_slot = _OpsMixin.get_memories_in_slot
    assign_memory_slot = _OpsMixin.assign_memory_slot
    _resolve_engram_slot_cache = _OpsMixin._resolve_engram_slot_cache
    _resolve_scope_versions = _OpsMixin._resolve_scope_versions
    _bump_slot_version = _OpsMixin._bump_slot_version
    _extract_id = _ClientMixin._extract_id

    def _now_iso(self) -> str:
        return "2026-07-06T00:00:00+00:00"

    def _rows_to_dicts(self, rows: list[dict]) -> list[dict]:
        return [dict(r) for r in rows]

    def _slot_members(self, si: int, *, heat_gate: bool) -> list[dict]:
        members = [
            r
            for r in self._rows.values()
            if r.get("slot_index") == si and (not heat_gate or r.get("heat", 0) > 0)
        ]
        members.sort(key=lambda r: r["created_at"])
        return members

    def _q(self, surql: str, params: dict | None = None) -> list:
        params = params or {}
        import re

        # assign_memory_slot old-slot lookup: SELECT VALUE slot_index FROM memory:$id
        if "SELECT VALUE slot_index" in surql:
            mid = int(params["id"])
            return [self._rows.get(mid, {}).get("slot_index")]
        # assign_memory_slot write: UPDATE ... SET slot_index = $si
        if surql.startswith("UPDATE") and "slot_index = $si" in surql:
            mid = int(params["id"])
            self._rows.setdefault(mid, {})["slot_index"] = params["si"]
            return []
        # Slot read. Two shapes:
        #   recheck (warm) — `id IN [...] AND slot_index=$si AND heat>0` (heat-gated)
        #   full scan (miss) — `slot_index=$si ORDER BY created_at` (HEAT-FREE now:
        #     Car 3 caches the structural membership; heat applied to the return).
        si = params.get("si")
        if "id IN" in surql:
            self.recheck_queries += 1
            allowed = {int(x) for x in re.findall(r"memory:(\d+)", surql)}
            heat_gate = "heat > 0" in surql
            rows = [r for r in self._slot_members(si, heat_gate=heat_gate) if r["id"] in allowed]
            return [dict(r) for r in rows]
        self.full_slot_scans += 1
        heat_gate = "heat > 0" in surql
        return [dict(r) for r in self._slot_members(si, heat_gate=heat_gate)]


def _row(mid: int, *, slot: int, heat: float = 1.0, created: str | None = None) -> dict:
    return {
        "id": mid,
        "slot_index": slot,
        "heat": heat,
        "created_at": created or f"2026-01-01T00:00:{mid:02d}+00:00",
        "content": f"content-{mid}",
        "tags": ["t"],
    }


def _mk_cache(budget: int = 1 << 20) -> Cache:
    return Cache(name="engram_slot_test", max_bytes=budget, obs_tier="cold")


# ── ScopeVersions unit ────────────────────────────────────────────────────────


def test_scope_versions_starts_at_zero_and_bumps():
    sv = ScopeVersions()
    assert sv.version("slot", 5) == 0
    sv.bump("slot", 5)
    assert sv.version("slot", 5) >= 1
    # independent scope-ids do not interfere
    assert sv.version("slot", 6) == 0
    # independent scope-kinds do not interfere (Car 4 reuse: kind="entity")
    sv.bump("entity", 5)
    assert sv.version("entity", 5) >= 1
    assert sv.version("slot", 6) == 0


def test_scope_versions_monotonic():
    sv = ScopeVersions()
    v0 = sv.version("slot", 1)
    sv.bump("slot", 1)
    v1 = sv.version("slot", 1)
    sv.bump("slot", 1)
    v2 = sv.version("slot", 1)
    assert v1 > v0
    assert v2 > v1


# ── get-or-compute ────────────────────────────────────────────────────────────


def test_get_or_compute_second_read_skips_full_scan():
    cache = _mk_cache()
    rows = {1: _row(1, slot=7), 2: _row(2, slot=7), 3: _row(3, slot=9)}
    st = _FakeStorage(rows, cache)

    first = st.get_memories_in_slot(7)
    assert st.full_slot_scans == 1
    assert [m["id"] for m in first] == [1, 2]

    second = st.get_memories_in_slot(7)
    # warm: no new full scan; a fresh id-restricted recheck instead.
    assert st.full_slot_scans == 1
    assert st.recheck_queries == 1
    assert [m["id"] for m in second] == [1, 2]


def test_created_at_order_preserved():
    cache = _mk_cache()
    rows = {
        1: _row(1, slot=3, created="2026-01-03T00:00:00+00:00"),
        2: _row(2, slot=3, created="2026-01-01T00:00:00+00:00"),
        3: _row(3, slot=3, created="2026-01-02T00:00:00+00:00"),
    }
    st = _FakeStorage(rows, cache)
    first = st.get_memories_in_slot(3)
    second = st.get_memories_in_slot(3)
    assert [m["id"] for m in first] == [2, 3, 1]
    assert [m["id"] for m in second] == [2, 3, 1]  # order stable across cache hit


# ── DI: Cache vs NullCache identical output ────────────────────────────────────


def test_null_cache_equivalent_output():
    rows = {1: _row(1, slot=4), 2: _row(2, slot=4), 3: _row(3, slot=5)}

    st_cache = _FakeStorage({k: dict(v) for k, v in rows.items()}, _mk_cache())
    st_null = _FakeStorage({k: dict(v) for k, v in rows.items()}, NullCache())

    for _ in range(3):
        out_cache = st_cache.get_memories_in_slot(4)
        out_null = st_null.get_memories_in_slot(4)
        assert out_cache == out_null

    # NullCache always misses → a full scan every call; never a recheck.
    assert st_null.full_slot_scans == 3
    assert st_null.recheck_queries == 0


# ── QUALITY-NEUTRAL: byte-identical membership across the cache boundary ────────


def test_quality_neutral_membership_identical():
    rows = {i: _row(i, slot=2) for i in range(1, 6)}
    st = _FakeStorage(rows, _mk_cache())
    cold = st.get_memories_in_slot(2)
    warm = st.get_memories_in_slot(2)
    assert [m["id"] for m in cold] == [m["id"] for m in warm]
    # full dicts equal too (fresh recheck returns the live row content)
    assert cold == warm


def test_quality_neutral_differential_across_mutations():
    """The real quality-neutral gate: cache-path ≡ no-cache-path across the FULL
    mutation set {add, delete, reslot-in, reslot-away, heat→0, revive}. Runs the
    identical sequence against a Cache-backed and a NullCache-backed host and
    asserts equal output after every step. Catches the heat-revival staleness bug
    (a heat-filtered cached candidate set would diverge here) automatically."""
    base = {1: _row(1, slot=8), 2: _row(2, slot=8), 3: _row(3, slot=3)}

    st_cache = _FakeStorage({k: dict(v) for k, v in base.items()}, _mk_cache())
    st_null = _FakeStorage({k: dict(v) for k, v in base.items()}, NullCache())

    def both(fn):
        fn(st_cache)
        fn(st_null)

    def assert_slot_equal(si):
        a = st_cache.get_memories_in_slot(si)
        b = st_null.get_memories_in_slot(si)
        assert a == b, f"slot {si}: cache={a!r} != nocache={b!r}"

    # warm both caches
    assert_slot_equal(8)
    assert_slot_equal(3)

    # add a new member to slot 8 (create-alloc → bump)
    both(
        lambda s: (
            s._rows.__setitem__(4, _row(4, slot=8, created="2026-01-01T00:00:99+00:00")),
            s.assign_memory_slot(4, 8),
        )
    )
    assert_slot_equal(8)

    # reslot 3 from slot 3 → slot 8 (bumps both)
    both(lambda s: s.assign_memory_slot(3, 8))
    assert_slot_equal(8)
    assert_slot_equal(3)

    # delete member 1 (no bump — inert via recheck)
    both(lambda s: s._rows.pop(1, None))
    assert_slot_equal(8)

    # heat→0 on member 2 (no bump — inert)
    both(lambda s: s._rows[2].__setitem__("heat", 0.0))
    assert_slot_equal(8)

    # revive member 2 (heat 0→>0, no bump — MUST reappear)
    both(lambda s: s._rows[2].__setitem__("heat", 0.7))
    assert_slot_equal(8)

    # reslot 4 away from slot 8 → slot 9 (source slot 8 loses it; inert via recheck)
    both(lambda s: s.assign_memory_slot(4, 9))
    assert_slot_equal(8)
    assert_slot_equal(9)


# ── STALE-EVICTION — the four vectors (load-bearing correctness) ───────────────


def test_stale_new_member_appears_after_version_bump():
    """NEW member joins slot between recalls → MUST appear (version-in-key path)."""
    cache = _mk_cache()
    rows = {1: _row(1, slot=8), 2: _row(2, slot=8)}
    st = _FakeStorage(rows, cache)

    first = st.get_memories_in_slot(8)
    assert [m["id"] for m in first] == [1, 2]

    # a new memory is allocated to slot 8 (create-alloc) — bumps the slot version.
    rows[3] = _row(3, slot=8, created="2026-01-01T00:00:99+00:00")
    st.assign_memory_slot(3, 8)

    second = st.get_memories_in_slot(8)
    assert 3 in [m["id"] for m in second], "new slot member must be visible after bump"
    assert [m["id"] for m in second] == [1, 2, 3]


def test_stale_reslot_into_appears_after_bump():
    """Reslot a memory INTO the slot → MUST appear; the old slot loses it."""
    cache = _mk_cache()
    rows = {1: _row(1, slot=8), 2: _row(2, slot=8), 5: _row(5, slot=3)}
    st = _FakeStorage(rows, cache)

    st.get_memories_in_slot(8)  # warm slot 8
    st.get_memories_in_slot(3)  # warm slot 3

    st.assign_memory_slot(5, 8)  # move 5 from slot 3 → slot 8 (bumps both)

    into = st.get_memories_in_slot(8)
    away = st.get_memories_in_slot(3)
    assert 5 in [m["id"] for m in into]
    assert 5 not in [m["id"] for m in away]


def test_stale_reslot_away_disappears_no_bump_needed():
    """Reslot-away is INERT via the fresh recheck even without a bump on the source
    slot (the fresh slot_index=$si filter drops the moved id)."""
    cache = _mk_cache()
    rows = {1: _row(1, slot=8), 2: _row(2, slot=8)}
    st = _FakeStorage(rows, cache)
    st.get_memories_in_slot(8)  # warm

    # move 2 away, but simulate a MISSED bump on the source slot (worst case).
    rows[2]["slot_index"] = 99

    out = st.get_memories_in_slot(8)
    assert [m["id"] for m in out] == [1], "reslot-away must drop via fresh recheck"


def test_stale_heat_zero_disappears_inert():
    """heat→0 decay (no slot write, no bump) → member MUST disappear via recheck."""
    cache = _mk_cache()
    rows = {1: _row(1, slot=8), 2: _row(2, slot=8)}
    st = _FakeStorage(rows, cache)
    st.get_memories_in_slot(8)  # warm — caches [1, 2]

    rows[2]["heat"] = 0.0  # pure decay zeroes a live row (no slot write)

    out = st.get_memories_in_slot(8)
    assert [m["id"] for m in out] == [1], "heat→0 member must drop via fresh heat recheck"


def test_stale_heat_revive_reappears():
    """A member that is heat=0 AT CACHE TIME and later revives (heat 0 → >0)
    MUST reappear with NO version bump — the load-bearing quality-neutral case.

    Because the miss path caches the HEAT-FREE structural membership (every id in
    the slot, not the heat-filtered result), id 2 is in the cached candidate set
    even though it was cold at cache time. A later access-boost revival (no slot
    write) is caught by the fresh heat>0 recheck. If the cache stored the
    heat-filtered ids instead, id 2 would be invisible until an unrelated slot
    write bumped the version — a quality-neutral violation with an unbounded
    staleness window."""
    cache = _mk_cache()
    rows = {1: _row(1, slot=8), 2: _row(2, slot=8, heat=0.0)}
    st = _FakeStorage(rows, cache)
    # cold read: 2 is heat-0 so excluded from the RETURNED members, but its id IS
    # cached (heat-free structural membership).
    first = st.get_memories_in_slot(8)
    assert [m["id"] for m in first] == [1]

    # 2's heat revives via access-boost — NO slot write, NO version bump.
    rows[2]["heat"] = 0.5

    second = st.get_memories_in_slot(8)
    assert [m["id"] for m in second] == [1, 2], "revived member must reappear (no bump)"
    assert st.full_slot_scans == 1, "revival must NOT force a full re-scan"


def test_stale_delete_disappears_inert():
    """Delete (row gone) → member MUST disappear via fresh recheck, no bump."""
    cache = _mk_cache()
    rows = {1: _row(1, slot=8), 2: _row(2, slot=8)}
    st = _FakeStorage(rows, cache)
    st.get_memories_in_slot(8)  # warm

    del rows[2]  # deleted — the fresh recheck finds no row

    out = st.get_memories_in_slot(8)
    assert [m["id"] for m in out] == [1], "deleted member must drop via fresh recheck"


# ── version-in-key bump on the slot-write site ────────────────────────────────


def test_assign_memory_slot_bumps_version():
    cache = _mk_cache()
    st = _FakeStorage({1: _row(1, slot=8)}, cache)
    sv = st._resolve_scope_versions()
    v_before = sv.version("slot", 8)
    st.assign_memory_slot(2, 8)
    assert sv.version("slot", 8) > v_before


def test_reslot_bumps_both_slots():
    cache = _mk_cache()
    st = _FakeStorage({5: _row(5, slot=3)}, cache)
    sv = st._resolve_scope_versions()
    v3 = sv.version("slot", 3)
    v8 = sv.version("slot", 8)
    st.assign_memory_slot(5, 8)  # reslot 3 → 8
    assert sv.version("slot", 8) > v8
    assert sv.version("slot", 3) > v3


# ── kill-switch: disabled ≡ today (all-miss, NullCache-equivalent) ─────────────


def test_kill_switch_disabled_is_null_equivalent(monkeypatch):
    monkeypatch.setenv("YADGAR_ENGRAM_SLOT_CACHE_ENABLED", "0")
    # a fresh registry entry must be built with budget 0 → all-miss.
    from yadgar.backend import cache as cache_mod

    cache_mod._REGISTRY.pop("engram_slot", None)
    c = get_engram_slot_cache()
    assert c.get(("slot", 0)) is None
    c.put(("slot", 0), [1, 2, 3])
    assert c.get(("slot", 0)) is None  # budget 0 → no-op
    cache_mod._REGISTRY.pop("engram_slot", None)


# ── obs counters ──────────────────────────────────────────────────────────────


def test_obs_counters_track_hits_and_misses():
    cache = _mk_cache()
    rows = {1: _row(1, slot=8), 2: _row(2, slot=8)}
    st = _FakeStorage(rows, cache)
    st.get_memories_in_slot(8)  # miss → put
    st.get_memories_in_slot(8)  # hit
    stats = cache.stats()
    assert stats["hits"] >= 1
    assert stats["misses"] >= 1


# ── factory / registry wiring ─────────────────────────────────────────────────


def test_get_engram_slot_cache_registered_and_protocol():
    from yadgar.backend import cache as cache_mod

    cache_mod._REGISTRY.pop("engram_slot", None)
    c = get_engram_slot_cache()
    assert isinstance(c, CacheProtocol)
    assert cache_mod._REGISTRY.get("engram_slot") is c
    cache_mod._REGISTRY.pop("engram_slot", None)
