"""Tests for check_invariants auto-repair: memory_transition, engram slot rebalancer,
caused_by dangling edges, and per-table size telemetry.

TDD: tests written before implementation — they will fail until fixes land.
"""

import pytest

from yadgar import server
from yadgar.config import Settings
from yadgar.engram import EngramAllocator
from yadgar.server import _run_check_invariants
from yadgar.storage import StorageEngine

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


# ── Item 6: caused_by dangling edge pruning ───────────────────────────────────


def _insert_entity(storage: StorageEngine, name: str) -> int:
    """Insert a minimal entity and return its integer ID."""
    return storage.insert_entity(
        {
            "name": name,
            "type": "concept",
            "description": f"test entity {name}",
        }
    )


def _insert_caused_by(storage: StorageEngine, src_id: int, tgt_id: int) -> int:
    """Insert a caused_by relationship and return its integer ID."""
    return storage.insert_relationship(
        {
            "source_entity_id": src_id,
            "target_entity_id": tgt_id,
            "relationship_type": "caused_by",
            "weight": 1.0,
        }
    )


def test_caused_by_dangling_edges_auto_repaired():
    """check_invariants must delete dangling caused_by rows and report them in 'fixed'.

    Fixture: 10 caused_by relationships — 6 valid, 4 pointing to deleted entities.
    After repair: 4 deleted, 6 remain.
    """
    storage = server._get_storage()

    # Create 6 valid entities and 6 valid caused_by edges
    valid_eids = [_insert_entity(storage, f"entity_valid_{i}") for i in range(6)]
    for i in range(5):
        _insert_caused_by(storage, valid_eids[i], valid_eids[i + 1])
    # One self-loop to reach 6 valid rows
    _insert_caused_by(storage, valid_eids[0], valid_eids[5])

    # Create 4 entities, insert caused_by edges, then delete the entities to create dangles
    dangling_eids = [_insert_entity(storage, f"entity_dangling_{i}") for i in range(4)]
    for did in dangling_eids:
        _insert_caused_by(storage, valid_eids[0], did)
    # Delete the target entities to create dangling references
    for did in dangling_eids:
        storage._q("DELETE type::record('entity', $id)", {"id": did})

    # Verify 10 caused_by rows exist before repair
    before_count = storage._q(
        "SELECT count() AS c FROM relationship WHERE relationship_type = 'caused_by' GROUP ALL"
    )
    before_n = int(before_count[0]["c"]) if before_count else 0
    assert before_n == 10, f"setup failed: expected 10 caused_by rows, got {before_n}"

    result = _run_check_invariants(storage)

    # 4 dangling rows deleted, 6 remain
    after_rows = storage._q("SELECT * FROM relationship WHERE relationship_type = 'caused_by'")
    assert len(after_rows) == 6, f"expected 6 caused_by rows after repair, got {len(after_rows)}"

    # Repair must be reported in 'fixed'
    assert any("caused_by" in f for f in result["fixed"]), (
        f"expected caused_by repair in 'fixed', got: {result['fixed']}"
    )

    # Must NOT appear in violations
    all_violations = result.get("violations", []) + result.get("warn_violations", [])
    cb_violations = [v for v in all_violations if "caused_by" in v]
    assert len(cb_violations) == 0, (
        f"dangling caused_by should NOT be in violations after auto-repair, got: {cb_violations}"
    )


# ── Item 7: per-table size breakdown ─────────────────────────────────────────


def test_per_table_size_in_check_invariants():
    """check_invariants must include per_table size breakdown in db_size.

    per_table["memory"]["rows"] must match SELECT count() FROM memory.
    """
    storage = server._get_storage()

    # Insert a few memories so the table is non-empty
    import struct

    embedding = struct.pack("<384f", *([0.0] * 384))
    for i in range(3):
        storage.insert_memory(
            {
                "content": f"per-table test memory {i}",
                "embedding": embedding,
                "tags": ["test"],
                "directory_context": "/tmp/test_per_table",
                "heat": 1.0,
                "is_stale": False,
                "embedding_model": "all-MiniLM-L6-v2",
            }
        )

    result = _run_check_invariants(storage)

    assert "db_size" in result, f"db_size missing from result: {list(result.keys())}"
    db_size = result["db_size"]
    assert "per_table" in db_size, f"per_table missing from db_size: {list(db_size.keys())}"

    per_table = db_size["per_table"]
    assert "memory" in per_table, f"memory missing from per_table: {list(per_table.keys())}"

    # Row count must match a direct query
    direct_count_rows = storage._q("SELECT count() AS c FROM memory GROUP ALL")
    direct_count = int(direct_count_rows[0]["c"]) if direct_count_rows else 0
    assert per_table["memory"]["rows"] == direct_count, (
        f"per_table memory rows mismatch: got {per_table['memory']['rows']}, "
        f"direct count={direct_count}"
    )

    # Each table entry must have at least 'rows'
    for table, info in per_table.items():
        assert "rows" in info, f"per_table[{table!r}] missing 'rows' key"


