"""Thermodynamic heat decay mixin for ConsolidationScheduler.

Architecture (T4 — BC-CSW1 single-writer):
    Cycle phases COLLECT heat-change intents (SQL, params) instead of writing
    directly.  A reconcile step merges the intent lists.  A SINGLE
    HeatWriter.apply_heat_intents() call applies the reconciled set — one
    storage.batch_writes for all heat mutations per cycle.

Batched writes (ledger task 14, car 10):
    The intents above are one UPDATE per changed row.  On the live corpus
    (1740 decay-eligible rows, measured 2026-08-21) that is 1740 statements —
    and in server mode ``_build_chunk_body`` expands each into 3 ``LET`` + 1
    ``UPDATE``, so SurrealDB parses ~6960 statements every cycle; in embedded
    mode (the nightly cycle) ``batch_writes`` runs each as its own round-trip.
    ``_compact_heat_intents`` folds each run of identical-template intents into
    a bounded number of ``FOR $r IN [...] { UPDATE $r.i SET ... }`` statements.

    The decay ARITHMETIC stays in Python, deliberately — it is NOT expressible
    in SurrealQL without changing behaviour or duplicating the formula:
      * ``tags`` is stored as BOTH a JSON string and a real array in the live
        corpus, and the ``_action_stream`` cold-threshold branch reads it;
      * ``last_accessed`` / ``last_decay_at`` are ISO strings, not datetimes;
      * the domain multiplier comes from ``astrocyte_process`` rows, not a
        ``memory`` column;
      * ``stats[...]`` and the ``heat_updated`` SSE payload both need the new
        per-row heat in Python, so the row fetch cannot go away — meaning a
        SQL-side formula would be a SECOND copy of the arithmetic, not a
        replacement, and ``math::pow`` is not bit-identical to Python ``**``.
    Collapsing only the WRITE keeps exactly one arithmetic implementation, so
    the batched path is equal to the per-row path by construction.
"""

import json
import logging
import math
from datetime import UTC, datetime

from yadgar._shared.observability.observe import observe
from yadgar._shared.server_helpers import _push_event
from yadgar._shared.storage.heat_writer import HeatWriter

logger = logging.getLogger("yadgar.consolidation")

# ---------------------------------------------------------------------------
# Decay UPDATE templates — the SINGLE source for both the emitters below and
# the compactor.  _compact_heat_intents matches on OBJECT IDENTITY with these
# constants (not by parsing SQL), so an emitter and its compaction rule can
# never drift apart.  Nothing else in the tree may reconstruct this text.
# ---------------------------------------------------------------------------
_MEM_DECAY_SQL = (
    "UPDATE type::record('memory', $id) SET "
    "heat = $heat, last_decay_at = $now, access_count_since_decay = 0"
)
_ENT_DECAY_SQL = "UPDATE type::record('entity', $id) SET heat = $heat, last_decay_at = $now"
_ENT_DECAY_COLD_SQL = (
    "UPDATE type::record('entity', $id) SET heat = $heat, last_decay_at = $now, archived = true"
)

# template -> (table, SET clause rewritten against the FOR-loop variable $r)
_COMPACTABLE: dict[str, tuple[str, str]] = {
    _MEM_DECAY_SQL: (
        "memory",
        "heat = $r.h, last_decay_at = $now, access_count_since_decay = 0",
    ),
    _ENT_DECAY_SQL: ("entity", "heat = $r.h, last_decay_at = $now"),
    _ENT_DECAY_COLD_SQL: ("entity", "heat = $r.h, last_decay_at = $now, archived = true"),
}

# Rows folded into one FOR statement.  A module constant, NOT a knob (avoid
# config-surface churn).  Must stay well under MAX_BATCH_BYTES: _send_chunk
# CANNOT split a single oversized statement — it only WARNs and posts anyway.
# At ~48 bytes per inlined row this is ~24 KB per statement vs the 1 MB cap.
_MAX_ROWS_PER_FOR = 500


def _batched_decay_writes_enabled() -> bool:
    """Kill switch for the compaction (env-only, mirrors the cache knobs).

    Deliberately not in the config registry: it exists so an operator can fall
    back to the per-row path if a future SurrealDB regresses the ``FOR`` form,
    not as a tuning surface.
    """
    from yadgar._shared.config import resolve_knob  # noqa: PLC0415

    return resolve_knob(
        "YADGAR_BATCHED_DECAY_WRITES_ENABLED",
        "BATCHED_DECAY_WRITES_ENABLED",
        lambda v: v.lower() not in ("0", "false", "no"),
        True,
    )


@observe(tier="hot")
def _inlinable(params: dict | None) -> bool:
    """True when (id, heat, now) can be safely inlined into a FOR array literal.

    ``id`` must be a real int (``bool`` is an int subclass and is rejected) so
    the inlined record-id literal cannot carry injected text; ``heat`` must be
    a finite real number so ``json.dumps`` emits a valid SurrealQL numeric
    literal; ``now`` must be a string so it can ride as the single bound param.
    Anything else is forwarded per-row, untouched.
    """
    if not params:
        return False
    rid, heat, now = params.get("id"), params.get("heat"), params.get("now")
    if not isinstance(rid, int) or isinstance(rid, bool):
        return False
    if isinstance(heat, bool) or not isinstance(heat, int | float):
        return False
    return math.isfinite(heat) and isinstance(now, str)


