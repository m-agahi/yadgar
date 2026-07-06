"""Car 4 — `graph` adjacency cache on spreading + PPR (backend 5.21.0).

Both graph read paths — PPR (`_build_networkx_graph`) and spreading BFS
(`_spreading_bfs_step`) — fan out through ``KnowledgeGraph._get_adjacent_batch``
(knowledge_graph.py:455), which today issues ONE
``get_relationships_for_frontier`` query per BFS depth (~1.7 s combined across
the two callers). Car 4 caches the per-entity adjacency so a warm frontier
serves cached neighbour lists and only the miss subset hits the DB.

VOLATILE-PREDICATE VERDICT — PURE STRUCTURAL (unlike Car 3):

  ``get_relationships_for_frontier`` / ``get_relationships_for_entity`` filter
  ONLY on the endpoint id (``WHERE source_entity_id IN $ids OR target_entity_id
  IN $ids ORDER BY id``). There is NO ``heat`` / ``archived`` / ``valid_until``
  / ``weight`` predicate on the adjacency read — entity heat/archived live on a
  SEPARATE entity-row read (``get_entities_by_ids``), never in the cached value,
  and the ``GRAPH_MIN_EDGE_WEIGHT`` cut is applied by the consumer to the
  ``weight`` that IS carried in the cached row. So the cached adjacency is
  PURE-STRUCTURAL and is served WHOLE — NO Car-3 fresh recheck (a recheck would
  re-query the relationship table and negate the ~1.7 s win).

  Consequence: unlike Car 3, DELETE is NOT inert here. With no recheck, EVERY
  edge mutation — insert, weight-change (reinforce / field update / decay),
  delete — must bump BOTH endpoint entities' versions, or a stale adjacency
  survives. Over-bump is perf-only; under-bump is a stale-recall correctness bug.

version-in-key: key = ``(entity_id, rel_types_key, entity_version)`` where
``entity_version`` is read from the shared ``ScopeVersions`` map with
``scope_kind="entity"`` (Car 3 reuse — same process-global instance, backend
writes + reads share one ``StorageEngine``). ``rel_types_key`` is the normalized
``rel_types`` filter (``None`` today for both callers) so a cached ``None``
(superset) value is never served to a future typed query.
"""

from __future__ import annotations

from yadgar.backend.cache import (
    Cache,
    CacheProtocol,
    NullCache,
    ScopeVersions,
    get_graph_cache,
)

# ── fake storage host ─────────────────────────────────────────────────────────


class _FakeStorage:
    """Minimal host exposing the graph read + the relationship-write bump sites.

    Backing store is a dict rel_id -> relationship row. ``_get_adjacent_batch``
    (borrowed from ``KnowledgeGraph``) reads adjacency through the cache seam;
    ``full_frontier_queries`` counts the DB fan-out (the cache-miss signal).
    """

    def __init__(self, rels: dict[int, dict], graph_cache: CacheProtocol) -> None:
        self._rels = (
            rels  # rel_id -> {source_entity_id, target_entity_id, relationship_type, weight}
        )
        self._graph_cache = graph_cache
        self._scope_versions = ScopeVersions()
        self._next_rel_id = (max(rels) + 1) if rels else 1
        self.full_frontier_queries = 0  # get_relationships_for_frontier DB hits

    from yadgar.knowledge_graph import KnowledgeGraph
    from yadgar.storage.entity import _EntityMixin
    from yadgar.storage.ops import _OpsMixin

    # graph read seam (KnowledgeGraph) — bound onto the fake so `self._storage`
    # is the fake itself (it plays both StorageEngine and its own `_graph`).
    _get_adjacent_batch = KnowledgeGraph._get_adjacent_batch
    _rows_to_adjacency = KnowledgeGraph._rows_to_adjacency

    # bump seam (storage ops) — the Car-4 additions.
    _resolve_graph_cache = _OpsMixin._resolve_graph_cache
    _resolve_scope_versions = _OpsMixin._resolve_scope_versions
    _bump_entity_version = _OpsMixin._bump_entity_version

    # relationship write sites (storage entity) that must bump both endpoints.
    insert_relationship = _EntityMixin.insert_relationship
    reinforce_relationship = _EntityMixin.reinforce_relationship
    update_relationship_fields = _EntityMixin.update_relationship_fields
    delete_relationship = _EntityMixin.delete_relationship
    _bump_relationship_endpoints = _EntityMixin._bump_relationship_endpoints

    # `KnowledgeGraph._get_adjacent_batch` reads `self._storage` and `self._graph`.
    @property
    def _storage(self):
        return self

    @property
    def _graph(self):
        return self

    def _now_iso(self) -> str:
        return "2026-07-06T00:00:00+00:00"

    def _rows_to_dicts(self, rows: list[dict]) -> list[dict]:
        return [dict(r) for r in rows]

    def _next_id(self, _kind: str) -> int:
        rid = self._next_rel_id
        self._next_rel_id += 1
        return rid

    # -- the DB adjacency query (frontier fan-out; pure-structural, id order) --
    def get_relationships_for_frontier(
        self, entity_ids: list[int], rel_types: list[str] | None = None
    ) -> list[dict]:
        self.full_frontier_queries += 1
        idset = set(entity_ids)
        rows = [
            r
            for rid, r in sorted(self._rels.items())
            if (r["source_entity_id"] in idset or r["target_entity_id"] in idset)
            and (rel_types is None or r["relationship_type"] in rel_types)
        ]
        return [dict(r) for r in rows]

    # -- minimal SurrealQL interpreter for the borrowed write mixins --
    def _q(self, surql: str, params: dict | None = None) -> list:
        params = params or {}
        if surql.startswith("CREATE") and "relationship" in surql:
            rid = int(params["id"])
            self._rels[rid] = {
                "id": rid,
                "source_entity_id": params["src"],
                "target_entity_id": params["tgt"],
                "relationship_type": params.get("rtype", params.get("rt", "co_occurrence")),
                "weight": params.get("weight", params.get("w", 1.0)),
            }
            return []
        if surql.startswith("SELECT") and "relationship" in surql and "$id" in surql:
            rid = int(params["id"])
            r = self._rels.get(rid)
            return [dict(r)] if r else []
        if surql.startswith("UPDATE") and "relationship" in surql:
            rid = int(params["id"])
            r = self._rels.get(rid)
            if r is not None and "inc" in params:
                r["weight"] = r.get("weight", 0.0) + params["inc"]
            elif r is not None and "weight" in params:
                r["weight"] = params["weight"]
            return []
        if surql.startswith("DELETE") and "relationship" in surql:
            rid = int(params["id"])
            self._rels.pop(rid, None)
            return []
        return []


