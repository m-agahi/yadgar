"""Operational/bookkeeping table CRUD.

_OpsMixin provides:
  - Consolidation log insert
  - Memory stats (aggregate queries)
  - Engram slot init/query/update/assign
  - Checkpoint insert/query/update/epoch helpers
  - prune_old_rows generic pruner
"""

import logging
import re as _re

from yadgar._shared.observability.observe import observe

_log = logging.getLogger(__name__)

# S1a (H-5): allowlist for extra_where clauses in prune_old_rows.
# Permits only simple "column = literal_value" fragments where:
#   - column: alphanumeric + underscore
#   - operator: =, !=, <, <=, >, >= (no keyword operators that could be injected)
#   - literal: boolean keywords (true/false/none/null), quoted strings, or numbers.
# Semicolons, comments (--), and parentheses are rejected outright.
# Callers (consolidation/cleanup.py) only ever pass "is_active = false" style clauses.
_EXTRA_WHERE_PATTERN = _re.compile(
    r"^[a-zA-Z_][a-zA-Z0-9_]*\s*(?:=|!=|<=?|>=?)\s*(?:true|false|none|null|-?\d+(?:\.\d+)?|'[^']*')$",
    _re.IGNORECASE,
)

_META_KEY_PATTERN = _re.compile(r"^[a-z0-9_]+$")


@observe(tier="hot", metric="storage.ops.sanitize_meta_key")
def _sanitize_meta_key(key: str) -> str:
    """Validate a consolidation_meta record key (lowercase alnum + underscore only).

    Keys are interpolated into the record id (`consolidation_meta:<key>`) so they
    must never contain query-breaking characters. Callers pass fixed literals.
    """
    if not _META_KEY_PATTERN.match(key or ""):
        raise ValueError(f"invalid consolidation_meta key: {key!r}")
    return key


