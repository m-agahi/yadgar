"""Phase-1 behavior-contract e2e tests — DB-layer only (v5.68).

Each test asserts a SHALL from BEHAVIOR_CONTRACT.md.  Status markers:
    GREEN  test passes
    xfail(strict, reason=#NN)  known-broken; flipping to pass = the fix

ANTI-BENDING rule: never weaken an assertion to make a test pass.
If code doesn't satisfy a SHALL, mark xfail(strict) and link the bug.

Tests drive the REAL code path against a real isolated SurrealDB.
No mocking the unit under test.  Seeding uses explicit IDs + real embeddings.
Assertions use membership / presence-absence — never similarity-ranking order.

Section A tests drive the REAL memorize() → drain → recall path.
Sections B/C/G/H seed via insert_memory (correct: seeding IS setup there,
the unit under test is recall/consolidation/checkpoint/reembed).

Run: make e2e
Requires: surreal binary on PATH (or YADGAR_DB_URL set by test harness).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

pytestmark = pytest.mark.e2e

# Branch used for memorize() calls — the e2e worktree branch.
_E2E_BRANCH = "feat/v5.68-e2e-net"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _embed(e2e_engines, content: str) -> bytes:
    """Encode *content* with the real embedding engine; return bytes."""
    return e2e_engines["embeddings"].encode(content)


def _drain(e2e_engines) -> None:
    """Drain the file queue so memorize() writes actually land in SurrealDB."""
    import yadgar._shared.runtime.state as _st

    drainer = _st._queue_drainer
    if drainer is not None:
        drainer.drain_now()


def _memorize_and_drain(e2e_engines, content: str, directory: str, tags: list[str]) -> dict | None:
    """Call memorize(), drain, return stored row dict or None.

    Returns the stored memory dict (with 'id', 'content', etc.) after drain,
    or None if the memory can't be found (signals a bug in the write path).
    Uses the same lookup pattern as memorize_sync() in tests/conftest.py.
    """
    server = e2e_engines["server"]
    storage = e2e_engines["storage"]

    result = server.memorize(
        content,
        directory,
        tags,
    )
    # Early-reject path — not queued, return result as-is
    if not result.get("queued"):
        return result

    # Drain the queue so the memory lands in SurrealDB
    _drain(e2e_engines)

    # Look up the stored row by exact content + directory match (FTS then heat scan)
    try:
        rows = storage.search_memories_fts(content[:100], min_heat=0.0, limit=20)
        for row in rows:
            if row.get("content") == content and row.get("directory_context") == directory:
                return row
    except Exception:
        pass

    try:
        recent = storage.get_memories_by_heat(min_heat=0.0, limit=100)
        for row in recent:
            if row.get("content") == content and row.get("directory_context") == directory:
                return row
    except Exception:
        pass

    return None


def _insert_mem(  # noqa: PLR0913 — test helper: each param is a required memory field
    e2e_engines,
    content: str,
    directory: str,
    *,
    heat: float = 0.8,
    tags: list[str] | None = None,
    last_accessed: str | None = None,
    is_protected: bool = False,
    tier: str | None = None,
    access_count: int = 0,
) -> int:
    """Insert a memory with a real embedding. Returns the row id.

    Used for seeding in sections B/C/G/H where insert_memory is correct
    (the unit under test is recall/consolidation, not the write path itself).
    Pass the desired directory_context directly as *directory*.
    """
    storage = e2e_engines["storage"]
    emb = _embed(e2e_engines, content)
    now = datetime.now(UTC).isoformat()
    doc = {
        "content": content,
        "embedding": emb,
        "directory_context": directory,
        "heat": heat,
        "tags": tags or [],
        "last_accessed": last_accessed or now,
        "created_at": now,
        "access_count": access_count,
        "is_protected": is_protected,
        "tier": tier,
    }
    return storage.insert_memory(doc)


def _ids_from_results(results: list[dict]) -> set[int]:
    """Extract integer ids from recall/wiki results."""
    out = set()
    for r in results:
        raw = r.get("id")
        if raw is None:
            continue
        if isinstance(raw, int):
            out.add(raw)
        elif isinstance(raw, str) and ":" in raw:
            try:
                out.add(int(raw.split(":", 1)[1]))
            except (ValueError, IndexError):  # fmt: skip
                pass
        else:
            try:
                out.add(int(raw))
            except (ValueError, TypeError):  # fmt: skip
                pass
    return out


# ---------------------------------------------------------------------------
# DATA-SAFETY proof
# ---------------------------------------------------------------------------


class TestDataSafetyIsolation:
    """Prove the isolation guard fires when pointed at the real data dir.

    This test does NOT use e2e_engines — it tests the guard function itself.
    Passes without touching real data; proves the guard is not a no-op.
    """

    def test_guard_raises_on_real_data_dir(self):
        """_assert_not_real_data_dir MUST raise RuntimeError on the real data dir."""
        # Build the path from the guard's OWN _REAL_DATA_DIR, not from a live
        # Path.home(): the autouse _guard_home fixture monkeypatches HOME to a tmp
        # dir, so Path.home() here would yield a tmp path that is NOT under the
        # guard's _REAL_DATA_DIR (frozen at module import = real home), and the
        # guard would correctly stay silent — masking the very violation this test
        # is meant to prove fires.
        from yadgar.tests.e2e.conftest import _REAL_DATA_DIR, _assert_not_real_data_dir

        real_data_dir = _REAL_DATA_DIR / "sentinel"
        with pytest.raises(RuntimeError, match="DATA-SAFETY VIOLATION"):
            _assert_not_real_data_dir(real_data_dir)

    def test_guard_passes_on_tmp_path(self, tmp_path):
        """_assert_not_real_data_dir MUST NOT raise on a tmp_path (safe path)."""
        from yadgar.tests.e2e.conftest import _assert_not_real_data_dir

        # tamper-lint: no-assert — assertion is implicit: call must not raise.
        # The test verifies the guard accepts safe paths without a RuntimeError.
        _assert_not_real_data_dir(tmp_path / "e2e_test.db")


# ---------------------------------------------------------------------------
# A. Write / memorize
#
# These tests drive the REAL memorize() → file-queue → drain → SurrealDB path.
# They do NOT use _insert_mem (direct storage bypass) — that would be circular
# and would miss regressions in the write pipeline.
# ---------------------------------------------------------------------------


class TestBCA1_MemorizeRecallRoundTrip:
    """BC-A1: memorize→recall round-trip stamps + returns the row.

    Drives: server.memorize() → queue drain → recall()
    Proves: the real write path persists the memory with correct directory_context
    and that recall() can surface it by content query.
    """

    def test_memorize_recall_roundtrip(self, e2e_engines, recall_backend_bypass):
        from yadgar.core.server.tools.recall import recall

        yadgar_dir = e2e_engines["yadgar_dir"]
        content = "BC-A1 unique sentinel content xbc1a1test real-memorize path"

        # Drive the REAL memorize() path — not direct storage insert
        row = _memorize_and_drain(e2e_engines, content, yadgar_dir, ["e2e", "bc-a1"])
        assert row is not None, (
            "BC-A1: memorize() + drain + lookup failed — memory not found in DB. "
            "Possible regression in the memorize→write-gate→embed→insert pipeline."
        )
        mid = row.get("id")
        assert mid is not None, f"BC-A1: stored row must have an id. Got: {row!r}"

        # Verify row has correct directory_context stamp
        assert row.get("directory_context") == yadgar_dir, (
            f"BC-A1: directory_context must equal {yadgar_dir!r}, got {row.get('directory_context')!r}"
        )

        # Verify recall surfaces the row from this directory.
        results = recall(
            "BC-A1 unique sentinel content",
            directory=yadgar_dir,
            max_results=10,
        )
        result_ids = _ids_from_results(results)
        # Also check by content string if id type mismatch
        result_contents = {r.get("content", "") for r in results}
        assert mid in result_ids or content in result_contents, (
            f"BC-A1: memorize→recall round-trip FAIL — stored id {mid!r} absent from recall. "
            f"Got ids: {result_ids}. Got contents sample: {list(result_contents)[:3]}"
        )


class TestBCA2_SurpriseGateDedup:
    """BC-A2: surprise/write gate stores novel; consolidation dedupes near-identical.

    test_novel_memory_stored: drives memorize() to prove novel content gets stored.
    test_exact_dedup_after_consolidation: seeds two identical rows, runs consolidation,
    asserts exactly one survives (consolidation is the unit under test — direct insert is correct).
    """

    def test_novel_memory_stored(self, e2e_engines):
        """A novel (unique) memory via memorize() SHALL be stored and retrievable."""
        yadgar_dir = e2e_engines["yadgar_dir"]
        content = "BC-A2 novel unique xbc2novel99837 memory absolutely unique content real-path"

        # Drive real memorize() path
        row = _memorize_and_drain(e2e_engines, content, yadgar_dir, ["e2e", "bc-a2"])
        assert row is not None, (
            "BC-A2: novel memory via memorize() must be stored and retrievable. Got None. "
            "Regression in write pipeline or queue drain."
        )
        assert row.get("content") == content, (
            f"BC-A2: stored content mismatch. Got: {row.get('content')!r}"
        )

    def test_exact_dedup_after_consolidation(self, e2e_engines):
        """Exact-duplicate content SHALL be merged down to one row after consolidation.

        Seeds via insert_memory (correct: consolidation is the unit under test,
        not the write path; insert_memory bypasses the gate to guarantee two rows exist).
        """
        import yadgar._shared.runtime.state as _st

        storage = e2e_engines["storage"]
        yadgar_dir = e2e_engines["yadgar_dir"]

        content = "BC-A2 exact-dup content xbc2dup88812 same content twice"
        mid1 = _insert_mem(e2e_engines, content, yadgar_dir, heat=0.7)
        mid2 = _insert_mem(e2e_engines, content, yadgar_dir, heat=0.6)

        assert mid1 != mid2, "Two insertions must produce distinct row ids"

        # Both rows exist before consolidation
        assert storage.get_memory(mid1) is not None
        assert storage.get_memory(mid2) is not None

        # Run consolidation — merge_duplicates phase fires
        consolidation = _st._consolidation
        assert consolidation is not None, "ConsolidationScheduler must be initialized"
        consolidation.force_consolidate()

        # After consolidation, exactly one of the two must survive (the hotter one)
        row1 = storage.get_memory(mid1)
        row2 = storage.get_memory(mid2)

        survivors = sum(1 for r in (row1, row2) if r is not None)
        assert survivors == 1, (
            f"BC-A2: exact-content dedup FAIL — {survivors} rows survived after consolidation "
            f"(expected 1). mid1={mid1} present={row1 is not None}, "
            f"mid2={mid2} present={row2 is not None}"
        )


class TestBCA3_EmbeddingOnWrite:
    """BC-A3: every write via memorize() SHALL receive an embedding.

    Drives: memorize() → drain → verify stored row has non-empty embedding bytes.
    This is NOT circular: memorize() triggers the full embed pipeline
    (EmbeddingEngine.encode → insert_memory with embedding).
    Direct insert_memory would bypass embed and trivially pass.
    """

    def test_memorize_generates_embedding(self, e2e_engines):
        """A memory written via memorize() + drain MUST have a non-empty embedding."""
        storage = e2e_engines["storage"]
        yadgar_dir = e2e_engines["yadgar_dir"]
        content = "BC-A3 embedding pipeline test xbc3emb77711 real-memorize content"

        # Drive real memorize() path — embed pipeline must run automatically
        row = _memorize_and_drain(e2e_engines, content, yadgar_dir, ["e2e", "bc-a3"])
        assert row is not None, (
            "BC-A3: memorize() + drain failed to persist. Regression in write pipeline."
        )

        mid = row.get("id")
        assert mid is not None, f"BC-A3: stored row must have an id. Got: {row!r}"

        # Fetch fresh from storage to get embedding (drain lookup strips it)
        if isinstance(mid, int):
            fresh = storage.get_memory(mid)
        else:
            # id may be a SurrealDB record string like "memory:123"
            try:
                numeric_id = int(str(mid).split(":")[-1])
                fresh = storage.get_memory(numeric_id)
            except (ValueError, IndexError):  # fmt: skip
                fresh = None

        if fresh is None:
            # Fall back to heat scan
            recent = storage.get_memories_by_heat(min_heat=0.0, limit=100)
            fresh = next((r for r in recent if r.get("content") == content), None)

        assert fresh is not None, f"BC-A3: could not re-fetch row for id={mid!r}"
        emb = fresh.get("embedding")
        assert emb is not None, (
            "BC-A3: memory written via memorize() SHALL have an embedding. Got None. "
            "Regression in embed pipeline or insert_memory embed field."
        )
        assert len(emb) > 0, "BC-A3: embedding must be non-empty bytes"


# ---------------------------------------------------------------------------
# B. Recall scoping
# ---------------------------------------------------------------------------


class TestBCB1_DirectoryFilter:
    """BC-B1: recall(directory=A) returns A + global, excludes project B."""

    def test_excludes_other_project(self, e2e_engines, recall_backend_bypass):
        """Memory stamped project-B MUST NOT appear in recall(directory=yadgar_dir)."""
        from yadgar.core.server.tools.recall import recall

        yadgar_dir = e2e_engines["yadgar_dir"]
        other_dir = e2e_engines["other_dir"]

        mid_yadgar = _insert_mem(
            e2e_engines,
            "BC-B1 yadgar project memory xb1yadgar55501",
            yadgar_dir,
            heat=0.9,
        )
        mid_other = _insert_mem(
            e2e_engines,
            "BC-B1 aws-work other project memory xb1other55502",
            other_dir,
            heat=0.9,
        )

        results = recall(
            "BC-B1 project memory",
            directory=yadgar_dir,
            max_results=20,
        )
        result_ids = _ids_from_results(results)

        assert mid_yadgar in result_ids, (
            f"BC-B1: yadgar-dir memory {mid_yadgar} must appear in recall(directory=yadgar_dir). "
            f"Got ids: {result_ids}"
        )
        assert mid_other not in result_ids, (
            f"BC-B1: other-project memory {mid_other} must NOT appear in recall(directory=yadgar_dir). "
            f"Got ids: {result_ids}"
        )

    def test_global_memory_included(self, e2e_engines, recall_backend_bypass):
        """Memory stamped directory_context='' (global) SHALL appear in any recall."""
        from yadgar.core.server.tools.recall import recall

        yadgar_dir = e2e_engines["yadgar_dir"]

        mid_global = _insert_mem(
            e2e_engines,
            "BC-B1 global memory xb1global55503 cross-project",
            "",  # global slot (empty string = global)
            heat=0.9,
        )

        results = recall(
            "BC-B1 global memory cross-project",
            directory=yadgar_dir,
            max_results=20,
        )
        result_ids = _ids_from_results(results)

        assert mid_global in result_ids, (
            f"BC-B1: global-slot memory {mid_global} must appear in recall(directory=yadgar_dir). "
            f"Got ids: {result_ids}"
        )


class TestBCB2_WikiDirectoryFilter:
    """BC-B2: wiki results in recall apply the SAME directory filter as memories."""

    def test_aws_wiki_excluded_from_yadgar_recall(self, e2e_engines, recall_backend_bypass):
        """A wiki page seeded with aws-dir MUST be excluded from recall(directory=yadgar_dir)."""
        from yadgar.core.server.tools.recall import recall
        from yadgar.core.server.tools.wiki import wiki_add

        yadgar_dir = e2e_engines["yadgar_dir"]
        other_dir = e2e_engines["other_dir"]

        # Seed a wiki page tagged to other_dir (aws-work)
        # Slug is auto-derived from title by the wiki layer.
        result = wiki_add(
            title="BC-B2 AWS xb2aws66601 test wiki page",
            content="# BC-B2 AWS Wiki\n\nThis page belongs to aws-work project xb2aws66601.",
            directory=other_dir,
            category="reference",
            tags=["e2e", "bc-b2"],
            wait=True,
        )
        # Accept any success indicator: committed, stored, or queued
        assert (
            result.get("committed")
            or result.get("stored")
            or result.get("queued")
            or "slug" in result
        ), f"BC-B2: wiki_add must succeed, got: {result}"

        stored_slug = result.get("slug", "")

        # Recall from yadgar_dir — the aws wiki page must NOT appear
        results = recall(
            "BC-B2 AWS xb2aws66601 wiki",
            directory=yadgar_dir,
            max_results=20,
        )
        result_slugs = {r.get("slug") for r in results if r.get("slug")}
        result_content_strs = " ".join(str(r.get("content", "")) for r in results)

        # Must not see the aws wiki page: check by slug if known, else by sentinel token
        if stored_slug:
            assert stored_slug not in result_slugs, (
                f"BC-B2: aws-dir wiki page (slug={stored_slug!r}) must NOT appear in "
                f"recall(directory=yadgar_dir). Got slugs: {result_slugs}"
            )
        # Belt-and-suspenders: sentinel token unique to the aws page must be absent
        assert "xb2aws66601" not in result_content_strs, (
            "BC-B2: aws-dir wiki page content must NOT appear in recall(directory=yadgar_dir)."
        )

    def test_yadgar_wiki_present_in_yadgar_recall(self, e2e_engines, recall_backend_bypass):
        """POSITIVE CONTROL: a wiki page seeded with yadgar_dir MUST appear in recall(directory=yadgar_dir).

        Without this, the absence assertion above passes trivially if recall returns zero
        results for any reason (e.g. filter bug that silences everything).
        """
        from yadgar.core.server.tools.recall import recall
        from yadgar.core.server.tools.wiki import wiki_add

        yadgar_dir = e2e_engines["yadgar_dir"]

        # Seed a wiki page tagged to yadgar_dir — mirror the aws seeding above exactly.
        result = wiki_add(
            title="BC-B2 Yadgar xb2yad77701 test wiki page",
            content="# BC-B2 Yadgar Wiki\n\nThis page belongs to yadgar project xb2yad77701.",
            directory=yadgar_dir,
            category="reference",
            tags=["e2e", "bc-b2"],
            wait=True,
        )
        assert (
            result.get("committed")
            or result.get("stored")
            or result.get("queued")
            or "slug" in result
        ), f"BC-B2 positive: wiki_add must succeed, got: {result}"

        stored_slug = result.get("slug", "")

        # Recall from yadgar_dir — the yadgar wiki page MUST appear
        results = recall(
            "BC-B2 Yadgar xb2yad77701 wiki",
            directory=yadgar_dir,
            max_results=20,
        )
        result_slugs = {r.get("slug") for r in results if r.get("slug")}
        result_content_strs = " ".join(str(r.get("content", "")) for r in results)

        # Must see the yadgar wiki page: check by slug if known, else by sentinel token
        if stored_slug:
            assert stored_slug in result_slugs, (
                f"BC-B2 positive: yadgar-dir wiki page (slug={stored_slug!r}) MUST appear in "
                f"recall(directory=yadgar_dir). Got slugs: {result_slugs}"
            )
        else:
            # Fallback: sentinel token unique to the yadgar page must be present
            assert "xb2yad77701" in result_content_strs, (
                "BC-B2 positive: yadgar-dir wiki page content MUST appear in recall(directory=yadgar_dir)."
            )


class TestBCB3_DirectoryRequired:
    """BC-B3: recall/wiki_query SHALL raise when directory is absent/empty."""

    def test_recall_raises_without_directory(self, e2e_engines):
        from yadgar.core.server.tools.recall import recall

        with pytest.raises(ValueError, match="directory"):
            recall("test query", directory=None)

    def test_recall_raises_with_empty_directory(self, e2e_engines):
        from yadgar.core.server.tools.recall import recall

        with pytest.raises(ValueError, match="directory"):
            recall("test query", directory="")

    def test_wiki_query_raises_without_directory(self, e2e_engines):
        from yadgar.core.server.tools.wiki import wiki_query

        with pytest.raises(ValueError, match="directory"):
            wiki_query("test query", directory=None)

    def test_wiki_query_raises_with_empty_directory(self, e2e_engines):
        from yadgar.core.server.tools.wiki import wiki_query

        with pytest.raises(ValueError, match="directory"):
            wiki_query("test query", directory="")


class TestBCB4_SystemTagExcluded:
    """BC-B4: directory_context='system' SHALL NOT be eligible in recall."""

    def test_system_memory_not_returned(self, e2e_engines, recall_backend_bypass):
        """A memory stamped directory_context='system' must not appear in user recall."""
        from yadgar.core.server.tools.recall import recall

        yadgar_dir = e2e_engines["yadgar_dir"]

        mid_system = _insert_mem(
            e2e_engines,
            "BC-B4 system internal sentinel xb4sys77701 drop-system test",
            "system",  # directory_context='system' must be excluded from recall
            heat=0.9,
        )

        results = recall(
            "BC-B4 system internal sentinel xb4sys77701",
            directory=yadgar_dir,
            max_results=20,
        )
        result_ids = _ids_from_results(results)

        assert mid_system not in result_ids, (
            f"BC-B4: system-stamped memory {mid_system} MUST NOT appear in recall. "
            f"Got ids: {result_ids}"
        )


class TestBCB5_ProfileRecallSurfaces:
    """BC-B5: profile-sourced results SHALL surface in recall when a matching profile exists.

    Bug #38: PROFILE_SEARCH_WEIGHT was undefined in config.py, referenced at
    retrieval/fusion.py:413, swallowed by bare except Exception: pass at ~416.
    Fix: define PROFILE_SEARCH_WEIGHT in Settings (default=1.0) AND narrow the
    except to (KeyError, TypeError, ValueError) so AttributeError surfaces.

    After the fix, a profile inserted for the test entity MUST appear in the
    recall results as a result with _source='profile' when PROFILE_EXTRACTION_ENABLED=True.
    """

    def test_profile_appears_in_recall(self, e2e_engines, recall_backend_bypass):
        from yadgar.core.server.tools.recall import recall

        yadgar_dir = e2e_engines["yadgar_dir"]
        storage = e2e_engines["storage"]

        # Insert a user_profile row directly (mimicking what profile extraction would do)
        pid = storage.insert_profile(
            entity_name="TestUserBC_B5_xb5prof88801",
            attribute_type="preference",
            attribute_key="language",
            attribute_value="Python (xb5prof88801 sentinel value)",
            confidence=0.9,
            directory_context=yadgar_dir,
        )
        assert isinstance(pid, int), f"insert_profile must return int id, got {pid!r}"

        # Also seed a real memory to ensure recall has at least one vector result
        _insert_mem(
            e2e_engines,
            "BC-B5 TestUserBC_B5_xb5prof88801 profile language preference Python",
            yadgar_dir,
            heat=0.9,
        )

        results = recall(
            "TestUserBC_B5_xb5prof88801 preference language",
            directory=yadgar_dir,
            max_results=20,
        )
        profile_sources = [r for r in results if r.get("_source") == "profile"]
        assert len(profile_sources) > 0, (
            "BC-B5: profile result MUST appear in recall when a matching profile exists. "
            "Got 0 profile-sourced results. "
            "Check fix #38: PROFILE_SEARCH_WEIGHT must be defined in Settings AND "
            "except clause in fusion.py must NOT catch AttributeError."
        )


class TestBCB6_BeliefRecallSurfaces:
    """BC-B6: belief-sourced results SHALL surface in recall when a matching belief exists.

    Bug (#230): the belief branch in retrieval/fusion.py caught a blanket
    ``except Exception: pass`` around ``search_beliefs_fts`` + config reads, so a
    missing config key (AttributeError) or any storage hiccup silently dropped
    ALL beliefs from recall. Fix: narrow the except to
    ``(KeyError, TypeError, ValueError)`` so AttributeError surfaces instead of
    swallowing every belief (mirrors the profile branch / BC-B5 fix #38).

    After the fix, a belief inserted for the test subject MUST appear in the
    recall results as a result with _source='belief' when DERIVED_BELIEFS_ENABLED=True.
    """

    def test_belief_appears_in_recall(self, e2e_engines, recall_backend_bypass):
        from yadgar._shared.storage.narrative import BeliefRecord
        from yadgar.core.server.tools.recall import recall

        yadgar_dir = e2e_engines["yadgar_dir"]
        storage = e2e_engines["storage"]

        # Insert a derived_belief row directly (mimicking what belief derivation would do)
        bid = storage.insert_belief(
            BeliefRecord(
                belief_type="preference",
                subject="TestUserBC_B6_xb6belief77701",
                content="TestUserBC_B6_xb6belief77701 prefers Python (xb6belief77701 sentinel value)",
                confidence=0.9,
                directory_context=yadgar_dir,
            )
        )
        assert isinstance(bid, int), f"insert_belief must return int id, got {bid!r}"

        # Also seed a real memory so recall has at least one vector result
        _insert_mem(
            e2e_engines,
            "BC-B6 TestUserBC_B6_xb6belief77701 belief preference Python",
            yadgar_dir,
            heat=0.9,
        )

        results = recall(
            "TestUserBC_B6_xb6belief77701 preference Python",
            directory=yadgar_dir,
            max_results=20,
        )
        belief_sources = [r for r in results if r.get("_source") == "belief"]
        assert len(belief_sources) > 0, (
            "BC-B6: belief result MUST appear in recall when a matching belief exists. "
            "Got 0 belief-sourced results. "
            "Check fix #230: the belief branch except in fusion.py must NOT catch "
            "AttributeError (narrowed to (KeyError, TypeError, ValueError)) so a "
            "config/storage error does not silently drop every belief."
        )


# ---------------------------------------------------------------------------
# C. Consolidation / decay / archive / purge
# ---------------------------------------------------------------------------


class TestBCC1_ConsolidationRuns:
    """BC-C1: consolidation cycle SHALL run to completion with 0 invariant violations."""

    def test_consolidation_completes_no_violations(self, e2e_engines, admin_backend_bypass):
        import yadgar._shared.runtime.state as _st
        from yadgar.core.server.tools.admin_invariants import check_invariants

        yadgar_dir = e2e_engines["yadgar_dir"]

        # Seed some memories to give consolidation something to process
        for i in range(3):
            _insert_mem(
                e2e_engines,
                f"BC-C1 consolidation test seed xbc1seed{i:05d} memory content",
                yadgar_dir,
                heat=0.8 - i * 0.1,
            )

        consolidation = _st._consolidation
        assert consolidation is not None, "ConsolidationScheduler must be initialized"

        # Run a full cycle — must not raise
        stats = consolidation.force_consolidate()
        assert stats is not None, "BC-C1: force_consolidate must return stats dict"

        # Check invariants — must report 0 violations on a healthy DB
        inv = check_invariants()
        violations = inv.get("violations", [])
        assert len(violations) == 0, (
            f"BC-C1: check_invariants reported {len(violations)} violation(s) "
            f"after consolidation: {violations}"
        )


class TestBCC2_HeatDecay:
    """BC-C2: heat decay SHALL lower heat; below cold_threshold → archived."""

    def test_heat_decay_lowers_heat(self, e2e_engines):
        """A memory with last_accessed far in the past must have heat lowered by decay."""
        import yadgar._shared.runtime.state as _st

        yadgar_dir = e2e_engines["yadgar_dir"]
        storage = e2e_engines["storage"]

        three_days_ago = (datetime.now(UTC) - timedelta(days=3)).isoformat()
        mid = _insert_mem(
            e2e_engines,
            "BC-C2 heat decay test xbc2decay99901 memory content old",
            yadgar_dir,
            heat=0.9,
            last_accessed=three_days_ago,
        )

        consolidation = _st._consolidation
        assert consolidation is not None
        stats = {"memories_archived": 0, "memories_updated": 0}
        consolidation._apply_decay(stats)

        row = storage.get_memory(mid)
        assert row is not None, "BC-C2: decayed memory must still exist"
        new_heat = float(row.get("heat", 0.9))
        assert new_heat < 0.9, (
            f"BC-C2: heat decay FAIL — heat must decrease from 0.9. Got {new_heat}"
        )

    def test_cold_memory_archived(self, e2e_engines):
        """A memory below cold_threshold after decay SHALL have heat set to 0.0 (archived marker)."""
        import yadgar._shared.runtime.state as _st
        from yadgar._shared.config import get_settings

        yadgar_dir = e2e_engines["yadgar_dir"]
        storage = e2e_engines["storage"]
        settings = get_settings()
        cold = settings.COLD_THRESHOLD

        # Insert a memory already just at the cold threshold
        # Use far past last_accessed to guarantee it decays below cold_threshold
        far_past = (datetime.now(UTC) - timedelta(days=365)).isoformat()
        mid = _insert_mem(
            e2e_engines,
            "BC-C2 cold archive test xbc2cold88802 memory content very old",
            yadgar_dir,
            heat=cold * 1.01,  # Just above threshold before decay
            last_accessed=far_past,
        )

        consolidation = _st._consolidation
        assert consolidation is not None

        # Apply decay — with 365-day-old last_accessed, heat will drop below cold
        stats = {"memories_archived": 0, "memories_updated": 0}
        consolidation._apply_decay(stats)

        # Check heat dropped to 0.0 (cold → archived per heat_decay.py:54-55)
        row = storage.get_memory(mid)
        assert row is not None
        new_heat = float(row.get("heat", cold))

        # The decay mixin sets heat=0.0 when below cold_threshold and increments memories_archived
        assert new_heat == 0.0 or stats["memories_archived"] >= 1, (
            f"BC-C2: cold memory {mid} must have heat=0.0 or be counted in memories_archived. "
            f"heat={new_heat}, stats={stats}"
        )


class TestBCC3_PurgeAndSpare:
    """BC-C3: old+not-recently-accessed derived memory purged; recently-accessed + protected spared."""

    def test_old_unaccessed_purged(self, e2e_engines):
        """An old, unaccessed, derived/auto-abstracted memory SHALL be purged."""
        import yadgar._shared.runtime.state as _st

        yadgar_dir = e2e_engines["yadgar_dir"]
        storage = e2e_engines["storage"]

        far_past = (datetime.now(UTC) - timedelta(days=400)).isoformat()
        mid = _insert_mem(
            e2e_engines,
            "BC-C3 old unaccessed derived memory xbc3purge77701 auto-abstracted",
            yadgar_dir,
            heat=0.01,  # Already cold
            last_accessed=far_past,
            tags=["auto-abstracted"],
            access_count=0,
        )
        # Manually set created_at to far past too
        storage._q(
            "UPDATE type::record('memory', $id) SET created_at = $ts, last_accessed = $ts",
            {"id": mid, "ts": far_past},
        )

        consolidation = _st._consolidation
        assert consolidation is not None
        consolidation.force_consolidate()

        # After consolidation, old unaccessed derived memory should be purged
        row = storage.get_memory(mid)
        assert row is None, (
            f"BC-C3: old unaccessed derived memory {mid} should be PURGED after consolidation. "
            "Still present — check prune_zombie_memories / _prune_old_derived."
        )

    def test_recently_accessed_spared(self, e2e_engines):
        """A recently-accessed memory SHALL be spared from purge."""
        import yadgar._shared.runtime.state as _st

        yadgar_dir = e2e_engines["yadgar_dir"]
        storage = e2e_engines["storage"]

        now = datetime.now(UTC).isoformat()
        mid = _insert_mem(
            e2e_engines,
            "BC-C3 recently accessed memory xbc3spare66602 keep this one",
            yadgar_dir,
            heat=0.5,
            last_accessed=now,
            access_count=5,
        )

        consolidation = _st._consolidation
        assert consolidation is not None
        consolidation.force_consolidate()

        row = storage.get_memory(mid)
        assert row is not None, (
            f"BC-C3: recently-accessed memory {mid} MUST be spared from purge. Was deleted."
        )

    def test_protected_always_spared(self, e2e_engines):
        """Protected/_anchor memory SHALL always be spared regardless of heat/age."""
        import yadgar._shared.runtime.state as _st

        yadgar_dir = e2e_engines["yadgar_dir"]
        storage = e2e_engines["storage"]

        far_past = (datetime.now(UTC) - timedelta(days=400)).isoformat()
        mid = _insert_mem(
            e2e_engines,
            "BC-C3 protected anchor memory xbc3anchor55503 must never die",
            yadgar_dir,
            heat=0.001,
            last_accessed=far_past,
            is_protected=True,
            tags=["_anchor"],
        )

        consolidation = _st._consolidation
        assert consolidation is not None
        consolidation.force_consolidate()

        row = storage.get_memory(mid)
        assert row is not None, (
            f"BC-C3: protected/_anchor memory {mid} MUST be spared from purge. Was deleted."
        )


# ---------------------------------------------------------------------------
# G. Checkpoint / restore
# ---------------------------------------------------------------------------


class TestBCCK1_CheckpointRestore:
    """BC-CK1: checkpoint then restore SHALL return the captured task/decisions/next-steps."""

    def test_checkpoint_restore_roundtrip(self, e2e_engines):
        """checkpoint() + restore() must return the captured fields."""
        from yadgar._shared.config import Settings
        from yadgar._shared.embeddings import EmbeddingEngine
        from yadgar._shared.restoration import CheckpointContext, CheckpointRestore

        storage = e2e_engines["storage"]
        yadgar_dir = e2e_engines["yadgar_dir"]

        settings = Settings(DB_PATH=e2e_engines["db_path"])
        embeddings = EmbeddingEngine()
        replay = CheckpointRestore(storage=storage, embeddings=embeddings, settings=settings)

        task = "BC-CK1 e2e checkpoint restore test xbcg1task44401"
        decisions = ["decision alpha xbcg1dec1", "decision beta xbcg1dec2"]
        next_steps = ["step one xbcg1ns1", "step two xbcg1ns2"]

        ctx = CheckpointContext(
            current_task=task,
            key_decisions=decisions,
            next_steps=next_steps,
        )
        replay.create_checkpoint(yadgar_dir, ctx)

        # Restore — must return checkpoint data
        restored = replay.restore(directory=yadgar_dir)
        assert restored is not None, "BC-CK1: restore must return a non-None result"

        # Find the checkpoint block in the restored output
        # restore() returns a dict; look for checkpoint content
        restored_str = str(restored)
        assert "xbcg1task44401" in restored_str or task in restored_str, (
            f"BC-CK1: restored output must contain the checkpoint task. Got: {restored_str[:500]}"
        )


# ---------------------------------------------------------------------------
# H. reembed_all
# ---------------------------------------------------------------------------


class TestBCADM1_ReembedAll:
    """BC-ADM1: reembed_all SHALL re-embed every memory missing an embedding."""

    def test_reembed_fills_missing_embeddings(self, e2e_engines, admin_backend_bypass):
        from yadgar.core.server.tools.admin_other import reembed_all

        storage = e2e_engines["storage"]
        yadgar_dir = e2e_engines["yadgar_dir"]

        # Insert a memory WITHOUT an embedding (bypass insert_memory's embed path)
        mid = storage._next_id("memory")
        now = datetime.now(UTC).isoformat()
        storage._q(
            f"CREATE memory:{mid} SET "
            "content = $content, directory_context = $dir, "
            "heat = $heat, last_accessed = $now, created_at = $now, "
            "embedding = NONE, tags = [], access_count = 0",
            {
                "content": "BC-ADM1 reembed all test xbch1reembed33301 no embedding initially",
                "dir": yadgar_dir,
                "heat": 0.8,
                "now": now,
            },
        )

        # Verify embedding is absent
        row = storage.get_memory(mid)
        assert row is not None
        assert row.get("embedding") is None, (
            "BC-ADM1: test setup: embedding must be None before reembed"
        )

        # Run reembed_all
        result = reembed_all()
        assert isinstance(result, dict), (
            f"BC-ADM1: reembed_all must return dict, got {type(result)}"
        )
        reembedded_count = result.get("reembedded", 0)
        assert reembedded_count >= 1, (
            f"BC-ADM1: reembed_all must report >=1 reembedded. Got {reembedded_count}. "
            f"Full result: {result}"
        )

        # Verify the row now has an embedding
        row2 = storage.get_memory(mid)
        assert row2 is not None
        emb = row2.get("embedding")
        assert emb is not None, (
            f"BC-ADM1: memory {mid} must have an embedding after reembed_all. Still None."
        )
        assert len(emb) > 0, "BC-ADM1: reembedded row must have non-empty embedding bytes"


# ---------------------------------------------------------------------------
# I. Hooks (directory stamping) — limited e2e feasibility
# ---------------------------------------------------------------------------


class TestBCI_HookNotes:
    """BC-I1/I2/I3: hooks directory stamping.

    BC-I1 and BC-I2 require a live Claude Code hook invocation to drive the
    real cwd-stamping path.  E2E testing at the hook-entry-point level is not
    deterministically feasible here (requires spawning Claude Code processes).
    Testing is at the unit level in existing test_hook_*.py files.

    BC-I3 (prompt-recall scoping) is verified by the directory filter tests
    above (BC-B1/B2) which exercise the same recall path the hook uses.

    This class documents the deferral reason — it is NOT empty to mark coverage.
    """

    @pytest.mark.skip(
        reason="⏳ BC-I1/I2: hook e2e requires live Claude Code process; deferred to Phase 2"
    )
    def test_bc_i1_tool_usage_hook_stamps_cwd(self, e2e_engines):
        """BC-I1: tool-usage capture hook SHALL stamp caller cwd as directory_context."""
        raise NotImplementedError

    @pytest.mark.skip(
        reason="⏳ BC-I2: session-end hook e2e requires live Claude Code process; deferred to Phase 2"
    )
    def test_bc_i2_session_end_hook_stamps_cwd(self, e2e_engines):
        """BC-I2: session-end hook SHALL stamp caller cwd as directory_context."""
        raise NotImplementedError