def _rel(
    rid: int, src: int, tgt: int, *, weight: float = 1.0, rtype: str = "co_occurrence"
) -> dict:
    return {
        "id": rid,
        "source_entity_id": src,
        "target_entity_id": tgt,
        "relationship_type": rtype,
        "weight": weight,
    }


def _mk_cache(budget: int = 1 << 20) -> Cache:
    return Cache(name="graph_test", max_bytes=budget, obs_tier="cold")


def _neighbors(adj: dict[int, list[dict]], eid: int) -> list[int]:
    return [n["entity_id"] for n in adj.get(eid, [])]


# ── ScopeVersions reuse: entity scope kind ────────────────────────────────────


def test_scope_versions_entity_kind_independent():
    sv = ScopeVersions()
    assert sv.version("entity", 10) == 0
    sv.bump("entity", 10)
    assert sv.version("entity", 10) >= 1
    # slot scope (Car 3) is untouched by entity bumps.
    assert sv.version("slot", 10) == 0


# ── get-or-compute: warm frontier skips the DB fan-out ─────────────────────────


def test_get_or_compute_second_read_skips_frontier_query():
    cache = _mk_cache()
    rels = {1: _rel(1, 10, 20), 2: _rel(2, 10, 30)}
    st = _FakeStorage(rels, cache)

    first = st._get_adjacent_batch([10], None)
    assert st.full_frontier_queries == 1
    assert sorted(_neighbors(first, 10)) == [20, 30]

    second = st._get_adjacent_batch([10], None)
    # warm: no new DB query, served pure-structural from cache.
    assert st.full_frontier_queries == 1
    assert sorted(_neighbors(second, 10)) == [20, 30]


def test_partial_hit_only_queries_the_miss_subset():
    cache = _mk_cache()
    rels = {1: _rel(1, 10, 20), 2: _rel(2, 11, 21)}
    st = _FakeStorage(rels, cache)

    st._get_adjacent_batch([10], None)  # warm 10
    assert st.full_frontier_queries == 1

    both = st._get_adjacent_batch([10, 11], None)  # 10 hit, 11 miss
    assert st.full_frontier_queries == 2  # only the miss subset queried
    assert _neighbors(both, 10) == [20]
    assert _neighbors(both, 11) == [21]


# ── DI: Cache vs NullCache identical output ────────────────────────────────────


def test_null_cache_equivalent_output():
    rels = {1: _rel(1, 10, 20), 2: _rel(2, 10, 30), 3: _rel(3, 40, 50)}

    st_cache = _FakeStorage({k: dict(v) for k, v in rels.items()}, _mk_cache())
    st_null = _FakeStorage({k: dict(v) for k, v in rels.items()}, NullCache())

    for _ in range(3):
        out_cache = st_cache._get_adjacent_batch([10, 40], None)
        out_null = st_null._get_adjacent_batch([10, 40], None)
        assert out_cache == out_null

    # NullCache always misses → a frontier query every call.
    assert st_null.full_frontier_queries == 3
    # Cache: one miss then warm.
    assert st_cache.full_frontier_queries == 1