class _OpsMixin:
    """Operational tables (consolidation_log, stats, engram_slot, checkpoint, prune) —
    mixed into StorageEngine."""

    # ------------------------------------------------------------------ Consolidation Log

    @observe(tier="stage", metric="storage.ops.insert_consolidation_log")
    def insert_consolidation_log(self, log: dict) -> int:
        cid = self._next_id("consolidation_log")
        self._q(
            "CREATE type::record('consolidation_log', $id) SET "
            "timestamp = $timestamp, memories_added = $added, "
            "memories_updated = $updated, memories_archived = $archived, "
            "memories_deleted = $deleted, duration_ms = $duration_ms",
            {
                "id": cid,
                "timestamp": log.get("timestamp", self._now_iso()),
                "added": log.get("memories_added", 0),
                "updated": log.get("memories_updated", 0),
                "archived": log.get("memories_archived", 0),
                "deleted": log.get("memories_deleted", 0),
                "duration_ms": log.get("duration_ms", 0),
            },
        )
        return cid

    # --------------------------------------------------- Consolidation Watermark
    # v5.86 (OT-C4): persisted timestamps that drive incremental similarity-linking.
    # Stored as singleton rows in `consolidation_meta` keyed by a stable record id
    # so reads are O(1) and writes are upsert-in-place (no per-write row growth).

    @observe(tier="stage", metric="storage.ops.get_consolidation_watermark")
    def get_consolidation_watermark(self, key: str) -> str | None:
        """Return the persisted ISO-8601 watermark for `key`, or None if unset.

        `key` is a short identifier (e.g. "similarity_linking", "full_reconcile").
        """
        safe = _sanitize_meta_key(key)
        rows = self._q(f"SELECT ts FROM consolidation_meta:{safe}")
        if rows and rows[0].get("ts"):
            return str(rows[0]["ts"])
        return None

    @observe(tier="stage", metric="storage.ops.set_consolidation_watermark")
    def set_consolidation_watermark(self, key: str, value: str) -> None:
        """Upsert the watermark for `key` to the ISO-8601 timestamp `value`."""
        safe = _sanitize_meta_key(key)
        self._q(
            f"UPSERT consolidation_meta:{safe} SET ts = $ts, updated_at = $now",
            {"ts": value, "now": self._now_iso()},
        )

    # --------------------------------------------------- Graph Layout Cache
    # v5.88: precomputed server-side 3D graph layout. A single singleton row
    # holds {signature, positions, computed_at} so /api/graph can attach x/y/z
    # when the flag is on and the cached signature still matches the live graph.

    @observe(tier="stage", metric="storage.ops.get_graph_layout_cache")
    def get_graph_layout_cache(self) -> dict | None:
        """Return the cached layout {signature, positions, computed_at}, or None.

        ``positions`` is a ``{node_id: [x, y, z]}`` map. Returns None when no
        layout has been computed yet.
        """
        from yadgar._shared.metrics import record_cache_hit, record_cache_miss

        rows = self._q("SELECT signature, positions, computed_at FROM graph_layout_cache:current")
        if not rows or not rows[0].get("signature"):
            record_cache_miss("graph_layout")
            return None
        record_cache_hit("graph_layout")
        row = rows[0]
        return {
            "signature": str(row["signature"]),
            "positions": dict(row.get("positions") or {}),
            "computed_at": str(row.get("computed_at") or ""),
        }

    @observe(tier="stage", metric="storage.ops.set_graph_layout_cache")
    def set_graph_layout_cache(self, signature: str, positions: dict, computed_at: str) -> None:
        """Upsert the singleton precomputed-layout row in place.

        ``positions`` is bound as a parameter ($pos) so SurrealDB serialises the
        nested object safely (no raw interpolation).
        """
        self._q(
            "UPSERT graph_layout_cache:current SET "
            "signature = $sig, positions = $pos, computed_at = $ts, updated_at = $now",
            {"sig": signature, "pos": positions, "ts": computed_at, "now": self._now_iso()},
        )

    # ------------------------------------------------------------------ Stats

    @observe(tier="stage", metric="storage.ops.get_memory_stats")
    def get_memory_stats(self) -> dict:
        total_rows = self._q("SELECT count() AS c FROM memory GROUP ALL")
        total = int(total_rows[0]["c"]) if total_rows else 0

        active_rows = self._q(
            "SELECT count() AS c FROM memory WHERE is_stale = false AND heat >= 0.05 GROUP ALL"
        )
        active = int(active_rows[0]["c"]) if active_rows else 0

        archived_rows = self._q("SELECT count() AS c FROM memory WHERE heat < 0.05 GROUP ALL")
        archived = int(archived_rows[0]["c"]) if archived_rows else 0

        stale_rows = self._q("SELECT count() AS c FROM memory WHERE is_stale = true GROUP ALL")
        stale = int(stale_rows[0]["c"]) if stale_rows else 0

        heat_rows = self._q("SELECT math::mean(heat) AS avg FROM memory GROUP ALL")
        avg_heat = (
            float(heat_rows[0]["avg"]) if heat_rows and heat_rows[0].get("avg") is not None else 0.0
        )

        log_rows = self._q("SELECT * FROM consolidation_log ORDER BY timestamp DESC LIMIT 1")
        last_consolidation = log_rows[0]["timestamp"] if log_rows else None

        return {
            "total_memories": total,
            "active_count": active,
            "archived_count": archived,
            "stale_count": stale,
            "avg_heat": avg_heat,
            "last_consolidation": last_consolidation,
        }

    # ------------------------------------------------------------------ prune_old_rows

    @observe(tier="stage", metric="storage.ops.prune_old_rows")
    def prune_old_rows(
        self,
        table: str,
        older_than_days: int,
        age_field: str = "created_at",
        extra_where: str | None = None,
    ) -> int:
        """Delete rows from `table` whose `age_field` is older than `older_than_days`.

        `extra_where` may contain additional AND conditions (e.g. ``is_active = false``).
        Only the listed tables may be pruned — rejects unknown tables to prevent SQL injection.

        Returns the approximate number of rows deleted (counted before the DELETE).
        """
        from datetime import UTC, datetime, timedelta

        _allowed_tables = frozenset(
            {
                "narrative_entry",
                "astrocyte_process",
                "memory_cluster",
                "derived_belief",
                "prospective_memory",
            }
        )
        if table not in _allowed_tables:
            raise ValueError(f"prune_old_rows: table '{table}' is not in the allowed set")
        if older_than_days <= 0:
            return 0

        cutoff = (datetime.now(UTC) - timedelta(days=older_than_days)).isoformat()
        where = f"{age_field} < $cutoff"
        if extra_where:
            # S1a (H-5): validate extra_where against a strict allowlist before interpolation.
            # Permits only "column op literal" forms (see _EXTRA_WHERE_PATTERN above).
            # Semicolons, comments, subqueries, and compound expressions are rejected.
            if not _EXTRA_WHERE_PATTERN.match(extra_where.strip()):
                raise ValueError(
                    f"prune_old_rows: extra_where {extra_where!r} is not an allowed clause. "
                    "Only simple 'column op literal' expressions are permitted."
                )
            where = f"{where} AND {extra_where}"
        count_rows = self._q(
            f"SELECT count() AS c FROM {table} WHERE {where} GROUP ALL",
            {"cutoff": cutoff},
        )
        n = int(count_rows[0]["c"]) if count_rows and count_rows[0].get("c") else 0
        self._q(f"DELETE FROM {table} WHERE {where}", {"cutoff": cutoff})
        return n

    # ------------------------------------------------------------------ Archive Retention

    @observe(tier="stage", metric="storage.ops.purge_expired_archives")
    def purge_expired_archives(
        self,
        dry_run: bool = False,
        retention_days_override: int | None = None,
    ) -> dict:
        """Purge memory_archive rows older than retention threshold.

        retention_days_override: if provided, use this value instead of
            MEMORY_ARCHIVE_RETENTION_DAYS for this call only.

        Returns dict:
          {
            "candidates": int,            # total matched (before circuit-breaker cap)
            "purged": int,                # actual rows deleted (0 if dry_run)
            "skipped_protected": int,
            "skipped_anchor": int,
            "skipped_recent": int,
            "circuit_breaker_hit": bool,
            "candidate_ids": list[int],   # first 10 candidate IDs (for MCP sample)
          }
        """
        from datetime import UTC, datetime, timedelta

        from yadgar._shared.config import get_settings

        cfg = get_settings()
        retention_days: int = (
            retention_days_override
            if retention_days_override is not None
            else cfg.MEMORY_ARCHIVE_RETENTION_DAYS
        )
        circuit_breaker: int = cfg.MEMORY_ARCHIVE_RETENTION_CIRCUIT_BREAKER
        thrash_guard_days: int = cfg.MEMORY_ARCHIVE_RETENTION_THRASH_GUARD_DAYS

        _zero: dict = {
            "candidates": 0,
            "purged": 0,
            "skipped_protected": 0,
            "skipped_anchor": 0,
            "skipped_recent": 0,
            "circuit_breaker_hit": False,
        }

        if retention_days == 0:
            return _zero

        now = datetime.now(UTC)
        archived_cutoff = (now - timedelta(days=retention_days)).isoformat()
        thrash_cutoff = (now - timedelta(days=thrash_guard_days)).isoformat()

        # Eligible rows: old enough, not protected, not anchor-tagged,
        # not recently created, and not in active migration grace.
        # Uses INSIDE/NOTINSIDE — the operator convention for SurrealDB v2 embedded mode.
        eligible_rows = self._q(
            "SELECT meta::id(id) AS rid FROM memory_archive "
            "WHERE archived_at < $archived_cutoff "
            "AND is_protected != true "
            "AND '_anchor' NOTINSIDE tags "
            "AND 'anchor' NOTINSIDE tags "
            "AND created_at < $thrash_cutoff "
            "AND (migration_grace != true OR valid_until < $now_iso) "
            "LIMIT $limit",
            {
                "archived_cutoff": archived_cutoff,
                "thrash_cutoff": thrash_cutoff,
                "now_iso": now.isoformat(),
                "limit": circuit_breaker + 1,
            },
        )

        candidates = len(eligible_rows)
        circuit_breaker_hit = candidates > circuit_breaker
        if circuit_breaker_hit:
            _log.critical(
                "purge_expired_archives: circuit-breaker hit — %d candidates exceed limit %d; "
                "capping purge to %d rows",
                candidates,
                circuit_breaker,
                circuit_breaker,
            )
            eligible_rows = eligible_rows[:circuit_breaker]
            candidates = circuit_breaker

        # Count skip categories over the same archived_at window (before exclusions).
        skipped_protected = self._count_archive_skip_protected(archived_cutoff)
        skipped_anchor = self._count_archive_skip_anchor(archived_cutoff)
        skipped_recent = self._count_archive_skip_recent(archived_cutoff, thrash_cutoff)

        all_ids = [int(r["rid"]) for r in eligible_rows]
        candidate_ids = all_ids[:10]

        purged = 0
        if not dry_run and all_ids:
            for aid in all_ids:
                self._q(
                    "DELETE type::record('memory_archive', $id)",
                    {"id": aid},
                )
            purged = len(all_ids)

        return {
            "candidates": candidates,
            "purged": purged,
            "skipped_protected": skipped_protected,
            "skipped_anchor": skipped_anchor,
            "skipped_recent": skipped_recent,
            "circuit_breaker_hit": circuit_breaker_hit,
            "candidate_ids": candidate_ids,
        }

    @observe(tier="hot", metric="storage.ops.count_archive_skip_protected")
    def _count_archive_skip_protected(self, archived_cutoff: str) -> int:
        """Count protected archives older than cutoff."""
        rows = self._q(
            "SELECT count() AS c FROM memory_archive "
            "WHERE archived_at < $cutoff AND is_protected = true GROUP ALL",
            {"cutoff": archived_cutoff},
        )
        return int(rows[0]["c"]) if rows and rows[0].get("c") else 0

    @observe(tier="hot", metric="storage.ops.count_archive_skip_anchor")
    def _count_archive_skip_anchor(self, archived_cutoff: str) -> int:
        """Count anchor-tagged archives older than cutoff."""
        rows = self._q(
            "SELECT count() AS c FROM memory_archive "
            "WHERE archived_at < $cutoff "
            "AND ('_anchor' INSIDE tags OR 'anchor' INSIDE tags) GROUP ALL",
            {"cutoff": archived_cutoff},
        )
        return int(rows[0]["c"]) if rows and rows[0].get("c") else 0

    @observe(tier="hot", metric="storage.ops.count_archive_skip_recent")
    def _count_archive_skip_recent(self, archived_cutoff: str, thrash_cutoff: str) -> int:
        """Count archives older than archived_cutoff but recently created (thrash-guard)."""
        rows = self._q(
            "SELECT count() AS c FROM memory_archive "
            "WHERE archived_at < $cutoff AND created_at >= $thrash GROUP ALL",
            {"cutoff": archived_cutoff, "thrash": thrash_cutoff},
        )
        return int(rows[0]["c"]) if rows and rows[0].get("c") else 0

    # ------------------------------------------------------------------ Engram Slots

    @observe(tier="stage", metric="storage.ops.init_engram_slots")
    def init_engram_slots(self, num_slots: int):
        """Ensure all slot indices exist in the engram_slot table."""
        now = self._now_iso()
        rows = self._q("SELECT VALUE slot_index FROM engram_slot")
        # Cast to int — SurrealDB may return floats (e.g. 0.0) instead of ints
        existing = {int(r) for r in rows if r is not None}
        missing = [i for i in range(num_slots) if i not in existing]
        if not missing:
            return
        records = [{"slot_index": i, "excitability": 0.0, "last_activated": now} for i in missing]
        _CHUNK = 500
        for start in range(0, len(records), _CHUNK):
            chunk = records[start : start + _CHUNK]
            # S1b (H-4): use bind parameter ($data) instead of raw json.dumps interpolation.
            # The LET-preamble mechanism in _q serialises the value safely with ensure_ascii=False,
            # preventing any dollar-token or quote-escape in surrounding context from corrupting
            # the INSERT statement.
            self._q("INSERT INTO engram_slot $data", {"data": chunk})

    def get_engram_slot(self, slot_index: int) -> dict | None:
        rows = self._q(
            "SELECT * FROM engram_slot WHERE slot_index = $si LIMIT 1",
            {"si": slot_index},
        )
        # Engram slots use SurrealDB auto-generated string IDs; skip _row_to_dict
        # which would try to coerce the ID to int and fail.
        return dict(rows[0]) if rows else None

    def get_all_engram_slots(self) -> list[dict]:
        rows = self._q("SELECT * FROM engram_slot ORDER BY slot_index")
        # Engram slots use SurrealDB auto-generated string IDs; skip _row_to_dict
        # which would try to coerce the ID to int and fail.
        return [dict(r) for r in rows]

    def update_engram_slot(self, slot_index: int, excitability: float, last_activated: str):
        self._q(
            "UPDATE engram_slot SET excitability = $exc, last_activated = $la "
            "WHERE slot_index = $si",
            {"si": slot_index, "exc": excitability, "la": last_activated},
        )

    @observe(tier="stage", metric="storage.ops.assign_memory_slot")
    def assign_memory_slot(self, memory_id: int, slot_index: int):
        now = self._now_iso()
        # Car 3 (backend 5.20.0): capture the memory's OLD slot BEFORE the write so
        # a reslot (rebalance, engram.py:177) can bump BOTH the source and target
        # slot versions — assign_memory_slot is the single slot_index-write choke
        # point (allocate + rebalance both route here). A fresh new-memory alloc
        # has no old slot (NONE) → only the target is bumped.
        old_slot = None
        try:
            rows = self._q(
                "SELECT VALUE slot_index FROM type::record('memory', $id)",
                {"id": memory_id},
            )
            if rows and rows[0] is not None:
                old_slot = int(rows[0])
        except Exception:  # noqa: BLE001 — a lookup failure must never fail the write
            _log.debug(
                "assign_memory_slot: old-slot lookup failed for %s", memory_id, exc_info=True
            )
        self._q(
            "UPDATE type::record('memory', $id) SET "
            "slot_index = $si, excitability = 1.0, last_excitability_update = $now",
            {"id": memory_id, "si": slot_index, "now": now},
        )
        # version-in-key bump: the target slot gains a member (invisible to a
        # cached, older-version candidate set until the version moves). Bump the
        # source too when this is a reslot (it also gains an *implicit* change, and
        # the reslot-into needs the target bumped for the moved id to appear).
        self._bump_slot_version(slot_index)
        if old_slot is not None and old_slot != slot_index:
            self._bump_slot_version(old_slot)

    @observe(tier="stage", metric="storage.ops.get_memories_in_slot")
    def get_memories_in_slot(self, slot_index: int) -> list[dict]:
        """Return the live members of an engram slot (``heat>0``, created_at order).

        Car 3 (backend 5.20.0): the ``engram_slot`` cache holds ONLY the STRUCTURAL
        membership — the ordered candidate memory ids for the slot, keyed by
        ``(slot_index, slot_version)``. The volatile ``heat>0`` predicate and the
        live ``slot_index`` match are re-verified FRESH here against the cached
        candidate ids (a light id-restricted query), so heat→0 decay, delete and
        reslot-away are inert (the fresh recheck drops them) with NO version bump.
        Only a NEW member appearing (create-alloc / reslot-into) needs a bump —
        done at ``assign_memory_slot`` — because a joined id is absent from the
        cached candidate set until the slot's version moves. A ``NullCache``
        (default until wired / kill-switched) makes every read a full slot scan,
        identical to pre-Car-3. See yadgar/backend/cache.py get_engram_slot_cache
        for the full staleness contract.
        """
        cache = self._resolve_engram_slot_cache()
        version = self._resolve_scope_versions().version("slot", slot_index)
        cached_ids = cache.get((slot_index, version))
        if cached_ids is not None:
            if not cached_ids:
                return []
            # Fresh recheck: re-apply heat>0 + live slot match on the cached ids,
            # preserving the cached created_at order. Structural drops (delete /
            # reslot-away / heat→0) fall out here without any version bump.
            id_list = ", ".join(f"memory:{mid}" for mid in cached_ids)
            rows = self._q(
                f"SELECT * FROM memory WHERE id IN [{id_list}] AND slot_index = $si AND heat > 0",
                {"si": slot_index},
            )
            live = self._rows_to_dicts(rows)
            live_by_id = {m["id"]: m for m in live}
            # rebuild in the cached (created_at) order; skip ids no longer live.
            return [live_by_id[mid] for mid in cached_ids if mid in live_by_id]

        # Miss: full slot scan. Cache the HEAT-FREE structural membership (every
        # id in the slot, created_at order) — NOT the heat-filtered result — so a
        # later heat-revival (heat 0→>0, no slot write, no version bump) is caught
        # by the fresh recheck (the id is in the candidate set). Apply heat>0 only
        # to the RETURNED rows, so the miss path is byte-identical to the pre-Car-3
        # `WHERE ... AND heat>0` query.
        rows = self._q(
            "SELECT * FROM memory WHERE slot_index = $si ORDER BY created_at",
            {"si": slot_index},
        )
        all_members = self._rows_to_dicts(rows)
        cache.put((slot_index, version), [m["id"] for m in all_members])
        return [m for m in all_members if m.get("heat", 0) > 0]

    @observe(tier="hot", metric="storage.ops.resolve_engram_slot_cache")
    def _resolve_engram_slot_cache(self):
        """Return the injected ``engram_slot`` cache, or the process-global default.

        Constructor-DI seam (mirrors ``_resolve_memory_doc_cache``): a
        ``StorageEngine`` may set ``self._engram_slot_cache`` to a ``NullCache``
        (disable) or a test double; otherwise the shared registered instance is
        used. Import is deferred to keep the storage import graph clean.
        """
        cache = getattr(self, "_engram_slot_cache", None)
        if cache is not None:
            return cache
        # Car 2 (folder-split #17): the lazy `backend.cache.get_engram_slot_cache`
        # fallback is deleted (it was a _shared→backend edge). The composition root
        # (lifecycle.init_engines) injects the REAL registered instance; the bare
        # default is a _shared NullCache (all-miss ≡ today's full-slot-scan).
        from yadgar._shared.protocols import NullCache  # noqa: PLC0415

        cache = NullCache()
        self._engram_slot_cache = cache
        return cache

    @observe(tier="hot", metric="storage.ops.resolve_scope_versions")
    def _resolve_scope_versions(self):
        """Return the injected :class:`ScopeVersions`, or the process-global one.

        The version-in-key store the slot read consults and the slot write bumps.
        A test may inject ``self._scope_versions``; otherwise the shared backend
        instance is used (slot writes + the slot read share one process).
        """
        sv = getattr(self, "_scope_versions", None)
        if sv is not None:
            return sv
        # Car 2 (folder-split #17): the lazy `backend.cache.get_scope_versions`
        # fallback is deleted (it was a _shared→backend edge). The composition root
        # injects the REAL process-global ScopeVersions; the bare default is a
        # _shared NullScopeVersions (frozen version 0 — harmless, since the paired
        # engram_slot/graph cache defaults to NullCache all-miss).
        from yadgar._shared.protocols import NullScopeVersions  # noqa: PLC0415

        sv = NullScopeVersions()
        self._scope_versions = sv
        return sv

    @observe(tier="hot", metric="storage.ops.bump_slot_version")
    def _bump_slot_version(self, slot_index: int) -> None:
        """Bump the ``slot`` scope version for ``slot_index`` (version-in-key).

        Called at the slot-write choke point (``assign_memory_slot``). Never fails
        a write — a bump miss only risks a briefly-stale NEW-member appearance,
        bounded by the next structural write to the slot."""
        try:
            self._resolve_scope_versions().bump("slot", slot_index)
        except Exception:  # noqa: BLE001 — version bump must never fail a write
            _log.debug("slot version bump failed for slot %s", slot_index, exc_info=True)

    # ── Car 4 (graph adjacency cache) — entity-scope version-in-key ───────────

    @observe(tier="hot", metric="storage.ops.resolve_graph_cache")
    def _resolve_graph_cache(self):
        """Return the injected ``graph`` cache, or the process-global default.

        Constructor-DI seam (mirrors ``_resolve_engram_slot_cache``): a
        ``StorageEngine`` may set ``self._graph_cache`` to a ``NullCache``
        (disable) or a test double; otherwise the shared registered instance is
        used. Import is deferred to keep the storage import graph clean."""
        cache = getattr(self, "_graph_cache", None)
        if cache is not None:
            return cache
        # Car 2 (folder-split #17): the lazy `backend.cache.get_graph_cache`
        # fallback is deleted (it was a _shared→backend edge). The composition root
        # injects the REAL registered instance; the bare default is a _shared
        # NullCache (all-miss ≡ today's uncached adjacency read).
        from yadgar._shared.protocols import NullCache  # noqa: PLC0415

        cache = NullCache()
        self._graph_cache = cache
        return cache

    @observe(tier="hot", metric="storage.ops.bump_entity_version")
    def _bump_entity_version(self, entity_id: int) -> None:
        """Bump the ``entity`` scope version for ``entity_id`` (Car 4 version-in-key).

        Called at EVERY relationship-write choke point (insert / reinforce / field
        update / delete). An edge lives in BOTH endpoints' adjacency, so both
        endpoints are bumped by the callers. Never fails a write — a bump miss
        risks a briefly-stale adjacency, bounded by the next edge write; over-bump
        is perf-only. The graph read is pure-structural (no fresh recheck), so
        delete/weight-change are NOT inert here — hence the bump on every mutation."""
        try:
            self._resolve_scope_versions().bump("entity", int(entity_id))
        except Exception:  # noqa: BLE001 — version bump must never fail a write
            _log.debug("entity version bump failed for entity %s", entity_id, exc_info=True)

    @observe(tier="stage", metric="storage.ops.get_slot_occupancy")
    def get_slot_occupancy(self) -> dict:
        """Return {slot_index: count} for all occupied slots."""
        rows = self._q(
            "SELECT slot_index, count() AS cnt FROM memory "
            "WHERE slot_index IS NOT NONE GROUP BY slot_index"
        )
        result = {}
        for row in rows:
            si = row.get("slot_index")
            cnt = int(row.get("cnt", 0))
            if si is not None:
                result[si] = cnt
        return result

    def get_memory_ids_in_slot(self, slot_index: int, limit: int = 100) -> list[int]:
        """Return memory IDs assigned to a given engram slot, up to limit."""
        rows = self._q(
            "SELECT VALUE meta::id(id) FROM memory WHERE slot_index = $slot LIMIT $lim",
            {"slot": slot_index, "lim": limit},
        )
        return [int(r) for r in (rows or [])]

    # ------------------------------------------------------------------ Checkpoints

    @observe(tier="stage", metric="storage.ops.insert_checkpoint")
    def insert_checkpoint(self, data: dict) -> int:
        """Replace any existing checkpoint for this directory.

        Old per-directory checkpoints are HARD-DELETED. Other directories untouched.
        is_active=true is kept on every row for backward compat with callers that
        still filter on it; get_active_checkpoint() now uses directory_context.
        """
        now = self._now_iso()
        cid = self._next_id("checkpoint")
        directory = data.get("directory_context", "")
        resume_hint = data.get("resume_hint", "") or f'restore(directory="{directory}")'
        # Hard-delete existing rows for this directory, then create new one.
        self._q(
            "BEGIN TRANSACTION;\n"
            "DELETE FROM checkpoint WHERE directory_context = $dir;\n"
            "CREATE type::record('checkpoint', $id) SET "
            "session_id = $session_id, directory_context = $dir, "
            "current_task = $task, files_being_edited = $files, "
            "key_decisions = $decisions, open_questions = $questions, "
            "next_steps = $steps, active_errors = $errors, "
            "custom_context = $custom, epoch = $epoch, "
            "resume_hint = $hint, "
            "created_at = $now, is_active = true;\n"
            "COMMIT TRANSACTION",
            {
                "id": cid,
                "session_id": data.get("session_id", "default"),
                "dir": directory,
                "task": data.get("current_task", ""),
                "files": data.get("files_being_edited", []),
                "decisions": data.get("key_decisions", []),
                "questions": data.get("open_questions", []),
                "steps": data.get("next_steps", []),
                "errors": data.get("active_errors", []),
                "custom": data.get("custom_context", ""),
                "epoch": data.get("epoch", 0),
                "hint": resume_hint,
                "now": now,
            },
        )
        return cid

    @observe(tier="stage", metric="storage.ops.get_active_checkpoint")
    def get_active_checkpoint(self, directory: str = "") -> dict | None:
        """Latest checkpoint for this directory. Empty directory = global most-recent."""
        if directory:
            rows = self._q(
                "SELECT * FROM checkpoint WHERE directory_context = $dir "
                "ORDER BY created_at DESC LIMIT 1",
                {"dir": directory},
            )
        else:
            rows = self._q("SELECT * FROM checkpoint ORDER BY created_at DESC LIMIT 1")
        if not rows:
            return None
        return self._row_to_dict(rows[0])

    @observe(tier="stage", metric="storage.ops.get_current_epoch")
    def get_current_epoch(self) -> int:
        """Get the current compaction epoch number."""
        rows = self._q("SELECT math::max(epoch) AS max_epoch FROM checkpoint GROUP ALL")
        if rows and rows[0].get("max_epoch") is not None:
            return int(rows[0]["max_epoch"])
        return 0

    def increment_epoch(self) -> int:
        """Increment and return the new epoch number."""
        current = self.get_current_epoch()
        return current + 1

    def update_checkpoint_epoch(self, checkpoint_id: int, epoch: int):
        """Update the epoch field on an existing checkpoint."""
        self._q(
            "UPDATE type::record('checkpoint', $id) SET epoch = $epoch",
            {"id": checkpoint_id, "epoch": epoch},
        )


