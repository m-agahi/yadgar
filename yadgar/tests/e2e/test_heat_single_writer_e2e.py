"""E2E test for BC-CSW1 — heat single-writer invariant (v5.75).

BC-CSW1: One consolidation cycle MUST issue exactly ONE storage.batch_writes
call for all heat mutations (memories + entities combined).  No phase other
than HeatWriter.apply_heat_intents may write heat during a cycle.

This test runs a REAL force_consolidate() cycle against an isolated SurrealDB
instance, spies on storage.batch_writes with wraps= (so the real DB write
executes), then asserts:

  1. Exactly ONE call to storage.batch_writes carries heat-decay statements
     (identified by the unique signature ``last_decay_at = $now`` written only
     by heat_decay.py).
  2. That single call contains BOTH memory and entity update statements
     (combined batch — not two separate calls).
  3. Heat actually decayed in the DB: post-cycle heat < pre-seeded heat
     for both a memory row and an entity row.

Clause 2 of the contract ("no OTHER phase writes heat") is covered structurally:
force_consolidate() runs ALL phases (decay, episodes, merge, graph, etc.) and
the spy records every batch_writes call — so any rogue heat write by another
phase would show up as an extra batch carrying ``last_decay_at``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.e2e

# Sentinel token unique to this test file — never changes; used in assertion messages.
_TOKEN = "xbccsw1e2e"


def _insert_test_memory(e2e_engines, label: str, heat: float, hours_old: float) -> int:
    """Seed a memory that will survive purge but has old enough last_accessed to decay."""
    storage = e2e_engines["storage"]
    embeddings = e2e_engines["embeddings"]
    yadgar_dir = e2e_engines["yadgar_dir"]

    last_accessed = (datetime.now(UTC) - timedelta(hours=hours_old)).isoformat()
    now = datetime.now(UTC).isoformat()
    content = f"BC-CSW1 e2e heat decay test {label} {_TOKEN} unique content"
    emb = embeddings.encode(content)

    doc = {
        "content": content,
        "embedding": emb,
        "directory_context": yadgar_dir,
        "heat": heat,
        "tags": [],  # NOT auto-abstracted — survives purge
        "last_accessed": last_accessed,
        "created_at": now,
        "access_count": 3,  # recently used — spared from old-unaccessed purge
        "is_protected": False,
        "tier": None,
    }
    return storage.insert_memory(doc)


def _insert_test_entity(e2e_engines, label: str, heat: float, hours_old: float) -> int:
    """Seed an entity that will decay during the cycle."""
    storage = e2e_engines["storage"]
    last_accessed = (datetime.now(UTC) - timedelta(hours=hours_old)).isoformat()
    now = datetime.now(UTC).isoformat()

    eid = storage.insert_entity(
        {
            "name": f"BCCSw1Entity_{label}_{_TOKEN}",
            "type": "test_entity",
            "heat": heat,
            "last_accessed": last_accessed,
            "created_at": now,
            "archived": False,
        }
    )
    return eid


class TestBCCSW1_HeatSingleWriterE2E:
    """BC-CSW1 e2e: one cycle → exactly ONE batch_writes for all heat mutations."""

    def test_single_batch_writes_for_heat_real_cycle(self, e2e_engines):
        """force_consolidate() against a real DB MUST issue exactly ONE batch_writes
        carrying heat-decay statements (last_decay_at), covering both memories and
        entities combined, and heat MUST have actually decreased in the DB.

        White-box spy: patch storage.batch_writes with wraps=real so the real DB
        write happens AND we can count/inspect calls.
        """
        import yadgar.server._state as _st

        storage = e2e_engines["storage"]
        consolidation = _st._consolidation
        assert consolidation is not None, "ConsolidationScheduler must be initialized"
        # Confirm the consolidation scheduler uses the same storage instance the
        # e2e_engines fixture wired up (so our spy patches the right object).
        assert consolidation._storage is storage, (
            "BC-CSW1 e2e: consolidation._storage must be the same StorageEngine "
            "as e2e_engines['storage']; spy would miss calls otherwise"
        )

        # Seed a memory + entity with heat=0.9, last_accessed 72 hours ago.
        # 72 h old → DECAY_FACTOR^72 ≈ meaningful drop; well above COLD_THRESHOLD.
        mid = _insert_test_memory(e2e_engines, "mem1", heat=0.9, hours_old=72)
        eid = _insert_test_entity(e2e_engines, "ent1", heat=0.85, hours_old=72)

        # Confirm seeds exist with initial heat before cycle
        pre_mem = storage.get_memory(mid)
        assert pre_mem is not None, f"BC-CSW1: seeded memory {mid} not found before cycle"
        pre_mem_heat = float(pre_mem["heat"])

        pre_ent_row = storage.get_entity_by_id(eid)
        assert pre_ent_row is not None, f"BC-CSW1: seeded entity {eid} not found before cycle"
        pre_ent_heat = float(pre_ent_row["heat"])

        # --- Spy on batch_writes via wraps= so the real DB write still executes ---
        call_log: list[list[tuple]] = []

        real_batch_writes = storage.batch_writes

        def _spy(statements):
            call_log.append(list(statements))
            return real_batch_writes(statements)

        with patch.object(storage, "batch_writes", side_effect=_spy):
            # Run the FULL consolidation cycle — all phases execute.
            # This covers clause 2: rogue heat writes by any other phase would
            # appear as extra batches containing ``last_decay_at``.
            consolidation.force_consolidate()

        # --- Assertion 1: exactly ONE batch_writes call carried decay statements ---
        # Decay signature: ``last_decay_at = $now`` written only in heat_decay.py.
        def _is_heat_decay_batch(stmts: list[tuple]) -> bool:
            return any("last_decay_at" in sql for sql, _ in stmts)

        heat_batches = [b for b in call_log if _is_heat_decay_batch(b)]

        assert len(heat_batches) == 1, (
            f"BC-CSW1 e2e FAIL — expected exactly 1 batch_writes call containing "
            f"heat-decay statements (last_decay_at), got {len(heat_batches)}. "
            f"Total batch_writes calls: {len(call_log)}. "
            f"Batch sizes: {[len(b) for b in call_log]}. "
            f"Heat batch sizes: {[len(b) for b in heat_batches]}. "
            "A non-1 count means either the single-writer refactor regressed "
            "(>1) or no decay ran at all (0)."
        )

        # --- Assertion 2: that single batch contains BOTH memory AND entity statements ---
        the_batch = heat_batches[0]
        sqls = [sql for sql, _ in the_batch]

        has_memory_stmt = any("memory" in sql for sql in sqls)
        has_entity_stmt = any("entity" in sql for sql in sqls)

        assert has_memory_stmt, (
            f"BC-CSW1 e2e FAIL — the single heat-decay batch must contain a memory "
            f"UPDATE statement. Got SQLs: {sqls[:5]!r}"
        )
        assert has_entity_stmt, (
            f"BC-CSW1 e2e FAIL — the single heat-decay batch must contain an entity "
            f"UPDATE statement (combined batch, not two separate calls). "
            f"Got SQLs: {sqls[:5]!r}"
        )

        # --- Assertion 3: heat actually decayed in the DB (real write happened) ---
        post_mem = storage.get_memory(mid)
        assert post_mem is not None, (
            f"BC-CSW1 e2e: seeded memory {mid} missing after cycle "
            "(purged unexpectedly — check seed parameters)"
        )
        post_mem_heat = float(post_mem["heat"])
        assert post_mem_heat < pre_mem_heat, (
            f"BC-CSW1 e2e FAIL — memory heat did NOT decay. "
            f"pre={pre_mem_heat:.6f} post={post_mem_heat:.6f}. "
            "Either batch_writes did not execute (wraps= broken) or decay formula is wrong."
        )

        post_ent = storage.get_entity_by_id(eid)
        assert post_ent is not None, f"BC-CSW1 e2e: seeded entity {eid} missing after cycle"
        post_ent_heat = float(post_ent["heat"])
        assert post_ent_heat < pre_ent_heat, (
            f"BC-CSW1 e2e FAIL — entity heat did NOT decay. "
            f"pre={pre_ent_heat:.6f} post={post_ent_heat:.6f}."
        )