# ── QUALITY-NEUTRAL: fan-out parity (self-loop + shared edge) ──────────────────


def test_quality_neutral_fanout_matches_nullcache():
    # id 5: self-loop; id 6: edge shared between two frontier eids.
    rels = {
        1: _rel(1, 100, 100),  # self-loop on 100
        2: _rel(2, 100, 200),  # 100<->200 (both may be in frontier)
        3: _rel(3, 200, 300),
    }
    st_cache = _FakeStorage({k: dict(v) for k, v in rels.items()}, _mk_cache())
    st_null = _FakeStorage({k: dict(v) for k, v in rels.items()}, NullCache())

    a = st_cache._get_adjacent_batch([100, 200], None)
    b = st_null._get_adjacent_batch([100, 200], None)
    assert a == b, f"cache={a!r} != nocache={b!r}"
    # self-loop contributes exactly once to 100's list.
    assert _neighbors(a, 100).count(100) == 1


def test_quality_neutral_differential_across_mutations():
    """The real gate: cache-path ≡ nullcache-path across the full edge-mutation
    set {add-edge, reinforce-weight, field-update, delete, add-entity+edge}.
    Runs the identical sequence against a Cache- and a NullCache-backed host and
    asserts equal adjacency after every step. A missed bump on any edge mutation
    diverges here (no fresh recheck to hide it — see the volatile verdict)."""
    base = {1: _rel(1, 10, 20), 2: _rel(2, 10, 30)}

    st_cache = _FakeStorage({k: dict(v) for k, v in base.items()}, _mk_cache())
    st_null = _FakeStorage({k: dict(v) for k, v in base.items()}, NullCache())

    def both(fn):
        fn(st_cache)
        fn(st_null)

    def assert_adj_equal(*eids):
        a = st_cache._get_adjacent_batch(list(eids), None)
        b = st_null._get_adjacent_batch(list(eids), None)
        assert a == b, f"eids={eids}: cache={a!r} != nocache={b!r}"

    # warm both
    assert_adj_equal(10, 20, 30)

    # add a new edge 10<->40 (insert_relationship → bumps both 10 and 40).
    both(
        lambda s: s.insert_relationship(
            {"source_entity_id": 10, "target_entity_id": 40, "relationship_type": "co_occurrence"}
        )
    )
    assert_adj_equal(10, 40)

    # reinforce edge rel 1 (weight change → bumps both endpoints 10, 20).
    both(lambda s: s.reinforce_relationship(1, weight_increase=2.0))
    assert_adj_equal(10, 20)

    # field-update weight on rel 2 (bumps both 10, 30).
    both(lambda s: s.update_relationship_fields(2, weight=0.25))
    assert_adj_equal(10, 30)

    # delete edge rel 1 (bumps both 10, 20 — delete is NOT inert here).
    both(lambda s: s.delete_relationship(1))
    assert_adj_equal(10, 20)

    # add-entity+edge: a bare entity is inert until connected; connect 50<->30.
    both(
        lambda s: s.insert_relationship(
            {"source_entity_id": 50, "target_entity_id": 30, "relationship_type": "co_occurrence"}
        )
    )
    assert_adj_equal(30, 50)


# ── STALE-EVICTION — load-bearing: each vector RED without the bump ────────────


def test_stale_new_edge_appears_after_bump():
    """A new edge between two recalls MUST appear (version-in-key path)."""
    cache = _mk_cache()
    rels = {1: _rel(1, 10, 20)}
    st = _FakeStorage(rels, cache)

    first = st._get_adjacent_batch([10], None)
    assert _neighbors(first, 10) == [20]

    st.insert_relationship(
        {"source_entity_id": 10, "target_entity_id": 30, "relationship_type": "co_occurrence"}
    )

    second = st._get_adjacent_batch([10], None)
    assert 30 in _neighbors(second, 10), "new edge must be visible after entity-version bump"
    assert sorted(_neighbors(second, 10)) == [20, 30]


def test_stale_deleted_edge_disappears_after_bump():
    """A deleted edge MUST disappear — delete is NOT inert (no fresh recheck)."""
    cache = _mk_cache()
    rels = {1: _rel(1, 10, 20), 2: _rel(2, 10, 30)}
    st = _FakeStorage(rels, cache)

    st._get_adjacent_batch([10], None)  # warm → caches [20, 30]

    st.delete_relationship(1)  # remove 10<->20 (bumps 10 and 20)

    out = st._get_adjacent_batch([10], None)
    assert _neighbors(out, 10) == [30], "deleted edge must drop after bump"


