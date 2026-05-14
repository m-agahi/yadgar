"""Tests for check_invariants auto-repair: memory_transition and engram slot rebalancer.

TDD: tests written before implementation — they will fail until fixes land.
"""

import pytest

from yadgar import server
from yadgar.config import Settings
from yadgar.engram import EngramAllocator
from yadgar.server import _run_check_invariants
from yadgar.storage import StorageEngine

pytestmark = pytest.mark.xdist_group("server_globals")


# ── Shared fixture ────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _engines(tmp_path):
    """Initialize global engines with a temp database for each test."""
    db_path = str(tmp_path / "test.db")
    server.init_engines(db_path=db_path, embedding_model="all-MiniLM-L6-v2")
    yield
    server.shutdown()


# ── Fix 1: dangling memory_transition auto-repair ────────────────────────────


def test_dangling_memory_transition_auto_repaired():
    """check_invariants must delete dangling memory_transition rows and report them in 'fixed'.

    A memory_transition row whose from_memory_id or to_memory_id references a
    non-existent memory is a useless orphan — safe to delete.  Previously the
    code added it to warn_violations; after the fix it must be in 'fixed'.
    """
    storage = server._get_storage()

    # Insert a dangling memory_transition row using IDs that don't exist
    non_existent_from = 9000001
    non_existent_to = 9000002
    storage._q(
        "CREATE type::record('memory_transition', $id) SET "
        "from_memory_id = $from_id, to_memory_id = $to_id, count = 1, "
        "last_transition = '2024-01-01T00:00:00Z', session_id = 'test'",
        {"id": 99998, "from_id": non_existent_from, "to_id": non_existent_to},
    )

    # Verify the row exists before running check_invariants
    before = storage._q(
        "SELECT * FROM memory_transition WHERE from_memory_id = $fid",
        {"fid": non_existent_from},
    )
    assert len(before) >= 1, "test setup failed: dangling row not inserted"

    result = _run_check_invariants(storage)

    # The row should have been deleted
    after = storage._q(
        "SELECT * FROM memory_transition WHERE from_memory_id = $fid",
        {"fid": non_existent_from},
    )
    assert len(after) == 0, "dangling memory_transition row was NOT deleted by check_invariants"

    # The fix must be reported in 'fixed', not in 'violations'
    assert any("memory_transition" in f for f in result["fixed"]), (
        f"expected memory_transition repair in 'fixed', got: {result['fixed']}"
    )

    # Must NOT appear in violations or warn_violations
    all_violations = result.get("violations", []) + result.get("warn_violations", [])
    mt_violations = [v for v in all_violations if "memory_transition" in v]
    assert len(mt_violations) == 0, (
        f"dangling memory_transition should NOT be in violations after auto-repair, "
        f"got: {mt_violations}"
    )


# ── Fix 2: engram slot rebalancer ────────────────────────────────────────────


@pytest.fixture
def small_settings(tmp_path):
    """Settings with 20 slots so we can easily exceed the 5% threshold."""
    return Settings(
        DB_PATH=str(tmp_path / "engram_rebal.db"),
        HOPFIELD_MAX_PATTERNS=20,
        EXCITABILITY_HALF_LIFE_HOURS=6.0,
        EXCITABILITY_BOOST=0.5,
    )


@pytest.fixture
def engram_storage(tmp_path):
    engine = StorageEngine(str(tmp_path / "engram_rebal.db"))
    yield engine
    engine.close()


def _insert_bare_memory(storage: StorageEngine, content: str) -> int:
    """Insert a memory with a zero embedding (fast, for slot-packing tests)."""
    import struct

    # 384-dim zero vector packed as little-endian float32 bytes
    embedding = struct.pack("<384f", *([0.0] * 384))
    return storage.insert_memory(
        {
            "content": content,
            "embedding": embedding,
            "tags": ["test"],
            "directory_context": "/tmp/test",
            "heat": 1.0,
            "is_stale": False,
            "embedding_model": "all-MiniLM-L6-v2",
        }
    )


