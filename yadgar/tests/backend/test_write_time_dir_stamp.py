"""v5.64.0 — write-time directory stamping (chunk 2 part A).

Tests that derived/promoted/dream memories are stamped with the ORIGINATING
project directory rather than the always-eligible "system" sink.

These tests were RED against the pre-fix code (which hardcoded "system" in all
three sites) and GREEN after the production fixes.

Three write sites under test:
1. curation/strengthen.py _memify_derive  — co-occurrence derived memories
2. cls_store/promotion.py _PromotionMixin._promote_pattern — CLS cluster promotion
3. sleep_compute/dream.py _DreamMixin._create_dream_insight — dream connections

Also tests the shared dominant_directory() helper in storage/directory.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from yadgar._shared.storage.directory import dominant_directory
from yadgar.backend.curation.strengthen import _memify_derive

# ── dominant_directory helper ─────────────────────────────────────────────────


def test_dominant_directory_single_real_dir():
    """Single distinct real dir → that dir."""
    assert (
        dominant_directory(["/home/max/git/yadgar", "/home/max/git/yadgar"])
        == "/home/max/git/yadgar"
    )


def test_dominant_directory_multiple_real_dirs():
    """Multiple distinct real dirs → global (cross-cutting)."""
    result = dominant_directory(["/home/max/git/yadgar", "/home/max/aws-work"])
    assert result == "global"


def test_dominant_directory_all_sentinels():
    """All sentinels → global (safe fallback)."""
    assert dominant_directory(["system", "global", "", None]) == "global"


def test_dominant_directory_empty():
    """Empty list → global."""
    assert dominant_directory([]) == "global"


def test_dominant_directory_mixed_real_and_sentinels():
    """Real dir wins over sentinels in the vote."""
    result = dominant_directory(["system", "/home/max/git/yadgar", "global", None])
    assert result == "/home/max/git/yadgar"


def test_dominant_directory_none_only():
    """None only → global."""
    assert dominant_directory([None, None]) == "global"


def test_dominant_directory_system_excluded():
    """'system' is treated as a sentinel, not a real dir."""
    result = dominant_directory(["system", "system"])
    assert result == "global"


# ── _memify_derive (strengthen) ───────────────────────────────────────────────


def _make_storage_derive(
    entities=None,
    relationships=None,
    all_mems=None,
) -> MagicMock:
    storage = MagicMock()
    storage.get_all_entities.return_value = entities or []
    storage.get_relationships_by_types.return_value = relationships or []
    storage.get_all_memories_with_embeddings.return_value = all_mems or []
    storage._now_iso.return_value = "2026-01-01T00:00:00+00:00"
    storage._next_id.return_value = 99
    storage._bytes_to_floats.return_value = [0.0, 0.1]
    storage.batch_writes.return_value = None
    return storage


def _make_embeddings_derive(encoded_val=b"\x00" * 4) -> MagicMock:
    emb = MagicMock()
    emb.encode.return_value = encoded_val
    emb.get_model_name.return_value = "test-model"
    return emb


def _make_rel(rid: int, sid: int, tid: int, weight: float = 15.0) -> dict:
    return {
        "id": rid,
        "source_entity_id": sid,
        "target_entity_id": tid,
        "weight": weight,
    }


def test_derive_stamps_originating_dir_not_system():
    """RED pre-fix: derived memory got directory_context='system'.
    GREEN post-fix: derived memory gets the originating project directory.
    """
    entity_dicts = [
        {"id": 1, "name": "yadgar_module_foo.py"},
        {"id": 2, "name": "yadgar_module_bar.py"},
    ]
    rels = [_make_rel(10, 1, 2, weight=15.0)]
    # Source memories mention the entity names and have a real directory
    source_mems = [
        {
            "content": "worked on yadgar_module_foo.py today",
            "directory_context": "/home/max/git/yadgar",
            "project_id": "m-agahi/yadgar",
            "tags": ["episodic"],
        },
        {
            "content": "also touched yadgar_module_bar.py",
            "directory_context": "/home/max/git/yadgar",
            "project_id": "m-agahi/yadgar",
            "tags": ["episodic"],
        },
    ]
    storage = _make_storage_derive(entities=entity_dicts, relationships=rels, all_mems=source_mems)
    embeddings = _make_embeddings_derive()
    stats = {"derived": 0}
    _memify_derive(storage, embeddings, stats)
    assert stats["derived"] == 1
    batch = storage.batch_writes.call_args[0][0]
    _create_sql, create_params = batch[0]
    # Must NOT be "system"
    assert create_params["directory_context"] != "system", (
        "derived memory must not be stamped 'system' — it leaks into every project"
    )
    # Must be the originating project directory
    assert create_params["directory_context"] == "/home/max/git/yadgar"


def test_derive_stamps_global_for_cross_project_pair():
    """Entity names spanning two DIRECTORIES of one project → directory_context 'global'.

    C4 (0047 PR#40 §5) narrowed the fixture from two projects to two checkouts
    of one. The subject is unchanged — ``dominant_directory``'s ">=2 distinct
    real dirs -> global" rule for ``directory_context``, which C4 does not
    touch. What C4 changed is the IDENTITY half: a pair spanning two distinct
    ``project_id`` values is now skipped outright rather than collapsed onto a
    sentinel, and that case is asserted in
    ``test_c4_sessionless_writers.TestMemifyDeriveSkipsUnnameablePairs``.
    """
    entity_dicts = [
        {"id": 1, "name": "shared_util.py"},
        {"id": 2, "name": "another_util.py"},
    ]
    rels = [_make_rel(10, 1, 2, weight=15.0)]
    source_mems = [
        {
            "content": "shared_util.py used in checkout A",
            "directory_context": "/home/max/git/yadgar",
            "project_id": "m-agahi/yadgar",
            "tags": ["episodic"],
        },
        {
            "content": "another_util.py lives in checkout B",
            "directory_context": "/home/max/git/yadgar-worktree",
            "project_id": "m-agahi/yadgar",
            "tags": ["episodic"],
        },
    ]
    storage = _make_storage_derive(entities=entity_dicts, relationships=rels, all_mems=source_mems)
    embeddings = _make_embeddings_derive()
    stats = {"derived": 0}
    _memify_derive(storage, embeddings, stats)
    assert stats["derived"] == 1
    batch = storage.batch_writes.call_args[0][0]
    _sql, params = batch[0]
    assert params["directory_context"] == "global"


def test_derive_skips_when_no_source_memories_match():
    """No source memory mentions either entity → the pair is SKIPPED.

    CONTRACT FLIP, C4 (0047 PR#40 §5). This used to assert the derived fact
    was written with ``directory_context='global'``. A pair no source memory
    mentions cannot be attributed to any project, and under ADR-0227 an
    unattributable write is skipped and counted, never stamped with a
    sentinel that manufactures a plausible-looking namespace. The
    ``directory_context`` rule itself is unchanged — this pair simply never
    reaches it.
    """
    entity_dicts = [
        {"id": 1, "name": "UnknownEntityXYZ"},
        {"id": 2, "name": "UnknownEntityABC"},
    ]
    rels = [_make_rel(10, 1, 2, weight=15.0)]
    source_mems = [
        {
            "content": "something completely unrelated",
            "directory_context": "/home/max/git/yadgar",
            "project_id": "m-agahi/yadgar",
            "tags": ["episodic"],
        },
    ]
    storage = _make_storage_derive(entities=entity_dicts, relationships=rels, all_mems=source_mems)
    embeddings = _make_embeddings_derive()
    stats = {"derived": 0}
    _memify_derive(storage, embeddings, stats)
    assert stats["derived"] == 0, "an unattributable pair must not count as derived"
    storage.batch_writes.assert_not_called()


def test_derive_excludes_derived_mems_from_dir_vote():
    """Derived/auto-generated memories must not vote on directory — prevents self-reinforcement."""
    entity_dicts = [
        {"id": 1, "name": "entity_foo.py"},
        {"id": 2, "name": "entity_bar.py"},
    ]
    rels = [_make_rel(10, 1, 2, weight=15.0)]
    # Only a derived/auto-generated memory mentions these entities
    # (which would have system dir from old code) — must not win the vote
    all_mems = [
        {
            "content": "entity_foo.py and entity_bar.py are frequently modified together",
            "directory_context": "system",
            "tags": ["derived", "auto-generated"],
        },
    ]
    storage = _make_storage_derive(entities=entity_dicts, relationships=rels, all_mems=all_mems)
    storage._next_id.return_value = 42
    # The derived content already exists — so dedup prevents a new insert.
    # Use fresh entity names to force a NEW derive.
    entity_dicts2 = [
        {"id": 3, "name": "fresh_entity_A.py"},
        {"id": 4, "name": "fresh_entity_B.py"},
    ]
    rels2 = [_make_rel(20, 3, 4, weight=15.0)]
    all_mems2 = [
        {
            "content": "fresh_entity_A.py is mentioned in a derived memory",
            "directory_context": "system",
            "project_id": "m-agahi/other",
            "tags": ["derived", "auto-generated"],
        },
        {
            "content": "a real user memory mentioning fresh_entity_B.py",
            "directory_context": "/home/max/git/yadgar",
            "project_id": "m-agahi/yadgar",
            "tags": ["episodic"],
        },
    ]
    storage2 = _make_storage_derive(entities=entity_dicts2, relationships=rels2, all_mems=all_mems2)
    embeddings = _make_embeddings_derive()
    stats = {"derived": 0}
    _memify_derive(storage2, embeddings, stats)
    assert stats["derived"] == 1
    batch = storage2.batch_writes.call_args[0][0]
    _sql, params = batch[0]
    # The derived memory with "system" dir should NOT vote.
    # Only the real user memory with /home/max/git/yadgar should vote.
    assert params["directory_context"] == "/home/max/git/yadgar"


# ── _promote_pattern (cls_store/promotion.py) ─────────────────────────────────


def _make_cls_engine(insert_id=1, similar_mems=None):
    """Build a minimal mock _PromotionMixin-style object."""
    # No spec= — _PromotionMixin is a mixin; it lacks abstract_to_schema which
    # lives on the concrete DualStoreCLS.  Use a plain MagicMock and set
    # abstract_to_schema to return a non-empty schema so the guard passes.
    engine = MagicMock()
    engine._storage = MagicMock()
    engine._embeddings = MagicMock()
    engine._settings = MagicMock()
    engine._settings.CURATION_SIMILARITY_THRESHOLD = 0.85
    engine._embeddings.encode.return_value = b"\x00" * 4
    engine._embeddings.similarity.return_value = 0.1  # below threshold → no skip
    engine._embeddings.get_model_name.return_value = "test-model"
    engine._storage.search_vectors.return_value = []
    engine._storage.get_memory.return_value = None
    engine._storage.insert_memory.return_value = insert_id
    engine._storage.get_entity_by_name.return_value = None
    engine._storage.insert_entity.return_value = 99
    engine._storage.get_relationships_among_entities.return_value = []
    # abstract_to_schema lives on DualStoreCLS, not _PromotionMixin.
    # Return a non-degenerate schema so the promotion guard passes.
    engine.abstract_to_schema.return_value = "Recurring pattern: auth flow observed across sessions"
    # C4 (0047 PR#40 §5): the near-duplicate check moved out of _promote_pattern
    # into _near_duplicate_semantic_exists (the I30 cap needed one branch back).
    # A bare MagicMock returns a TRUTHY mock for it, which would short-circuit
    # every promotion — pin it False so these directory-stamp tests still reach
    # the insert. Behaviour under test is unchanged; only the seam moved.
    engine._near_duplicate_semantic_exists.return_value = False
    return engine


def _make_cluster_mem(
    mid: int, content: str, directory: str, project_id: str = "m-agahi/yadgar"
) -> dict:
    """Build a cluster member.

    C4 (0047 PR#40 §5): ``project_id`` defaults to a single real project so
    these ``directory_context`` tests keep exercising ``dominant_directory``.
    The identity half — a cluster spanning two projects is skipped, not
    collapsed — is asserted in
    ``test_c4_sessionless_writers.TestClsPromotionSkipsUnnameableClusters``.
    """
    return {
        "id": mid,
        "content": content,
        "embedding": b"\x00" * 4,
        "directory_context": directory,
        "project_id": project_id,
    }


def test_promote_stamps_originating_dir_not_system():
    """RED pre-fix: promoted memory got directory_context from directories[0] which
    could be 'system'. GREEN post-fix: gets dominant real project directory.
    """
    from yadgar.backend.cls_store.promotion import _PromotionMixin

    engine = _make_cls_engine(insert_id=101)
    cluster_mems = [
        _make_cluster_mem(1, "memory about auth", "/home/max/git/yadgar"),
        _make_cluster_mem(2, "memory about auth flow", "/home/max/git/yadgar"),
        _make_cluster_mem(3, "another auth memory", "/home/max/git/yadgar"),
    ]
    pattern = {
        "memories": cluster_mems,
        "directories": ["/home/max/git/yadgar"],
        "pattern_summary": "auth pattern",
        "occurrence_count": 3,
        "session_count": 2,
    }

    # Call real method directly by binding it to our mock engine
    result = _PromotionMixin._promote_pattern(engine, pattern)
    assert result is True

    # Verify directory_context in the insert_memory call
    call_kwargs = engine._storage.insert_memory.call_args[0][0]
    assert call_kwargs["directory_context"] != "system", (
        "promoted memory must not be stamped 'system'"
    )
    assert call_kwargs["directory_context"] == "/home/max/git/yadgar"


def test_promote_stamps_global_for_cross_dir_cluster():
    """Cluster with members from multiple directories of one project → 'global'."""
    from yadgar.backend.cls_store.promotion import _PromotionMixin

    engine = _make_cls_engine(insert_id=102)
    cluster_mems = [
        _make_cluster_mem(1, "worktree thing", "/home/max/git/yadgar-wt"),
        _make_cluster_mem(2, "yadgar thing", "/home/max/git/yadgar"),
    ]
    pattern = {
        "memories": cluster_mems,
        "directories": ["/home/max/git/yadgar-wt", "/home/max/git/yadgar"],
        "pattern_summary": "mixed pattern",
        "occurrence_count": 2,
        "session_count": 2,
    }
    result = _PromotionMixin._promote_pattern(engine, pattern)
    assert result is True
    call_kwargs = engine._storage.insert_memory.call_args[0][0]
    assert call_kwargs["directory_context"] == "global"
    assert call_kwargs["directory_context"] != "system"


def test_promote_stamps_global_when_no_cluster_dir():
    """Empty cluster_mems (degenerate) → global fallback, never 'system'."""
    from yadgar.backend.cls_store.promotion import _PromotionMixin

    engine = _make_cls_engine(insert_id=103)
    # All cluster members have sentinel directories
    cluster_mems = [
        _make_cluster_mem(1, "some memory", "system"),
        _make_cluster_mem(2, "another memory", "global"),
    ]
    pattern = {
        "memories": cluster_mems,
        "directories": [],
        "pattern_summary": "pattern",
        "occurrence_count": 2,
        "session_count": 2,
    }
    result = _PromotionMixin._promote_pattern(engine, pattern)
    assert result is True
    call_kwargs = engine._storage.insert_memory.call_args[0][0]
    assert call_kwargs["directory_context"] == "global"
    assert call_kwargs["directory_context"] != "system"


def test_promote_uses_cluster_mems_not_directories_list():
    """Fix verifies we use cluster_mems for dir derivation, not pattern['directories'].
    Even if pattern['directories'] has a misleading value, the real dir is used.
    """
    from yadgar.backend.cls_store.promotion import _PromotionMixin

    engine = _make_cls_engine(insert_id=104)
    cluster_mems = [
        _make_cluster_mem(1, "mem1", "/home/max/git/yadgar"),
        _make_cluster_mem(2, "mem2", "/home/max/git/yadgar"),
        _make_cluster_mem(3, "mem3", "/home/max/git/yadgar"),
    ]
    # Deliberately wrong directories list (what old code would pick as directories[0])
    pattern = {
        "memories": cluster_mems,
        "directories": ["system"],  # old code would return "system" from directories[0]
        "pattern_summary": "real yadgar pattern",
        "occurrence_count": 3,
        "session_count": 2,
    }
    result = _PromotionMixin._promote_pattern(engine, pattern)
    assert result is True
    call_kwargs = engine._storage.insert_memory.call_args[0][0]
    # Must use cluster_mems dirs, not the 'system' from pattern['directories']
    assert call_kwargs["directory_context"] == "/home/max/git/yadgar"


# ── _create_dream_insight (sleep_compute/dream.py) ────────────────────────────


def _make_dream_storage(insert_id: int = 200) -> MagicMock:
    storage = MagicMock()
    storage.insert_memory.return_value = insert_id
    storage.update_memory_scores.return_value = None
    return storage


def _make_dream_embeddings() -> MagicMock:
    emb = MagicMock()
    emb.encode.return_value = b"\x00" * 4
    emb.get_model_name.return_value = "test-model"
    return emb


def _make_dream_engine(storage, embeddings):
    from yadgar.backend.sleep_compute.dream import _DreamMixin

    engine = MagicMock(spec=_DreamMixin)
    engine._storage = storage
    engine._embeddings = embeddings
    return engine


def test_dream_insight_stamps_global_not_system():
    """RED pre-fix: dream insight got directory_context='system'.
    GREEN post-fix: stamps 'global' (dreams are cross-cutting by construction).
    """
    from yadgar.backend.sleep_compute.dream import _DreamMixin

    storage = _make_dream_storage(insert_id=201)
    embeddings = _make_dream_embeddings()
    engine = _make_dream_engine(storage, embeddings)

    mem_a = {
        "id": 1,
        "content": "React component lifecycle hooks",
        "embedding": b"\x00" * 4,
        "project_id": "m-agahi/yadgar",
    }
    mem_b = {
        "id": 2,
        "content": "Vue.js lifecycle methods",
        "embedding": b"\x00" * 4,
        "project_id": "m-agahi/yadgar",
    }

    _DreamMixin._create_dream_insight(engine, mem_a, mem_b)

    storage.insert_memory.assert_called_once()
    call_kwargs = storage.insert_memory.call_args[0][0]
    assert call_kwargs["directory_context"] != "system", (
        "dream insight must not be stamped 'system' — it leaks into every project"
    )
    assert call_kwargs["directory_context"] == "global", (
        "dream insight must be stamped 'global' (cross-cutting by construction)"
    )


def test_dream_insight_content_mentions_both_mems():
    """Sanity check: dream content mentions both source memories."""
    from yadgar.backend.sleep_compute.dream import _DreamMixin

    storage = _make_dream_storage(insert_id=202)
    embeddings = _make_dream_embeddings()
    engine = _make_dream_engine(storage, embeddings)

    mem_a = {
        "id": 3,
        "content": "Django ORM query optimization",
        "embedding": b"\x00" * 4,
        "project_id": "m-agahi/yadgar",
    }
    mem_b = {
        "id": 4,
        "content": "SQL index strategies for large tables",
        "embedding": b"\x00" * 4,
        "project_id": "m-agahi/yadgar",
    }

    _DreamMixin._create_dream_insight(engine, mem_a, mem_b)

    call_kwargs = storage.insert_memory.call_args[0][0]
    assert "Dream connection:" in call_kwargs["content"]
    assert call_kwargs["directory_context"] == "global"


def test_dream_insight_global_regardless_of_mem_dirs():
    """Dream insight is always 'global' even when both source mems are same dir."""
    from yadgar.backend.sleep_compute.dream import _DreamMixin

    storage = _make_dream_storage(insert_id=203)
    embeddings = _make_dream_embeddings()
    engine = _make_dream_engine(storage, embeddings)

    # Both from same dir — but dreams are still cross-cutting (random pairs)
    mem_a = {
        "id": 5,
        "content": "auth module refactor",
        "embedding": b"\x00" * 4,
        "directory_context": "/home/max/git/yadgar",
        "project_id": "m-agahi/yadgar",
    }
    mem_b = {
        "id": 6,
        "content": "permission check refactor",
        "embedding": b"\x00" * 4,
        "directory_context": "/home/max/git/yadgar",
        "project_id": "m-agahi/yadgar",
    }

    _DreamMixin._create_dream_insight(engine, mem_a, mem_b)

    call_kwargs = storage.insert_memory.call_args[0][0]
    # Dreams stay 'global' — they're synthetic random-pair associations
    assert call_kwargs["directory_context"] == "global"
