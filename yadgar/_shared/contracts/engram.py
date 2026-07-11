"""Engram allocation — competitive memory slot storage based on excitability.

Implements the Josselyn & Frankland (2007) / Rashid et al. (2016) model:
neurons (slots) compete via CREB-like excitability. High-excitability slots
win the competition and memories stored nearby in time share the same slot,
creating automatic temporal linking with zero explicit logic.
"""

import logging
import time
from datetime import UTC, datetime

from yadgar._shared.config import Settings
from yadgar._shared.observability.observe import observe
from yadgar._shared.storage import StorageEngine

logger = logging.getLogger(__name__)


class EngramAllocator:
    """Competitive memory slot allocator with excitability-based temporal linking."""

    def __init__(self, storage: StorageEngine, settings: Settings) -> None:
        self._storage = storage
        self._settings = settings
        self._num_slots = settings.HOPFIELD_MAX_PATTERNS
        self._half_life = settings.EXCITABILITY_HALF_LIFE_HOURS
        self._boost = settings.EXCITABILITY_BOOST
        # Ensure slot table is populated
        self._storage.init_engram_slots(self._num_slots)

    # At/above this decayed-excitability level a slot is considered "warm" — a
    # temporal cluster is currently active there. With a 0.5 boost and a ~6h
    # half-life, a slot stays warm for roughly 3 half-lives (~18h) after its
    # last activation, then goes cold and the next memory starts a new cluster.
    _WARM_THRESHOLD = 0.05

    @observe(tier="boundary", metric="engram.allocate")
    def allocate(self, memory_id: int) -> dict:
        """Allocate a memory to a slot.

        If a warm slot exists (a temporal cluster is currently active), the
        memory joins it — temporally close memories share a slot. Otherwise this
        starts a *new* cluster in the least-occupied slot, so clusters spread
        across the slot table instead of all piling into slot 0.

        Returns dict with slot_index, excitability, temporally_linked IDs, and link_count.
        """
        # PR-E: bracket allocate duration
        _t0 = time.perf_counter()
        try:
            return self._allocate_inner(memory_id)
        finally:
            try:
                from yadgar._shared.observability.metrics import (
                    yadgar_engram_allocate_duration_ms,  # noqa: PLC0415
                )

                yadgar_engram_allocate_duration_ms.observe((time.perf_counter() - _t0) * 1000)
            except Exception:
                pass

    @observe(tier="stage", metric="engram.allocate_inner")
    def _allocate_inner(self, memory_id: int) -> dict:
        """Inner implementation of allocate()."""
        all_slots = self._storage.get_all_engram_slots()
        occupancy = self._storage.get_slot_occupancy()

        exc_by_slot = {
            slot["slot_index"]: self._compute_decayed_excitability(
                slot["excitability"], slot.get("last_activated")
            )
            for slot in all_slots
        }
        best_excitability = max(exc_by_slot.values(), default=0.0)

        if best_excitability >= self._WARM_THRESHOLD:
            # Continue the active temporal cluster. Among slots tied at the max
            # excitability, prefer the least-occupied, then lowest index.
            candidates = [
                idx for idx, exc in exc_by_slot.items() if exc >= best_excitability - 1e-9
            ]
        else:
            # Cold start — begin a new cluster. All slots are candidates; the
            # least-occupied (an empty slot if any) wins.
            candidates = list(exc_by_slot) or [0]

        best_slot = min(candidates, key=lambda idx: (occupancy.get(idx, 0), idx))

        # Get memories already in this slot (these are temporally linked)
        existing_memories = self._storage.get_memories_in_slot(best_slot)
        linked_ids = [m["id"] for m in existing_memories if m["id"] != memory_id]

        # Assign memory to the winning slot
        self._storage.assign_memory_slot(memory_id, best_slot)

        # Boost the winning slot's excitability
        new_excitability = self.boost_excitability(best_slot)

        # Apply lateral inhibition to adjacent slots
        self.apply_lateral_inhibition(best_slot)

        # Update the memory's excitability field to match slot
        self._storage.update_memory_excitability(memory_id, new_excitability)

        return {
            "slot_index": best_slot,
            "excitability": round(new_excitability, 4),
            "temporally_linked": linked_ids,
            "link_count": len(linked_ids),
        }

    def get_excitability(self, slot_index: int) -> float:
        """Get the current decayed excitability for a slot."""
        slot = self._storage.get_engram_slot(slot_index)
        if slot is None:
            return 0.0
        return self._compute_decayed_excitability(slot["excitability"], slot.get("last_activated"))

    @observe(tier="stage", metric="engram.boost_excitability")
    def boost_excitability(self, slot_index: int) -> float:
        """Boost a slot's excitability by EXCITABILITY_BOOST, capped at 1.0."""
        current = self.get_excitability(slot_index)
        new_exc = min(current + self._boost, 1.0)
        now = self._storage._now_iso()
        self._storage.update_engram_slot(slot_index, new_exc, now)
        return new_exc

    @observe(tier="stage", metric="engram.get_temporally_linked")
    def get_temporally_linked(self, memory_id: int) -> list[int]:
        """Return all other memory IDs in the same slot as this memory."""
        mem = self._storage.get_memory(memory_id)
        if mem is None or mem.get("slot_index") is None:
            return []
        memories = self._storage.get_memories_in_slot(mem["slot_index"])
        return [m["id"] for m in memories if m["id"] != memory_id]

    @observe(tier="stage", metric="engram.apply_lateral_inhibition")
    def apply_lateral_inhibition(self, activated_slot: int) -> None:
        """Reduce excitability of slots within ±2 of the activated slot."""
        inhibition = self._boost * 0.5
        for offset in range(-2, 3):
            if offset == 0:
                continue
            neighbor = activated_slot + offset
            if neighbor < 0 or neighbor >= self._num_slots:
                continue
            current = self.get_excitability(neighbor)
            new_exc = max(current - inhibition, 0.0)
            now = self._storage._now_iso()
            self._storage.update_engram_slot(neighbor, new_exc, now)

    @observe(tier="stage", metric="engram.rebalance_if_needed")
    def rebalance_if_needed(self, threshold_pct: float = 0.05) -> int:
        """Redistribute memories from over-occupied slots to least-occupied ones.

        Returns count of memories reassigned. Only moves enough to bring each
        over-occupied slot down to the threshold.
        """
        occupancy = self._storage.get_slot_occupancy()  # {slot_index: count}
        total = sum(occupancy.values())
        if total == 0:
            return 0
        threshold = max(1, int(total * threshold_pct))
        moved = 0
        for slot_idx in sorted(occupancy, key=lambda s: -occupancy[s]):
            count = occupancy[slot_idx]
            if count <= threshold:
                break
            excess = count - threshold
            # Get IDs of memories to reassign (pick the excess ones)
            mem_ids = self._storage.get_memory_ids_in_slot(slot_idx, limit=excess)
            for mem_id in mem_ids:
                # Find least-occupied slot (excluding current)
                target = min(
                    (s for s in range(self._num_slots) if s != slot_idx),
                    key=lambda s: occupancy.get(s, 0),
                )
                self._storage.assign_memory_slot(mem_id, target)
                occupancy[slot_idx] -= 1
                occupancy[target] = occupancy.get(target, 0) + 1
                moved += 1
        return moved

    @observe(tier="stage", metric="engram.get_slot_statistics")
    def get_slot_statistics(self) -> dict:
        """Return slot occupancy and excitability statistics."""
        occupancy = self._storage.get_slot_occupancy()
        all_slots = self._storage.get_all_engram_slots()

        excitabilities = []
        for slot in all_slots:
            exc = self._compute_decayed_excitability(
                slot["excitability"], slot.get("last_activated")
            )
            excitabilities.append(exc)

        occupied = len(occupancy)
        avg_exc = sum(excitabilities) / len(excitabilities) if excitabilities else 0.0
        max_exc = max(excitabilities) if excitabilities else 0.0

        return {
            "total_slots": self._num_slots,
            "occupied_slots": occupied,
            "avg_excitability": round(avg_exc, 4),
            "max_excitability": round(max_exc, 4),
            "slot_distribution": occupancy,
        }

    def _compute_decayed_excitability(
        self, stored_excitability: float, last_activated: str | None
    ) -> float:
        """Apply exponential decay to stored excitability based on elapsed time."""
        if last_activated is None or stored_excitability <= 0.0:
            return 0.0
        try:
            last_dt = datetime.fromisoformat(last_activated)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=UTC)
        except (ValueError, TypeError) as _e:
            return 0.0
        now = datetime.now(UTC)
        elapsed_hours = (now - last_dt).total_seconds() / 3600.0
        if elapsed_hours < 0:
            elapsed_hours = 0.0
        # Exponential decay with half-life: E(t) = E0 * 2^(-t/half_life)
        decayed = stored_excitability * (2.0 ** (-elapsed_hours / self._half_life))
        return decayed
