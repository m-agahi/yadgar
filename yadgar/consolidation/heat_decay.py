"""Thermodynamic heat decay mixin for ConsolidationScheduler."""

import logging
from datetime import UTC, datetime

logger = logging.getLogger("yadgar.consolidation")


class _HeatDecayMixin:
    """Applies thermodynamic decay to memory and entity heat values."""

    def _apply_decay(self, stats: dict) -> None:
        now = datetime.now(UTC)
        mem_batch = self._decay_memories(stats, now)
        if mem_batch:
            self._storage.batch_writes(mem_batch)
        ent_batch = self._decay_entities(now)
        if ent_batch:
            self._storage.batch_writes(ent_batch)

    def _decay_memories(self, stats: dict, now: datetime) -> list[tuple[str, dict | None]]:
        """Compute per-memory heat decay; return batch of DB writes."""
        cold = self._settings.COLD_THRESHOLD
        action_stream_cold = self._settings.ACTION_STREAM_COLD_THRESHOLD
        # C2: recall-frequency-modulated decay (MemoryBank parity)
        recall_boost = self._settings.RECALL_BOOST

        mem_batch: list[tuple[str, dict | None]] = []
        for mem in self._storage.get_all_memories_for_decay():
            if mem.get("is_protected"):
                continue
            last = datetime.fromisoformat(mem["last_accessed"])
            hours = (now - last).total_seconds() / 3600.0
            # Base decay — uses importance/valence/confidence modifiers
            new_heat = self._thermo.compute_decay(mem, hours)
            # C2: add per-cycle recall boost; reset counter atomically in same UPDATE
            access_since_decay = int(mem.get("access_count_since_decay", 0))
            if recall_boost > 0.0 and access_since_decay > 0:
                new_heat = min(new_heat + access_since_decay * recall_boost, 1.0)
            tags = mem.get("tags") or []
            if isinstance(tags, str):
                import json

                tags = json.loads(tags)
            effective_cold = action_stream_cold if "_action_stream" in tags else cold
            if new_heat < effective_cold:
                new_heat = 0.0
                stats["memories_archived"] += 1
            if abs(new_heat - mem["heat"]) > 1e-9 or access_since_decay > 0:
                # Always reset access_count_since_decay to 0 if it was non-zero,
                # even when heat didn't change (so the cycle-counter stays clean).
                mem_batch.append(
                    (
                        "UPDATE type::record('memory', $id) SET "
                        "heat = $heat, access_count_since_decay = 0",
                        {"id": mem["id"], "heat": new_heat},
                    )
                )
                if abs(new_heat - mem["heat"]) > 1e-9:
                    stats["memories_updated"] += 1
        return mem_batch

    def _decay_entities(self, now: datetime) -> list[tuple[str, dict | None]]:
        """Compute per-entity heat decay; return batch of DB writes."""
        decay = self._settings.DECAY_FACTOR
        cold = self._settings.COLD_THRESHOLD

        ent_batch: list[tuple[str, dict | None]] = []
        for ent in self._storage.get_all_entities_for_decay():
            last = datetime.fromisoformat(ent["last_accessed"])
            hours = (now - last).total_seconds() / 3600.0
            new_heat = ent["heat"] * (decay**hours)
            goes_cold = new_heat < cold
            if goes_cold:
                new_heat = 0.0
            if abs(new_heat - ent["heat"]) > 1e-9 or goes_cold:
                set_clause = "heat = $heat"
                if goes_cold:
                    set_clause += ", archived = true"
                ent_batch.append(
                    (
                        f"UPDATE type::record('entity', $id) SET {set_clause}",
                        {"id": ent["id"], "heat": new_heat},
                    )
                )
        return ent_batch