def test_stale_reinforced_weight_reflected_after_bump():
    """A weight change (reinforce) MUST be reflected — weight is IN the cached
    value and PPR/spreading read it, so a stale weight is a quality bug."""
    cache = _mk_cache()
    rels = {1: _rel(1, 10, 20, weight=1.0)}
    st = _FakeStorage(rels, cache)

    first = st._get_adjacent_batch([10], None)
    assert first[10][0]["weight"] == 1.0

    st.reinforce_relationship(1, weight_increase=3.0)  # weight 1.0 -> 4.0, bumps 10, 20

    second = st._get_adjacent_batch([10], None)
    assert second[10][0]["weight"] == 4.0, "reinforced weight must be reflected after bump"


# ── version bump per write site (BOTH endpoints) ──────────────────────────────


def test_insert_relationship_bumps_both_endpoints():
    st = _FakeStorage({}, _mk_cache())
    sv = st._resolve_scope_versions()
    v10, v20 = sv.version("entity", 10), sv.version("entity", 20)
    st.insert_relationship(
        {"source_entity_id": 10, "target_entity_id": 20, "relationship_type": "co_occurrence"}
    )
    assert sv.version("entity", 10) > v10
    assert sv.version("entity", 20) > v20


def test_reinforce_relationship_bumps_both_endpoints():
    st = _FakeStorage({1: _rel(1, 10, 20)}, _mk_cache())
    sv = st._resolve_scope_versions()
    v10, v20 = sv.version("entity", 10), sv.version("entity", 20)
    st.reinforce_relationship(1)
    assert sv.version("entity", 10) > v10
    assert sv.version("entity", 20) > v20


def test_update_relationship_fields_bumps_both_endpoints():
    st = _FakeStorage({1: _rel(1, 10, 20)}, _mk_cache())
    sv = st._resolve_scope_versions()
    v10, v20 = sv.version("entity", 10), sv.version("entity", 20)
    st.update_relationship_fields(1, weight=0.5)
    assert sv.version("entity", 10) > v10
    assert sv.version("entity", 20) > v20


def test_delete_relationship_bumps_both_endpoints():
    st = _FakeStorage({1: _rel(1, 10, 20)}, _mk_cache())
    sv = st._resolve_scope_versions()
    v10, v20 = sv.version("entity", 10), sv.version("entity", 20)
    st.delete_relationship(1)
    assert sv.version("entity", 10) > v10
    assert sv.version("entity", 20) > v20


# ── rel_types in the key: a typed query never serves the None superset ─────────


def test_rel_types_key_isolation():
    cache = _mk_cache()
    rels = {1: _rel(1, 10, 20, rtype="calls"), 2: _rel(2, 10, 30, rtype="imports")}
    st = _FakeStorage(rels, cache)

    # cache the None (superset) result for entity 10.
    superset = st._get_adjacent_batch([10], None)
    assert sorted(_neighbors(superset, 10)) == [20, 30]

    # a typed query for the SAME entity must NOT be served the superset.
    typed = st._get_adjacent_batch([10], ["calls"])
    assert _neighbors(typed, 10) == [20], "typed query must not serve the None superset"


# ── kill-switch: disabled ≡ NullCache (all-miss) ──────────────────────────────


def test_kill_switch_disabled_is_null_equivalent(monkeypatch):
    monkeypatch.setenv("YADGAR_GRAPH_CACHE_ENABLED", "0")
    from yadgar.backend import cache as cache_mod

    cache_mod._REGISTRY.pop("graph", None)
    c = get_graph_cache()
    assert c.get((1, None, 0)) is None
    c.put((1, None, 0), [{"entity_id": 2, "relationship_type": "x", "weight": 1.0}])
    assert c.get((1, None, 0)) is None  # budget 0 → no-op
    cache_mod._REGISTRY.pop("graph", None)


# ── obs counters ──────────────────────────────────────────────────────────────


def test_obs_counters_track_hits_and_misses():
    cache = _mk_cache()
    rels = {1: _rel(1, 10, 20)}
    st = _FakeStorage(rels, cache)
    st._get_adjacent_batch([10], None)  # miss → put
    st._get_adjacent_batch([10], None)  # hit
    stats = cache.stats()
    assert stats["hits"] >= 1
    assert stats["misses"] >= 1


# ── factory / registry wiring ─────────────────────────────────────────────────


def test_get_graph_cache_registered_and_protocol():
    from yadgar.backend import cache as cache_mod

    cache_mod._REGISTRY.pop("graph", None)
    c = get_graph_cache()
    assert isinstance(c, CacheProtocol)
    assert cache_mod._REGISTRY.get("graph") is c
    cache_mod._REGISTRY.pop("graph", None)