def test_per_table_size_in_memory_stats(tmp_path, monkeypatch):
    """memory_stats() must include per_table in its db_size block."""
    db_dir = tmp_path / "surreal_db"
    db_dir.mkdir(parents=True, exist_ok=True)
    (db_dir / "vlog").mkdir()
    (db_dir / "vlog" / "x.vlog").write_bytes(b"\x00" * 1024)

    from yadgar import server as _s

    monkeypatch.setattr(_s.settings, "DB_PATH", str(db_dir), raising=False)

    stats = server.memory_stats()
    assert "db_size" in stats, f"db_size missing from memory_stats: {list(stats.keys())}"
    assert "per_table" in stats["db_size"], (
        f"per_table missing from memory_stats db_size: {list(stats['db_size'].keys())}"
    )


# ── Fix H1: caused_by ceiling-prune removes oldest rows ──────────────────────


def test_caused_by_row_count_ceiling_prunes_oldest(monkeypatch):
    """check_invariants must prune the oldest caused_by rows when the ceiling is exceeded.

    Strategy:
    - Monkeypatch MAX_CAUSED_BY_ROWS to 5.
    - Insert 7 caused_by relationships with distinct, increasing created_at timestamps.
    - Run _run_check_invariants.
    - Assert: count drops from 7 to 5 (2 oldest removed).
    - 'fixed' must contain a string with "Pruned 2" and "caused_by".
    """
    import datetime

    from yadgar import server as _s

    monkeypatch.setattr(_s.settings, "MAX_CAUSED_BY_ROWS", 5, raising=True)

    storage = server._get_storage()

    # Two live entities to anchor all relationships
    e1 = _insert_entity(storage, "ceiling_e1")
    e2 = _insert_entity(storage, "ceiling_e2")

    base_time = datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC)
    # Insert 7 caused_by rows with distinct, increasing created_at so ordering is deterministic
    for i in range(7):
        ts = (base_time + datetime.timedelta(hours=i)).isoformat()
        storage.insert_relationship(
            {
                "source_entity_id": e1,
                "target_entity_id": e2,
                "relationship_type": "caused_by",
                "weight": 1.0,
                "created_at": ts,
            }
        )

    before = storage._q(
        "SELECT count() AS c FROM relationship WHERE relationship_type = 'caused_by' GROUP ALL"
    )
    before_n = int(before[0]["c"]) if before else 0
    assert before_n == 7, f"setup failed: expected 7 caused_by rows, got {before_n}"

    result = _run_check_invariants(storage)

    # 2 oldest pruned → 5 remain
    after = storage._q(
        "SELECT count() AS c FROM relationship WHERE relationship_type = 'caused_by' GROUP ALL"
    )
    after_n = int(after[0]["c"]) if after else 0
    assert after_n == 5, f"expected 5 caused_by rows after ceiling prune, got {after_n}"

    # 'fixed' must mention "Pruned 2" and "caused_by"
    prune_msgs = [f for f in result.get("fixed", []) if "caused_by" in f.lower()]
    assert prune_msgs, (
        f"expected caused_by prune message in 'fixed', got: {result.get('fixed', [])}"
    )
    assert any("Pruned 2" in m for m in prune_msgs), (
        f"expected 'Pruned 2' in fixed messages, got: {prune_msgs}"
    )

    # Verify the OLDEST 2 rows were pruned (the policy guarantee).
    # base_time + 0h and +1h must be gone; +2h..+6h must remain.
    remaining = storage._q(
        "SELECT created_at FROM relationship WHERE relationship_type = 'caused_by'"
    )
    remaining_ts = sorted(str(r["created_at"]) for r in remaining)
    expected_remaining = sorted(
        (base_time + datetime.timedelta(hours=i)).isoformat() for i in range(2, 7)
    )
    assert remaining_ts == expected_remaining, (
        f"oldest-semantics violated: expected {expected_remaining}, "
        f"got {remaining_ts} — ORDER BY created_at ASC likely flipped"
    )
