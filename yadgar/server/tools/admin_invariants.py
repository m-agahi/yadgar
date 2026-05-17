"""check_invariants tool and _run_check_invariants helper."""

from __future__ import annotations

import logging

import yadgar.server._state as _st
from yadgar.config import get_settings
from yadgar.server._app import _tool
from yadgar.server._helpers import _q_with_timeout
from yadgar.server.lifecycle import _get_storage

logger = logging.getLogger(__name__)

settings = get_settings()


def _run_check_invariants(storage) -> dict:  # type: ignore[no-untyped-def]
    """Core logic for check_invariants — separated so tests can call it directly.

    Auto-repairs fixable violations (dangling foreign keys with no information loss)
    and returns them in the 'fixed' list. Non-fixable structural issues remain in
    'violations'. ok=True only when violations is empty.

    Each table check runs with a per-table timeout
    (CHECK_INVARIANTS_QUERY_TIMEOUT_SECONDS, default 60 s).  On timeout the table
    is logged at WARN and recorded in 'timeouts'; remaining tables still run.
    'ok' is False whenever violations or timeouts is non-empty.
    """
    import datetime as _dt
    import sys as _sys

    # Look up settings via yadgar.server so patch("yadgar.server.settings", ...) takes effect.
    # Falls back to the module-level settings if the server module isn't loaded yet.
    _srv = _sys.modules.get("yadgar.server")
    _settings = getattr(_srv, "settings", None) if _srv is not None else None
    if _settings is None:
        _settings = settings

    violations: list[str] = []
    # warn_violations: non-repairable issues that are expected / low-severity.
    # Logged at WARN (not CRITICAL) but still count toward ok=False.
    warn_violations: list[str] = []
    fixed: list[str] = []
    counts: dict[str, int] = {}
    timed_out: list[str] = []

    query_timeout = _settings.CHECK_INVARIANTS_QUERY_TIMEOUT_SECONDS

    # ── helpers ──────────────────────────────────────────────────────────────

    # Timeout sentinel — catch both Python's TimeoutError and httpx's variant.
    def _is_timeout(exc: BaseException) -> bool:
        try:
            import httpx as _httpx

            if isinstance(exc, _httpx.TimeoutException):
                return True
        except ImportError:
            pass
        return isinstance(exc, TimeoutError)

    def _q_t(surql: str, params: dict | None = None) -> list:
        """_q with a per-call timeout — delegates to module-level _q_with_timeout."""
        return _q_with_timeout(storage, surql, params, timeout_seconds=query_timeout)

    def _count_q(surql: str, params: dict | None = None) -> int:
        rows = _q_t(surql, params)
        if not rows:
            return 0
        row = rows[0]
        return int(row.get("c", row.get("count", 0)))

    # ── 1. Dangling links ────────────────────────────────────────────────────

    # memory table row count (used repeatedly — short query, no special timeout)
    mem_count = _count_q("SELECT count() AS c FROM memory GROUP ALL")
    counts["memory"] = mem_count

    # memory_similarity_link dangling endpoints — FIXABLE (safe DELETE).
    #
    # Previous implementation used a correlated NOT IN subquery that SurrealDB
    # v3 re-evaluates per row → O(N*M) → timeout on large tables.
    #
    # Rewritten as a Python-side set-difference:
    #   1. Fetch all live memory IDs into a Python set (one indexed lookup).
    #   2. Fetch all MSL rows (source_memory_id, target_memory_id, id).
    #   3. Compute dangling set in Python — no correlated subquery.
    #   4. Issue targeted DELETE by ID list if needed.
    try:
        live_ids_rows = _q_t("SELECT VALUE meta::id(id) FROM memory")
        live_ids: set[int] = {int(x) for x in live_ids_rows if x is not None}

        msl_rows = _q_t(
            "SELECT meta::id(id) AS rid, source_memory_id, target_memory_id "
            "FROM memory_similarity_link"
        )
        dangling_rids: list[int] = []
        for row in msl_rows:
            src = row.get("source_memory_id")
            tgt = row.get("target_memory_id")
            if src not in live_ids or tgt not in live_ids:
                rid = row.get("rid")
                if rid is not None:
                    dangling_rids.append(rid)

        dangling_msl = len(dangling_rids)
        counts["memory_similarity_link_dangling"] = dangling_msl
        if dangling_msl:
            # Batch-delete by ID to avoid re-running the full scan.
            for rid in dangling_rids:
                try:
                    storage._q("DELETE type::record('memory_similarity_link', $rid)", {"rid": rid})
                except Exception as del_exc:
                    logger.warning(
                        "check_invariants: failed to delete dangling MSL row %s: %s", rid, del_exc
                    )
            fixed.append(
                f"Deleted {dangling_msl} memory_similarity_link rows referencing non-existent memory IDs"
            )
            logger.info(
                "check_invariants: auto-fixed %d dangling memory_similarity_link rows", dangling_msl
            )
            counts["memory_similarity_link_dangling"] = 0
    except Exception as exc:
        if _is_timeout(exc):
            logger.warning(
                "check_invariants: memory_similarity_link check timed out after %ds; "
                "skipping auto-repair this cycle",
                query_timeout,
            )
            timed_out.append("memory_similarity_link")
        else:
            logger.warning("check_invariants: memory_similarity_link check failed: %s", exc)

    # memory_transition dangling — safe to delete: orphan rows have no valid endpoints
    try:
        dangling_mt = _count_q(
            "SELECT count() AS c FROM memory_transition "
            "WHERE from_memory_id NOT IN (SELECT VALUE meta::id(id) FROM memory) "
            "OR to_memory_id NOT IN (SELECT VALUE meta::id(id) FROM memory) "
            "GROUP ALL"
        )
        counts["memory_transition_dangling"] = dangling_mt
        if dangling_mt:
            storage._q(
                "DELETE FROM memory_transition WHERE from_memory_id NOT IN "
                "(SELECT VALUE meta::id(id) FROM memory) OR to_memory_id NOT IN "
                "(SELECT VALUE meta::id(id) FROM memory)"
            )
            fixed.append(
                f"Deleted {dangling_mt} dangling memory_transition row(s) (both endpoints gone)"
            )
            logger.info(
                "check_invariants: auto-fixed %d dangling memory_transition row(s)", dangling_mt
            )
            counts["memory_transition_dangling"] = 0
    except Exception as exc:
        if _is_timeout(exc):
            logger.warning(
                "check_invariants: memory_transition check timed out after %ds; skipping this cycle",
                query_timeout,
            )
            timed_out.append("memory_transition")
        else:
            logger.warning("check_invariants: memory_transition check failed: %s", exc)

    # memory_archive dangling — NOT fixable (archival records)
    try:
        dangling_ma = _count_q(
            "SELECT count() AS c FROM memory_archive "
            "WHERE original_memory_id NOT IN (SELECT VALUE meta::id(id) FROM memory) "
            "GROUP ALL"
        )
        counts["memory_archive_dangling"] = dangling_ma
        if dangling_ma:
            violations.append(
                f"{dangling_ma} memory_archive rows reference non-existent memory IDs"
            )
    except Exception as exc:
        if _is_timeout(exc):
            logger.warning(
                "check_invariants: memory_archive check timed out after %ds; skipping this cycle",
                query_timeout,
            )
            timed_out.append("memory_archive")
        else:
            logger.warning("check_invariants: memory_archive check failed: %s", exc)

    # caused_by dangling entity endpoints — FIXABLE (safe DELETE: no information loss)
    # Other relationship types — NOT fixable (structural data)
    try:
        ent_count = _count_q("SELECT count() AS c FROM entity GROUP ALL")
        counts["entity"] = ent_count

        # Fetch live entity IDs into a Python set for O(1) lookup.
        live_ent_ids_rows = _q_t("SELECT VALUE meta::id(id) FROM entity")
        live_ent_ids: set[int] = {int(x) for x in live_ent_ids_rows if x is not None}

        # Safety guard: if the ID fetch returned nothing but the count query said >0 rows
        # exist, this is a transient query glitch.  Proceeding would treat every caused_by
        # row as dangling and mass-delete all of them.  Skip and let the next cycle retry.
        if not live_ent_ids and ent_count > 0:
            logger.critical(
                "check_invariants: live_ent_ids is empty but ent_count=%d — "
                "possible transient query glitch; skipping dangling-relationship detection "
                "this cycle to avoid mass deletion",
                ent_count,
            )
        else:
            # Fetch all relationship rows that have dangling endpoints.
            dangling_rel_rows = _q_t(
                "SELECT meta::id(id) AS rid, relationship_type, source_entity_id, target_entity_id "
                "FROM relationship"
            )
            dangling_caused_by_rids: list[int] = []
            dangling_other_count = 0
            for row in dangling_rel_rows:
                src = row.get("source_entity_id")
                tgt = row.get("target_entity_id")
                is_dangling = src not in live_ent_ids or tgt not in live_ent_ids
                if not is_dangling:
                    continue
                if row.get("relationship_type") == "caused_by":
                    rid = row.get("rid")
                    if rid is not None:
                        dangling_caused_by_rids.append(rid)
                else:
                    dangling_other_count += 1

            dangling_caused_by = len(dangling_caused_by_rids)
            counts["caused_by_dangling"] = dangling_caused_by
            if dangling_caused_by:
                for rid in dangling_caused_by_rids:
                    try:
                        storage._q("DELETE type::record('relationship', $rid)", {"rid": rid})
                    except Exception as del_exc:
                        logger.warning(
                            "check_invariants: failed to delete dangling caused_by row %s: %s",
                            rid,
                            del_exc,
                        )
                fixed.append(
                    f"Deleted {dangling_caused_by} caused_by relationship rows referencing "
                    f"non-existent entity IDs"
                )
                logger.info(
                    "check_invariants: auto-fixed %d dangling caused_by rows", dangling_caused_by
                )
                counts["caused_by_dangling"] = 0

            # Renamed from "relationship_dangling" in v4.9: caused_by got its own count key
            # above; this key now represents non-caused_by dangling relationships only.
            counts["relationship_dangling_other"] = dangling_other_count
            if dangling_other_count:
                violations.append(
                    f"{dangling_other_count} relationship rows (non-caused_by) reference "
                    f"non-existent entity IDs"
                )
    except Exception as exc:
        if _is_timeout(exc):
            logger.warning(
                "check_invariants: relationship/caused_by check timed out after %ds; "
                "skipping this cycle",
                query_timeout,
            )
            timed_out.append("relationship")
        else:
            logger.warning("check_invariants: relationship/caused_by check failed: %s", exc)

    # caused_by row-count ceiling — prune oldest by created_at when exceeded.
    try:
        caused_by_ceiling = _settings.MAX_CAUSED_BY_ROWS
        if caused_by_ceiling > 0:
            caused_by_count = _count_q(
                "SELECT count() AS c FROM relationship "
                "WHERE relationship_type = 'caused_by' GROUP ALL"
            )
            counts["caused_by"] = caused_by_count
            if caused_by_count > caused_by_ceiling:
                excess = caused_by_count - caused_by_ceiling
                # Fetch oldest rows to prune (by created_at ascending — oldest first).
                # Fetch all matching rows and slice in Python to avoid SurrealDB v3
                # LIMIT-with-parameter incompatibilities on the relationship table.
                oldest_rows_all = _q_t(
                    "SELECT meta::id(id) AS rid, created_at FROM relationship "
                    "WHERE relationship_type = 'caused_by' "
                    "ORDER BY created_at ASC"
                )
                oldest_rows = oldest_rows_all[:excess]
                pruned = 0
                for row in oldest_rows:
                    rid = row.get("rid")
                    if rid is not None:
                        try:
                            storage._q("DELETE type::record('relationship', $rid)", {"rid": rid})
                            pruned += 1
                        except Exception as del_exc:
                            logger.warning(
                                "check_invariants: failed to prune caused_by row %s: %s",
                                rid,
                                del_exc,
                            )
                if pruned:
                    fixed.append(
                        f"Pruned {pruned} oldest caused_by rows (ceiling={caused_by_ceiling})"
                    )
                    logger.info(
                        "check_invariants: pruned %d oldest caused_by rows (ceiling=%d)",
                        pruned,
                        caused_by_ceiling,
                    )
    except Exception as exc:
        if _is_timeout(exc):
            logger.warning(
                "check_invariants: caused_by ceiling check timed out after %ds; "
                "skipping this cycle",
                query_timeout,
            )
            timed_out.append("caused_by_ceiling")
        else:
            logger.warning("check_invariants: caused_by ceiling check failed: %s", exc)

    # wiki_crossref dangling slugs — FIXABLE (safe DELETE, slugs are just links)
    try:
        slug_rows = storage._q("SELECT VALUE slug FROM wiki_page")
        valid_slugs = set(slug_rows) if slug_rows else set()
        all_refs = storage.get_all_wiki_crossrefs()
        dangling_xrefs = [
            r
            for r in all_refs
            if r.get("from_slug") not in valid_slugs or r.get("to_slug") not in valid_slugs
        ]
        dangling_xref = len(dangling_xrefs)
        counts["wiki_crossref_dangling"] = dangling_xref
        if dangling_xref:
            # Delete each dangling crossref row
            for ref in dangling_xrefs:
                try:
                    storage._q(
                        "DELETE wiki_crossref WHERE from_slug = $fs AND to_slug = $ts",
                        {"fs": ref.get("from_slug"), "ts": ref.get("to_slug")},
                    )
                except Exception as del_exc:
                    logger.warning(
                        "check_invariants: failed to delete wiki_crossref row: %s", del_exc
                    )
            fixed.append(
                f"Deleted {dangling_xref} wiki_crossref rows referencing non-existent page slugs"
            )
            logger.info(
                "check_invariants: auto-fixed %d dangling wiki_crossref rows", dangling_xref
            )
            counts["wiki_crossref_dangling"] = 0
    except Exception as exc:
        if _is_timeout(exc):
            logger.warning(
                "check_invariants: wiki_crossref check timed out after %ds; skipping this cycle",
                query_timeout,
            )
            timed_out.append("wiki_crossref")
        else:
            logger.warning("check_invariants: wiki_crossref check failed: %s", exc)

    # ── 2. memory:N orphan entities — FIXABLE (safe DELETE, purely derived data) ─
    try:
        mem_entity_rows = storage._q(
            "SELECT meta::id(id) AS eid, name FROM entity "
            "WHERE string::starts_with(name, 'memory:')"
        )
        orphan_eids: list[int] = []
        mem_ids_set: set[int] = set()
        if mem_count > 0:
            id_rows = storage._q("SELECT VALUE meta::id(id) FROM memory")
            mem_ids_set = {int(x) for x in id_rows if x is not None}
        for row in mem_entity_rows:
            name = row.get("name", "")
            suffix = name.split(":", 1)[1] if ":" in name else ""
            try:
                mid = int(suffix)
            except (ValueError, TypeError) as _e:
                continue
            if mid not in mem_ids_set:
                eid = row.get("eid")
                if eid is not None:
                    orphan_eids.append(eid)
        orphan_count = len(orphan_eids)
        counts["memory_entity_orphans"] = orphan_count
        if orphan_count:
            for eid in orphan_eids:
                try:
                    storage._q(
                        "DELETE type::record('entity', $eid)",
                        {"eid": eid},
                    )
                except Exception as del_exc:
                    logger.warning(
                        "check_invariants: failed to delete orphan entity %s: %s", eid, del_exc
                    )
            fixed.append(
                f"Deleted {orphan_count} entity rows named 'memory:<N>' where N is not a live memory ID"
            )
            logger.info("check_invariants: auto-fixed %d memory entity orphans", orphan_count)
            counts["memory_entity_orphans"] = 0
    except Exception as exc:
        if _is_timeout(exc):
            logger.warning(
                "check_invariants: memory entity orphan check timed out after %ds; "
                "skipping this cycle",
                query_timeout,
            )
            timed_out.append("memory_entity_orphans")
        else:
            logger.warning("check_invariants: memory entity orphan check failed: %s", exc)

    # ── 3. Row-count ceilings (non-fixable — structural) ────────────────────
    _CEILINGS = {
        "action_log": 100_000,
        "episode": 10_000,
        "wiki_page": 5_000,
    }
    for table, ceiling in _CEILINGS.items():
        try:
            n = _count_q(f"SELECT count() AS c FROM {table} GROUP ALL")
            counts[table] = n
            if n > ceiling:
                violations.append(f"{table} has {n} rows (ceiling {ceiling}) — consider pruning")
        except Exception as exc:
            if _is_timeout(exc):
                logger.warning(
                    "check_invariants: %s ceiling check timed out after %ds; skipping this cycle",
                    table,
                    query_timeout,
                )
                timed_out.append(f"{table}_ceiling")
            else:
                logger.warning("check_invariants: %s ceiling check failed: %s", table, exc)

    # memory_similarity_link ceiling (dynamic, non-fixable)
    try:
        msl_count = _count_q("SELECT count() AS c FROM memory_similarity_link GROUP ALL")
        counts["memory_similarity_link"] = msl_count
        msl_ceiling = mem_count * _settings.MAX_SIMILARITY_LINKS_PER_MEMORY * 2
        if msl_ceiling > 0 and msl_count > msl_ceiling:
            violations.append(
                f"memory_similarity_link has {msl_count} rows (ceiling {msl_ceiling})"
            )
    except Exception as exc:
        if _is_timeout(exc):
            logger.warning(
                "check_invariants: msl ceiling check timed out after %ds; skipping this cycle",
                query_timeout,
            )
            timed_out.append("memory_similarity_link_ceiling")
        else:
            logger.warning("check_invariants: msl ceiling check failed: %s", exc)

    # ── 4. Engram slot distribution ───────────────────────────────────────────
    try:
        if mem_count > 0:
            # Attempt rebalancing first if the allocator is available
            if _st._engram is not None:
                moved = _st._engram.rebalance_if_needed(threshold_pct=0.05)
                if moved:
                    fixed.append(
                        f"Rebalanced engram slots: moved {moved} memories from over-occupied slots"
                    )
                    logger.info(
                        "check_invariants: rebalanced %d memories from over-occupied engram slots",
                        moved,
                    )

            # Re-check occupancy after any rebalancing
            slot_rows = storage._q(
                "SELECT slot_index, count() AS n FROM memory "
                "WHERE slot_index IS NOT NONE GROUP BY slot_index"
            )
            threshold = max(1, int(mem_count * 0.05))
            for row in slot_rows:
                n = int(row.get("n", 0))
                slot = row.get("slot_index")
                if n > threshold:
                    violations.append(
                        f"Slot {slot} holds {n} memories (>{threshold}, >5% of {mem_count}) — engram collapse?"
                    )
    except Exception as exc:
        if _is_timeout(exc):
            logger.warning(
                "check_invariants: slot distribution check timed out after %ds; skipping this cycle",
                query_timeout,
            )
            timed_out.append("engram_slot_distribution")
        else:
            logger.warning("check_invariants: slot distribution check failed: %s", exc)

    # ── 5. Engram slot table integrity (non-fixable — structural) ───────────
    try:
        engram_count = _count_q("SELECT count() AS c FROM engram_slot GROUP ALL")
        counts["engram_slot"] = engram_count
        expected = _settings.HOPFIELD_MAX_PATTERNS
        if engram_count != expected:
            violations.append(
                f"engram_slot has {engram_count} rows (expected {expected} = HOPFIELD_MAX_PATTERNS)"
            )
    except Exception as exc:
        if _is_timeout(exc):
            logger.warning(
                "check_invariants: engram_slot check timed out after %ds; skipping this cycle",
                query_timeout,
            )
            timed_out.append("engram_slot")
        else:
            logger.warning("check_invariants: engram_slot check failed: %s", exc)

    # ── 6. DB-size telemetry ─────────────────────────────────────────────────
    try:
        db_size = storage.get_db_size()
        if db_size["size_warning"]:
            # Throttle WARN to at most once per hour.
            current_hour = _dt.datetime.now(_dt.UTC).hour
            if current_hour != _st._db_size_warn_last_logged_hour:
                _st._db_size_warn_last_logged_hour = current_hour
                logger.warning(
                    "check_invariants: db_size %d bytes exceeds warning threshold %d bytes "
                    "(vlog=%d, sstables=%d, wal=%d)",
                    db_size["db_size_bytes"],
                    _settings.DB_SIZE_WARNING_BYTES,
                    db_size["vlog_size_bytes"],
                    db_size["sstables_size_bytes"],
                    db_size["wal_size_bytes"],
                )
    except Exception as exc:
        logger.warning("check_invariants: db_size telemetry failed: %s", exc)
        db_size = {}

    # ── 7. Per-table size breakdown ──────────────────────────────────────────
    # Uses the module-level _PER_TABLE_FIELDS constant (shared with memory_stats).
    per_table: dict[str, dict] = {}
    for _tbl, _content_field in _st._PER_TABLE_FIELDS.items():
        try:
            if _content_field:
                _rows = _q_t(
                    f"SELECT count() AS c, "
                    f"math::sum(string::len({_content_field})) AS content_bytes "
                    f"FROM {_tbl} GROUP ALL"
                )
            else:
                _rows = _q_t(f"SELECT count() AS c FROM {_tbl} GROUP ALL")
            if _rows:
                _r = _rows[0]
                _row_count = int(_r.get("c", 0))
                _entry: dict = {"rows": _row_count}
                if _content_field:
                    _entry["estimated_bytes"] = int(_r.get("content_bytes") or 0)
                per_table[_tbl] = _entry
            else:
                per_table[_tbl] = {"rows": 0}
        except Exception as _tbl_exc:
            logger.warning(
                "check_invariants: per_table size query failed for %s: %s", _tbl, _tbl_exc
            )
            per_table[_tbl] = {"rows": 0, "error": str(_tbl_exc)}

    if db_size:
        db_size["per_table"] = per_table

    # ok=False when any violations, warn_violations, or timeouts exist
    ok = len(violations) == 0 and len(warn_violations) == 0 and len(timed_out) == 0
    for v in violations:
        logger.critical("check_invariants: %s", v)
    for v in warn_violations:
        logger.warning("check_invariants: %s", v)

    all_violations = violations + warn_violations
    result: dict = {
        "ok": ok,
        "violations": all_violations,
        "fixed": fixed,
        "counts": counts,
    }
    if timed_out:
        result["timeouts"] = timed_out
    if db_size:
        result["db_size"] = db_size

    return result


@_tool(power=True)
def check_invariants() -> dict:
    """Run consistency checks on the memory store, auto-repairing fixable issues.

    Returns {"ok": bool, "violations": [...], "fixed": [...], "counts": {...}}.
    - violations: unfixable structural problems (ceiling breaches, slot anomalies, etc.)
    - fixed: descriptions of auto-repaired issues (dangling FK rows deleted)
    - ok: True only when violations is empty (fixed items don't affect ok)
    Logs INFO for each auto-repair, CRITICAL for each remaining violation.
    """
    storage = _get_storage()
    return _run_check_invariants(storage)