# ── Module-level helpers ────────────────────────────────────────────────────


def purge_expired_archives(
    storage,
    dry_run: bool = False,
    retention_days_override: int | None = None,
) -> dict:
    """Purge memory_archive rows older than retention threshold.

    Thin wrapper around storage.purge_expired_archives() for callers that
    prefer the standalone-function calling convention.

    Returns dict:
      {
        "candidates": int,            # total matched (before circuit-breaker cap)
        "purged": int,                # actual rows deleted (0 if dry_run)
        "skipped_protected": int,
        "skipped_anchor": int,
        "skipped_recent": int,
        "circuit_breaker_hit": bool,
        "candidate_ids": list[int],   # first 10 candidate IDs
      }
    """
    return storage.purge_expired_archives(
        dry_run=dry_run,
        retention_days_override=retention_days_override,
    )


@observe(tier="stage", metric="storage.ops.vacuum_checkpoints")
def vacuum_checkpoints(storage, *, dry_run: bool = True) -> dict:
    """Collapse stale checkpoints: keep latest per directory_context, delete rest.

    Idempotent. Call with dry_run=False after v5.6.5 deploy to clean up rows
    accumulated under the old global-deactivate scheme.

    Returns:
        {
            "stale_count": int,   # rows that would be / were deleted
            "deleted": int,       # 0 if dry_run=True
            "survivors": int,     # rows remaining after vacuum
            "dry_run": bool,
        }
    """
    # Fetch all checkpoint rows (id + directory_context + created_at).
    all_rows = storage._q("SELECT id, directory_context, created_at FROM checkpoint")
    if not all_rows:
        return {"stale_count": 0, "deleted": 0, "survivors": 0, "dry_run": dry_run}

    # Group by directory_context; find winner (latest created_at) per group.
    from collections import defaultdict

    groups: dict[str, list[dict]] = defaultdict(list)
    for row in all_rows:
        groups[row.get("directory_context", "")].append(row)

    stale_int_ids: list[int] = []
    for _dir, rows in groups.items():
        # Sort descending by created_at string (ISO-8601 sorts lexicographically).
        rows.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        # Winner stays; everything else is stale.
        for stale in rows[1:]:
            raw_id = stale["id"]
            # Normalise to int (raw row ids may be RecordID or "checkpoint:N" strings)
            stale_int_ids.append(storage._extract_id(raw_id))

    stale_count = len(stale_int_ids)
    deleted = 0

    if not dry_run:
        for iid in stale_int_ids:
            storage._q(
                "DELETE type::record('checkpoint', $id)",
                {"id": iid},
            )
            deleted += 1

    survivors_count = len(all_rows) - deleted
    return {
        "stale_count": stale_count,
        "deleted": deleted,
        "survivors": survivors_count,
        "dry_run": dry_run,
    }