@observe(tier="stage", metric="consolidation.compact_heat_intents")
def _compact_heat_intents(
    intents: list[tuple[str, dict | None]],
) -> list[tuple[str, dict | None]]:
    """Fold per-row decay UPDATEs into bounded ``FOR``-loop statements.

    Groups by (SQL template, ``now`` value) so the three decay templates never
    merge with each other and rows stamped with different watermarks stay apart.
    Each group is emitted at the position of its FIRST member, so relative order
    is preserved.  Intents whose template is unknown, or whose params fail
    ``_inlinable``, pass through verbatim.

    The float literal is produced by ``json.dumps`` — the SAME shortest
    round-trip text ``_build_chunk_body`` would have written into its ``LET``,
    so the double SurrealDB parses is bit-identical to the per-row path.
    """
    if not intents or not _batched_decay_writes_enabled():
        return list(intents)

    # Pass 1 — bucket each intent under an emit key, remembering first-seen order.
    # A pass-through gets its own singleton key so it keeps its place in the list.
    order: list[tuple[str, str] | int] = []
    groups: dict[tuple[str, str], list[tuple[int, float]]] = {}
    passthrough: dict[int, tuple[str, dict | None]] = {}

    for idx, (sql, params) in enumerate(intents):
        if sql not in _COMPACTABLE or not _inlinable(params):
            passthrough[idx] = (sql, params)
            order.append(idx)
            continue
        assert params is not None  # noqa: S101 — narrowed by _inlinable
        key = (sql, params["now"])
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append((params["id"], float(params["heat"])))

    # Pass 2 — emit.
    out: list[tuple[str, dict | None]] = []
    for entry in order:
        if isinstance(entry, int):
            out.append(passthrough[entry])
            continue
        sql, now = entry
        table, set_clause = _COMPACTABLE[sql]
        rows = groups[entry]
        for start in range(0, len(rows), _MAX_ROWS_PER_FOR):
            chunk = rows[start : start + _MAX_ROWS_PER_FOR]
            array = ", ".join(f"{{i: {table}:{rid}, h: {json.dumps(h)}}}" for rid, h in chunk)
            out.append((f"FOR $r IN [{array}] {{ UPDATE $r.i SET {set_clause}; }};", {"now": now}))
    return out


@observe(tier="stage", metric="consolidation.build_heat_updates")
def _build_heat_updates(
    mem_intents: list[tuple[str, dict | None]],
    ent_intents: list[tuple[str, dict | None]],
) -> list[dict]:
    """Build the SSE heat_updated payload from the reconciled heat intents.

    Each intent carries the persisted new heat in ``params["heat"]`` and the row
    id in ``params["id"]``. Only rows that CHANGED produce an intent, so this
    naturally emits one update per changed row. Ids are typed to match the viz
    node id space: memories → ``mem:{id}``, entities → ``entity:{id}`` (the
    frontend patch loop is id-generic — CAP-VIZ-011 / F2).
    """
    updates: list[dict] = []
    for prefix, intents in (("mem", mem_intents), ("entity", ent_intents)):
        for _sql, params in intents:
            if not params or "id" not in params or "heat" not in params:
                continue
            updates.append({"id": f"{prefix}:{params['id']}", "heat": params["heat"]})
    return updates


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

    @observe(tier="stage", metric="consolidation.decay")
    def _apply_decay(self, stats: dict) -> None:
        """Collect memory + entity heat intents, reconcile, apply once (BC-CSW1)."""
        now = datetime.now(UTC)
        # Phase 1 — collect intents (no writes yet)
        mem_intents = self._decay_memories(stats, now)
        ent_intents = self._decay_entities(now)
        # Phase 2 — reconcile: merge both intent lists (one entry per id)
        all_intents = _reconcile_heat_intents(mem_intents, ent_intents)
        # Phase 3 — single apply via HeatWriter facade (BC-CSW1).  The intents
        # are compacted into FOR-loop statements FIRST (task 14) so the one
        # batch carries a bounded statement count instead of one per row.  The
        # UNCOMPACTED lists are what Phase 4 reads — compaction changes the
        # write shape only, never the values.
        HeatWriter(self._storage).apply_heat_intents(_compact_heat_intents(all_intents))
        # Phase 4 — emit ONE heat_updated SSE event for the changed rows (F2).
        # After apply, so the payload references persisted heat values. Skip the
        # push entirely when nothing changed (the idempotency fix makes repeat
        # cycles near-noop — don't spam empty events). Backend-process push;
        # core's SSE loop relays it to browsers via the /viz events op.
        updates = _build_heat_updates(mem_intents, ent_intents)
        if updates:
            _push_event({"event": "heat_updated", "updates": updates})

    @observe(tier="stage", metric="consolidation.build_domain_multiplier_map")
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
        except Exception:  # BLE001-KEEP: the astrocyte-pool read that supplies per-domain decay multipliers: storage raises with no common base, and an unavailable pool must leave every multiplier at 1.0 rather than stop the decay pass
            return {}  # pool unavailable -> all mults default 1.0

    @observe(tier="stage", metric="consolidation.decay_memories")
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
                        _MEM_DECAY_SQL,
                        {"id": mem["id"], "heat": new_heat, "now": now.isoformat()},
                    )
                )
                if abs(new_heat - mem["heat"]) > 1e-9:
                    stats["memories_updated"] += 1
        return mem_batch

    @observe(tier="stage", metric="consolidation.decay_entities")
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
                sql = _ENT_DECAY_COLD_SQL if goes_cold else _ENT_DECAY_SQL
                ent_batch.append((sql, {"id": ent["id"], "heat": new_heat, "now": now.isoformat()}))
        return ent_batch
