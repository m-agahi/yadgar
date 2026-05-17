"""Thermodynamic heat decay mixin for ConsolidationScheduler."""

import logging
from datetime import UTC, datetime

logger = logging.getLogger("yadgar.consolidation")


class _HeatDecayMixin:
    """Applies thermodynamic decay to memory and entity heat values."""

    def _apply_decay(self, stats: dict) -> None:
        now = datetime.now(UTC)
        decay = self._settings.DECAY_FACTOR
        cold = self._settings.COLD_THRESHOLD
        action_stream_cold = self._settings.ACTION_STREAM_COLD_THRESHOLD

        mem_batch: list[tuple[str, dict | None]] = []
        for mem in self._storage.get_all_memories_for_decay():
            if mem.get("is_protected"):
                continue
            last = datetime.fromisoformat(mem["last_accessed"])
            hours = (now - last).total_seconds() / 3600.0
            new_heat = self._thermo.compute_decay(mem, hours)
            tags = mem.get("tags") or []
            if isinstance(tags, str):
                import json

                tags = json.loads(tags)
            effective_cold = action_stream_cold if "_action_stream" in tags else cold
            if new_heat < effective_cold:
                new_heat = 0.0
                stats["memories_archived"] += 1
            if abs(new_heat - mem["heat"]) > 1e-9:
                mem_batch.append(
                    (
                        "UPDATE type::record('memory', $id) SET heat = $heat",
                        {"id": mem["id"], "heat": new_heat},
                    )
                )
                stats["memories_updated"] += 1

        if mem_batch:
            self._storage.batch_writes(mem_batch)

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

        if ent_batch:
            self._storage.batch_writes(ent_batch)