def test_engram_rebalance_if_needed_redistributes_overloaded_slot(engram_storage, small_settings):
    """rebalance_if_needed() must move memories out of a slot exceeding the threshold.

    With 20 slots and threshold=5%, max per slot = max(1, int(total * 0.05)).
    We insert 20 memories and pack 4 of them into slot 0 (20% > 5%).
    After rebalance, slot 0 should hold at most threshold memories.
    """
    allocator = EngramAllocator(engram_storage, small_settings)

    total = 20
    mids = [_insert_bare_memory(engram_storage, f"memory {i}") for i in range(total)]

    # Assign 4 memories to slot 0 — that's 4/20 = 20%, well above 5%
    for mid in mids[:4]:
        engram_storage.assign_memory_slot(mid, 0)
    # Distribute the rest across other slots (one each into slots 1..16)
    for i, mid in enumerate(mids[4:], start=1):
        engram_storage.assign_memory_slot(mid, i)

    # Verify setup: slot 0 is over-occupied
    occupancy_before = engram_storage.get_slot_occupancy()
    assert occupancy_before[0] == 4, f"setup failed: slot 0 has {occupancy_before[0]} memories"

    moved = allocator.rebalance_if_needed(threshold_pct=0.05)

    assert moved > 0, "rebalance_if_needed() returned 0 but slot 0 was over-occupied"

    occupancy_after = engram_storage.get_slot_occupancy()
    threshold = max(1, int(total * 0.05))
    assert occupancy_after.get(0, 0) <= threshold, (
        f"slot 0 still has {occupancy_after.get(0, 0)} memories after rebalance "
        f"(threshold={threshold})"
    )


def test_engram_rebalance_noop_when_balanced(engram_storage, small_settings):
    """rebalance_if_needed() must return 0 when all slots are within the threshold.

    With 20 slots and 20 memories (1 each), no slot exceeds 5% of 20 = 1.
    """
    allocator = EngramAllocator(engram_storage, small_settings)

    total = 20
    mids = [_insert_bare_memory(engram_storage, f"balanced memory {i}") for i in range(total)]
    # Distribute evenly — one memory per slot
    for i, mid in enumerate(mids):
        engram_storage.assign_memory_slot(mid, i)

    moved = allocator.rebalance_if_needed(threshold_pct=0.05)

    assert moved == 0, f"rebalance_if_needed() moved {moved} memories but distribution was balanced"


def test_check_invariants_engram_rebalance_wired(tmp_path):
    """check_invariants must call rebalance_if_needed and report moves in 'fixed'.

    We pack slot 0 past the 5% threshold via direct DB writes, then run
    _run_check_invariants and assert either:
    - the rebalancer fired and 'fixed' contains an engram entry, OR
    - the slot is now within threshold (i.e. no violation remains).
    """
    db_path = str(tmp_path / "ci_rebal.db")
    server.shutdown()
    server.init_engines(db_path=db_path, embedding_model="all-MiniLM-L6-v2")
    storage = server._get_storage()

    total = 40
    mids = [_insert_bare_memory(storage, f"ci rebal {i}") for i in range(total)]
    # Put 8 memories in slot 0 — 8/40 = 20%, well above 5% threshold
    for mid in mids[:8]:
        storage.assign_memory_slot(mid, 0)
    for i, mid in enumerate(mids[8:], start=1):
        storage.assign_memory_slot(
            mid,
            i
            % (storage._q("SELECT count() AS c FROM engram_slot GROUP ALL") or [{"c": 20}])[0].get(
                "c", 20
            ),
        )

    result = _run_check_invariants(storage)

    # Either rebalancer fired (reported in fixed) or slot is no longer over-threshold
    engram_fixed = [
        f for f in result.get("fixed", []) if "engram" in f.lower() or "slot" in f.lower()
    ]
    occupancy_after = storage.get_slot_occupancy()
    threshold = max(1, int(total * 0.05))
    slot0_ok = occupancy_after.get(0, 0) <= threshold

    assert engram_fixed or slot0_ok, (
        f"check_invariants did not rebalance slot 0: "
        f"slot0 count={occupancy_after.get(0, 0)}, threshold={threshold}, "
        f"fixed={result.get('fixed', [])}, violations={result.get('violations', [])}"
    )
