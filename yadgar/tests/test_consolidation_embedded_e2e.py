"""Behavioral E2E regression guard: embedded-mode consolidation liveness.

WHY THIS TEST EXISTS
====================
For 6 weeks the nightly consolidation cycle silently died every night. The
nightly script (yadgar/scripts/nightly_cycle.py:152) opens StorageEngine in
*embedded* mode (no YADGAR_DB_URL) then calls scheduler.force_consolidate().
Before the fix, _init_schema() issued a FULLTEXT index definition unconditionally;
the embedded Python surrealdb v2 driver does not support FULLTEXT/BM25 and throws
a parse error, so StorageEngine.__init__ crashed before force_consolidate() was
ever reached.  No heat decay, no prune, no episode processing -- silently.

Existing consolidation tests all run against the daemon fixture (YADGAR_DB_URL
set) where FULLTEXT works, so none of them caught this.

THIS TEST:
1. Opens StorageEngine in EMBEDDED mode (monkeypatch.delenv ensures no YADGAR_DB_URL).
2. Seeds 4 memories with controlled heat + last_accessed:
   - mem_old_a: heat=0.9, last_accessed=3 days ago, not protected  -> must decay
   - mem_old_b: heat=0.85, last_accessed=3 days ago, not protected -> must decay
   - mem_anchor: heat=0.5, last_accessed=3 days ago, is_protected=true, tier='conditional'
                  -> must NOT decay (get_all_memories_for_decay excludes protected rows)
   - mem_fresh: heat=0.6, last_accessed=NOW -> minimal/no decay
3. Patches batch_writes on the storage instance to rewrite type::record() parameterized
   IDs to inline IDs (e.g. `memory:1`), since the embedded Python surrealdb v2 SDK
   rejects integer params in type::record(). This lets _apply_decay() write heat
   updates back to the DB without a server.
4. Calls scheduler._apply_decay() and asserts real heat changes in the DB.

FAIL condition on pre-fix code: StorageEngine.__init__ raises on the FULLTEXT
DEFINE statement before _apply_decay is ever reached.

PASS condition post-fix: _init_schema guards FULLTEXT behind `if self._db_url`,
embedded init succeeds, decay runs, assertions hold.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from yadgar.config import get_settings
from yadgar.consolidation import ConsolidationScheduler
from yadgar.embeddings import EmbeddingEngine
from yadgar.storage import StorageEngine

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DIR = "/tmp/test_embedded_consolidate_e2e"
_THREE_DAYS_AGO = (datetime.now(UTC) - timedelta(days=3)).isoformat()
_NOW = datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _insert_memory_embedded(
    storage: StorageEngine,
    content: str,
    heat: float,
    last_accessed: str,
    is_protected: bool = False,
    tier: str | None = None,
) -> int:
    """Insert a memory row using inline integer ID syntax.

    Uses `CREATE memory:{mid} SET ...` rather than `CREATE type::record('memory', $id) SET ...`
    because the embedded Python surrealdb v2 SDK rejects integer params in type::record().
    Integer IDs are generated via _next_id() (counter table) to stay int-compatible.
    """
    mid = storage._next_id("memory")
    storage._q(
        f"CREATE memory:{mid} SET "
        "content = $content, directory_context = $dir, "
        "heat = $heat, last_accessed = $last_accessed, created_at = $created_at, "
        "is_protected = $is_protected, tags = $tags, tier = $tier, "
        "access_count = 0, access_count_since_decay = 0",
        {
            "content": content,
            "dir": _DIR,
            "heat": heat,
            "last_accessed": last_accessed,
            "created_at": last_accessed,
            "is_protected": is_protected,
            "tags": ["_anchor"] if is_protected else [],
            "tier": tier,
        },
    )
    return mid


def _read_heat(storage: StorageEngine, mid: int) -> float:
    """Read the current heat for a specific memory row."""
    rows = storage._q(f"SELECT heat FROM memory:{mid}")
    assert rows, f"memory:{mid} not found"
    return float(rows[0]["heat"])


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def embedded_storage(tmp_path, monkeypatch):
    """StorageEngine in embedded mode -- no YADGAR_DB_URL whatsoever.

    CRITICAL: if the FULLTEXT guard is missing from _init_schema(), this fixture
    raises a SurrealDB parse error and every test below fails at setup. That is
    the regression we are guarding against.
    """
    monkeypatch.delenv("YADGAR_DB_URL", raising=False)
    get_settings.cache_clear()
    db_path = str(tmp_path / "test_embedded_e2e.db")
    storage = StorageEngine(db_path)
    # NO batch_writes patch — exercise the REAL embedded write path (v5.63).
    yield storage
    storage.close()


@pytest.fixture()
def scheduler(embedded_storage):
    """ConsolidationScheduler wired to the embedded StorageEngine.

    EmbeddingEngine is created but NOT warmed up -- _apply_decay() does not call
    encode(), so the sentence-transformer model is never downloaded.
    """
    settings = get_settings()
    embeddings = EmbeddingEngine()
    return ConsolidationScheduler(embedded_storage, embeddings, settings)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEmbeddedConsolidationE2E:
    """E2E regression: embedded StorageEngine + heat decay must work without YADGAR_DB_URL.

    Pre-fix failure mode: StorageEngine.__init__ raised a SurrealDB parse error on
    the FULLTEXT DEFINE statement -- no code ever reached force_consolidate() or
    _apply_decay().
    """

    def test_embedded_storage_initialises_without_error(self, embedded_storage):
        """StorageEngine opens in embedded mode without raising.

        On pre-fix code this test fails with a SurrealDB parse error inside
        _init_schema() -- the FULLTEXT statement was not guarded by `if self._db_url`.
        This is the PRIMARY regression check.
        """
        assert embedded_storage._db_url is None, (
            "YADGAR_DB_URL must be absent for embedded mode; fixture isolation failed"
        )

    def test_decay_reduces_heat_on_old_non_protected_memories(self, embedded_storage, scheduler):
        """Non-protected memories with old last_accessed have lower heat after _apply_decay.

        This is the liveness check: decay must actually run and write updated heat values
        back to the embedded DB. Pre-fix: crash before reaching this point.
        """
        mid_a = _insert_memory_embedded(
            embedded_storage,
            content="old non-anchored memory A -- embedded decay test",
            heat=0.9,
            last_accessed=_THREE_DAYS_AGO,
        )
        mid_b = _insert_memory_embedded(
            embedded_storage,
            content="old non-anchored memory B -- embedded decay test",
            heat=0.85,
            last_accessed=_THREE_DAYS_AGO,
        )

        stats = {"memories_updated": 0, "memories_archived": 0}
        scheduler._apply_decay(stats)

        heat_a = _read_heat(embedded_storage, mid_a)
        heat_b = _read_heat(embedded_storage, mid_b)

        assert heat_a < 0.9, (
            f"mem_old_a heat should have decayed below 0.9 (seeded), got {heat_a}. "
            "Decay did not run on the embedded path."
        )
        assert heat_b < 0.85, (
            f"mem_old_b heat should have decayed below 0.85 (seeded), got {heat_b}. "
            "Decay did not run on the embedded path."
        )

    def test_protected_anchor_heat_is_preserved(self, embedded_storage, scheduler):
        """Protected (anchored) memory must not be decayed.

        get_all_memories_for_decay excludes is_protected=true rows; this is a
        critical invariant -- anchor memories must never be silently decayed away.
        """
        mid_c = _insert_memory_embedded(
            embedded_storage,
            content="protected anchor memory C -- embedded decay test",
            heat=0.5,
            last_accessed=_THREE_DAYS_AGO,  # old -- would decay if not protected
            is_protected=True,
            tier="conditional",
        )

        stats = {"memories_updated": 0, "memories_archived": 0}
        scheduler._apply_decay(stats)

        heat_c = _read_heat(embedded_storage, mid_c)
        assert heat_c == pytest.approx(0.5, abs=1e-6), (
            f"Protected anchor heat must not change; expected 0.5 got {heat_c}."
        )

    def test_fresh_memory_heat_barely_changes(self, embedded_storage, scheduler):
        """Memory accessed RIGHT NOW has negligible heat change after one decay pass.

        DECAY_FACTOR=0.9995 per hour; 0 hours elapsed => factor^0 = 1.0 => no change.
        """
        mid_d = _insert_memory_embedded(
            embedded_storage,
            content="fresh memory D just accessed -- embedded decay test",
            heat=0.6,
            last_accessed=_NOW,
        )

        stats = {"memories_updated": 0, "memories_archived": 0}
        scheduler._apply_decay(stats)

        heat_d = _read_heat(embedded_storage, mid_d)
        # Allow small floating-point drift but heat must be very close to 0.6.
        assert heat_d == pytest.approx(0.6, abs=0.01), (
            f"Fresh memory heat should barely change; expected ~0.6 got {heat_d}."
        )

    def test_decay_updates_stats_counter(self, embedded_storage, scheduler):
        """_apply_decay must increment stats['memories_updated'] for changed rows."""
        _insert_memory_embedded(
            embedded_storage,
            content="stats counter check -- embedded decay test",
            heat=0.9,
            last_accessed=_THREE_DAYS_AGO,
        )

        stats = {"memories_updated": 0, "memories_archived": 0}
        scheduler._apply_decay(stats)

        assert stats["memories_updated"] >= 1, (
            f"Expected at least 1 memory updated in stats, got {stats['memories_updated']}. "
            "Decay cycle either did not run or did not write back results."
        )

    def test_embedded_storage_engine_init_matches_nightly_cycle_pattern(
        self, tmp_path, monkeypatch
    ):
        """StorageEngine can be opened in embedded mode via the nightly_cycle pattern.

        nightly_cycle.py line 149-152 explicitly pops YADGAR_DB_URL and opens
        StorageEngine(str(db_path)) in embedded mode. This test reproduces that
        exact call and verifies it does NOT raise.

        Pre-fix: StorageEngine.__init__ raised a SurrealDB parse error on the
        FULLTEXT DEFINE statement inside _init_schema(), so the nightly step
        crashed immediately (returned exit code 30 — or worse, killed the cycle).
        Post-fix: init succeeds; ConsolidationScheduler can be instantiated.

        This test is the closest behavioral analog to what the nightly systemd
        timer actually does on every invocation.
        """
        from yadgar.consolidation import ConsolidationScheduler as _CS
        from yadgar.embeddings import EmbeddingEngine as _EE

        monkeypatch.delenv("YADGAR_DB_URL", raising=False)
        get_settings.cache_clear()

        db_path = str(tmp_path / "nightly_pattern_test.db")
        settings = get_settings()

        # Mirror nightly_cycle.py _step_consolidation exactly:
        storage = StorageEngine(db_path)  # Was the crash site pre-fix
        try:
            embeddings = _EE()
            scheduler = _CS(storage, embeddings, settings)
            # Scheduler was successfully constructed -- the rest (force_consolidate)
            # may still fail on other embedded-mode limitations, but INIT succeeded.
            assert scheduler is not None
            assert storage._db_url is None, "must be embedded mode"
        finally:
            storage.close()


class TestDecayIdempotency:
    """Decay must NOT compound across repeated cycles for unaccessed memories.

    THE BUG (pre-fix): the decay UPDATE wrote only `heat` (+ access counter), never
    advancing a decay watermark.  Each cycle recomputed `now - last_accessed` (which
    only moves on *access*) and multiplied that full elapsed decay onto the already-
    decayed heat.  For an unaccessed memory, the exponent grew every cycle while heat
    kept shrinking -> quadratic over-decay (a 20-day-untouched memory lands at ~0.08
    instead of ~0.79).  Nightly runs effectively killed cold memories in ~2-3 weeks
    vs the configured ~2-month half-life.

    THE FIX: persist `last_decay_at` and decay over `now - max(last_accessed,
    last_decay_at)`, advancing the watermark each write.  Decay becomes idempotent:
    running it twice back-to-back applies the true (near-zero) elapsed time on the
    second pass, not the whole age again.

    None of the single-run E2E tests above catch this -- only a repeated-run test does.
    """

    def test_second_decay_pass_is_near_noop_without_access(self, embedded_storage, scheduler):
        """Two back-to-back decay passes (no access between) must not double-decay.

        Pre-fix: pass 2 re-applies the full ~10-day decay -> heat2 << heat1 (RED).
        Post-fix: pass 2 sees ~0 elapsed since last_decay_at -> heat2 ~= heat1 (GREEN).
        """
        ten_days_ago = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        mid = _insert_memory_embedded(
            embedded_storage,
            content="unaccessed memory -- decay idempotency guard",
            heat=0.9,
            last_accessed=ten_days_ago,
        )

        stats = {"memories_updated": 0, "memories_archived": 0}
        scheduler._apply_decay(stats)
        heat_after_first = _read_heat(embedded_storage, mid)

        # Sanity: the first pass DID decay (10 days elapsed).
        assert heat_after_first < 0.9, (
            f"first decay pass should reduce heat from 0.9, got {heat_after_first}"
        )

        # Second pass immediately after, with NO intervening access.
        scheduler._apply_decay(stats)
        heat_after_second = _read_heat(embedded_storage, mid)

        # The only elapsed time between the two passes is microseconds, so the
        # second pass must barely move heat.  The bug makes it drop by another
        # ~10 days' worth of decay.
        assert heat_after_second == pytest.approx(heat_after_first, abs=0.005), (
            f"decay compounded across cycles: heat went {heat_after_first} -> "
            f"{heat_after_second} on a second pass with no access. Decay is not "
            "idempotent -- last_decay_at watermark missing or not honored."
        )


class TestNightlyCycleEmbedded:
    """The REAL nightly path: force_consolidate() end-to-end in EMBEDDED mode.

    Pre-v5.63 the nightly cycle (host nightly_cycle.py opens StorageEngine
    embedded) FAILED every run with exit 30:
    - `batch_writes` raised RuntimeError (server-only) on any non-empty decay
      batch -> killed consolidation;
    - `insert_consolidation_log` (fires every cycle) and `insert_astrocyte_process`
      (scheduler init) emit `type::record('t', $id)` with an INTEGER id, which the
      embedded SurrealDB Python SDK rejects ("second argument must be a table name
      or a string"). The astrocyte failure left the engram empty (the
      `engram_slot has 0 rows` check_invariants violation).

    v5.63 fixes both at the embedded transport layer (client.py:
    `_inline_int_record_ids` + embedded `batch_writes` loop). These tests exercise
    the REAL path with NO patching — they were RED before the fix.
    """

    def test_force_consolidate_completes_embedded(self, embedded_storage, scheduler):
        _insert_memory_embedded(
            embedded_storage, "nightly cycle mem A", heat=0.9, last_accessed=_THREE_DAYS_AGO
        )
        _insert_memory_embedded(
            embedded_storage, "nightly cycle mem B", heat=0.85, last_accessed=_THREE_DAYS_AGO
        )
        # Must NOT raise — this was the nightly exit-30 failure.
        scheduler.force_consolidate()
        # insert_consolidation_log fires every cycle via type::record (Mode B).
        rows = embedded_storage._q("SELECT id FROM consolidation_log")
        assert rows, (
            "consolidation_log row not written — insert_consolidation_log "
            "(type::record) failed in embedded mode"
        )

    def test_astrocyte_init_writes_embedded(self, embedded_storage):
        # ConsolidationScheduler construction -> AstrocytePool.init_processes ->
        # insert_astrocyte_process (type::record). Pre-fix this failed embedded,
        # leaving the engram empty (the engram_slot=0 invariant violation source).
        from yadgar.config import get_settings as _gs
        from yadgar.consolidation import ConsolidationScheduler as _CS
        from yadgar.embeddings import EmbeddingEngine as _EE

        _CS(embedded_storage, _EE(), _gs())
        rows = embedded_storage._q("SELECT id FROM astrocyte_process")
        assert rows, (
            "astrocyte_process rows not written — astrocyte init (type::record) "
            "failed in embedded mode"
        )
