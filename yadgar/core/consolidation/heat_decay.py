"""Thermodynamic heat decay mixin for ConsolidationScheduler.

Architecture (T4 — BC-CSW1 single-writer):
    Cycle phases COLLECT heat-change intents (SQL, params) instead of writing
    directly.  A reconcile step merges the intent lists.  A SINGLE
    HeatWriter.apply_heat_intents() call applies the reconciled set — one
    storage.batch_writes for all heat mutations per cycle.
"""

import logging
from datetime import UTC, datetime

from yadgar._shared.observability.observe import observe
from yadgar._shared.storage.heat_writer import HeatWriter

logger = logging.getLogger("yadgar.consolidation")


def _reconcile_heat_intents(
    mem_intents: list[tuple[str, dict | None]],
    ent_intents: list[tuple[str, dict | None]],
) -> list[tuple[str, dict | None]]:
    """Merge memory and entity heat intents into one ordered list.

    Each intent targets a unique (table, id) pair — no deduplication is needed
    because _decay_memories and _decay_entities each emit at most one UPDATE per
    record.  This reconcile step exists to make the single-writer contract
    explicit: all heat mutations across both tables are combined before the
    single batch_writes call.

    Args:
        mem_intents: UPDATE statements for memory table (from _decay_memories).
        ent_intents: UPDATE statements for entity table (from _decay_entities).

    Returns:
        Combined list: memories first, then entities.  Order within each group
        is preserved (stable, deterministic — same as before the refactor).
    """
    return list(mem_intents) + list(ent_intents)


class _HeatDecayMixin:
    """Applies thermodynamic decay to memory and entity heat values."""

    @observe(tier="stage", name="consolidation.decay")
    def _apply_decay(self, stats: dict) -> None:
        """Collect memory + entity heat intents, reconcile, apply once (BC-CSW1)."""
        now = datetime.now(UTC)
        # Phase 1 — collect intents (no writes yet)
        mem_intents = self._decay_memories(stats, now)
        ent_intents = self._decay_entities(now)
        # Phase 2 — reconcile: merge both intent lists (one entry per id)
        all_intents = _reconcile_heat_intents(mem_intents, ent_intents)
        # Phase 3 — single apply via HeatWriter facade (BC-CSW1)
        HeatWriter(self._storage).apply_heat_intents(all_intents)

    @observe(tier="stage", name="consolidation.build_domain_multiplier_map")
    def _build_domain_multiplier_map(self) -> dict[int, float]:
        """Return {memory_id -> max decay multiplier across all domains}.

        Called once per decay cycle; result passed into _decay_memories.
        Higher multiplier -> fewer effective hours -> slower decay.
        MAX tie-break for memories assigned to multiple domains.
        Returns empty dict when pool is disabled or unavailable (all mults
        fall through to 1.0, giving identical-to-today behaviour).
        """
        if not getattr(self._settings, "ASTROCYTE_POOL_ENABLED", True):
            return {}
        try:
            from yadgar._shared.astrocyte_pool import DOMAIN_DEFINITIONS  # noqa: PLC0415

            domain_mult: dict[int, float] = {}
            for proc in self._storage.get_astrocyte_processes():
                proc_name = proc.get("name", "")
                proc_mult = DOMAIN_DEFINITIONS.get(proc_name, {}).get("decay_multiplier", 1.0)
                for mid in proc.get("memory_ids", []):
                    existing = domain_mult.get(mid)
                    domain_mult[mid] = (
                        max(existing, proc_mult) if existing is not None else proc_mult
                    )
            return domain_mult
        except Exception:
            return {}  # pool unavailable -> all mults default 1.0

    @observe(tier="stage", name="consolidation.decay_memories")
    def _decay_memories(self, stats: dict, now: datetime) -> list[tuple[str, dict | None]]:
        """Compute per-memory heat decay; return batch of DB writes."""
        cold = self._settings.COLD_THRESHOLD
        action_stream_cold = self._settings.ACTION_STREAM_COLD_THRESHOLD
        # C2: recall-frequency-modulated decay (MemoryBank parity)
        recall_boost = self._settings.RECALL_BOOST

        # Domain-multiplier map: empty when pool disabled -> all mults fall through to 1.0.
        # This is the ONLY decay site -- consolidate_domain no longer writes heat.
        domain_mult = self._build_domain_multiplier_map()

        mem_batch: list[tuple[str, dict | None]] = []
        for mem in self._storage.get_all_memories_for_decay_scalar():
            if mem.get("is_protected"):
                continue
            # Decay over time since the LAST decay pass (watermark), not since
            # last access.  Without last_decay_at, every cycle re-applied the full
            # now-last_accessed span onto already-decayed heat -> quadratic
            # over-decay for unaccessed memories.  Fall back to last_accessed for
            # rows written before last_decay_at existed.
            last = datetime.fromisoformat(mem["last_accessed"])
            last_decay_raw = mem.get("last_decay_at")
            if last_decay_raw:
                last = max(last, datetime.fromisoformat(last_decay_raw))
            hours = (now - last).total_seconds() / 3600.0
            # Domain-aware decay: divide hours by multiplier (>1 -> slower, <1 -> faster).
            # MAX tie-break: if memory belongs to multiple domains, use highest multiplier.
            mult = domain_mult.get(mem["id"], 1.0)
            adjusted_hours = hours / mult if mult > 0.0 else hours
            # Base decay -- uses importance/valence/confidence modifiers
            new_heat = self._thermo.compute_decay(mem, adjusted_hours)
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
                        "heat = $heat, last_decay_at = $now, access_count_since_decay = 0",
                        {"id": mem["id"], "heat": new_heat, "now": now.isoformat()},
                    )
                )
                if abs(new_heat - mem["heat"]) > 1e-9:
                    stats["memories_updated"] += 1
        return mem_batch

    @observe(tier="stage", name="consolidation.decay_entities")
    def _decay_entities(self, now: datetime) -> list[tuple[str, dict | None]]:
        """Compute per-entity heat decay; return batch of DB writes."""
        decay = self._settings.DECAY_FACTOR
        cold = self._settings.COLD_THRESHOLD

        ent_batch: list[tuple[str, dict | None]] = []
        for ent in self._storage.get_all_entities_for_decay():
            # Same watermark fix as memories: decay since the last decay pass, not
            # since last access, so repeated cycles don't compound over-decay.
            last = datetime.fromisoformat(ent["last_accessed"])
            last_decay_raw = ent.get("last_decay_at")
            if last_decay_raw:
                last = max(last, datetime.fromisoformat(last_decay_raw))
            hours = (now - last).total_seconds() / 3600.0
            new_heat = ent["heat"] * (decay**hours)
            goes_cold = new_heat < cold
            if goes_cold:
                new_heat = 0.0
            if abs(new_heat - ent["heat"]) > 1e-9 or goes_cold:
                set_clause = "heat = $heat, last_decay_at = $now"
                if goes_cold:
                    set_clause += ", archived = true"
                ent_batch.append(
                    (
                        f"UPDATE type::record('entity', $id) SET {set_clause}",
                        {"id": ent["id"], "heat": new_heat, "now": now.isoformat()},
                    )
                )
        return ent_batch
