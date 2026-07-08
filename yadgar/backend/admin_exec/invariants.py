"""Backend execution body for the check_invariants admin op (R3 Car 3d / R5).

``check_invariants`` runs consistency checks over the memory store AND
auto-repairs fixable issues (dangling FK rows, orphan entities, crossref
stragglers, ceiling prunes) via ``storage._q`` DELETEs / ``delete_relationship``.
Because it WRITES, the whole compute forwards here: the core ``@_tool`` shell is
a no-arg, no-gate ``return _forward_admin("check_invariants", {})``.

The check helpers were relocated verbatim from
``yadgar.core.server.tools.admin_invariants``. They import only ``_shared``
(``_q_with_timeout`` moved to ``_shared.server_helpers`` for this). ``settings``
is looked up via a soft ``sys.modules`` probe of ``yadgar.core.server`` (test
patch hook) that falls back to the module-level settings — no core import.
"""

from __future__ import annotations

import logging

import yadgar._shared.runtime.state as _st
from yadgar._shared.config import get_settings
from yadgar._shared.observability.observe import observe
from yadgar._shared.runtime.lifecycle import _get_storage
from yadgar._shared.server_helpers import _q_with_timeout

logger = logging.getLogger(__name__)

settings = get_settings()


# ── shared low-level helpers (module-level so check helpers can call them) ───


@observe(tier="hot", metric="tools.admin_invariants._is_timeout")
def _is_timeout(exc: BaseException) -> bool:
    """Return True if *exc* is any recognised timeout variant."""
    try:
        import httpx as _httpx

        if isinstance(exc, _httpx.TimeoutException):
            return True
    except ImportError:
        pass
    return isinstance(exc, TimeoutError)


def _q_t(storage, query_timeout: int, surql: str, params: dict | None = None) -> list:
    """_q with a per-call timeout — delegates to module-level _q_with_timeout."""
    return _q_with_timeout(storage, surql, params, timeout_seconds=query_timeout)


@observe(tier="stage", metric="tools.admin_invariants._count_q")
def _count_q(storage, query_timeout: int, surql: str, params: dict | None = None) -> int:
    rows = _q_t(storage, query_timeout, surql, params)
    if not rows:
        return 0
    row = rows[0]
    return int(row.get("c", row.get("count", 0)))


@observe(tier="stage", metric="tools.admin_invariants._delete_record")
def _delete_record(storage, table: str, rid: int, label: str) -> None:
    """Delete a single record by integer id, logging on failure."""
    try:
        storage._q(f"DELETE type::record('{table}', $rid)", {"rid": rid})
    except Exception as del_exc:
        logger.warning("check_invariants: failed to delete %s row %s: %s", label, rid, del_exc)


@observe(tier="stage", metric="tools.admin_invariants._delete_records")
def _delete_records(storage, table: str, rids: list[int], label: str) -> None:
    """Batch-delete records by integer id list, logging each failure."""
    for rid in rids:
        _delete_record(storage, table, rid, label)


# ── per-invariant check helpers ──────────────────────────────────────────────


@observe(tier="stage", metric="tools.admin_invariants._check_memory_similarity_link")
def _check_memory_similarity_link(
    storage,
    query_timeout: int,
    live_ids: set[int],
    counts: dict,
    fixed: list[str],
    timed_out: list[str],
) -> None:
    """Check and auto-repair dangling memory_similarity_link rows."""
    try:
        live_ids_rows = _q_t(storage, query_timeout, "SELECT VALUE meta::id(id) FROM memory")
        live_ids.update(int(x) for x in live_ids_rows if x is not None)

        msl_rows = _q_t(
            storage,
            query_timeout,
            "SELECT meta::id(id) AS rid, source_memory_id, target_memory_id "
            "FROM memory_similarity_link",
        )
        dangling_rids = [
            row["rid"]
            for row in msl_rows
            if (
                row.get("source_memory_id") not in live_ids
                or row.get("target_memory_id") not in live_ids
            )
            and row.get("rid") is not None
        ]
        dangling_msl = len(dangling_rids)
        counts["memory_similarity_link_dangling"] = dangling_msl
        if not dangling_msl:
            return
        _delete_records(storage, "memory_similarity_link", dangling_rids, "dangling MSL")
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


@observe(tier="stage", metric="tools.admin_invariants._check_memory_transition")
def _check_memory_transition(
    storage,
    query_timeout: int,
    counts: dict,
    fixed: list[str],
    timed_out: list[str],
) -> None:
    """Check and auto-repair dangling memory_transition rows."""
    try:
        dangling_mt = _count_q(
            storage,
            query_timeout,
            "SELECT count() AS c FROM memory_transition "
            "WHERE from_memory_id NOT IN (SELECT VALUE meta::id(id) FROM memory) "
            "OR to_memory_id NOT IN (SELECT VALUE meta::id(id) FROM memory) "
            "GROUP ALL",
        )
        counts["memory_transition_dangling"] = dangling_mt
        if not dangling_mt:
            return
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


@observe(tier="stage", metric="tools.admin_invariants._check_memory_archive")
def _check_memory_archive(
    storage,
    query_timeout: int,
    counts: dict,
    violations: list[str],
    timed_out: list[str],
) -> None:
    """Check for dangling memory_archive rows (NOT fixable — archival records)."""
    try:
        dangling_ma = _count_q(
            storage,
            query_timeout,
            "SELECT count() AS c FROM memory_archive "
            "WHERE original_memory_id NOT IN (SELECT VALUE meta::id(id) FROM memory) "
            "GROUP ALL",
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


@observe(tier="stage", metric="tools.admin_invariants._repair_dangling_caused_by")
def _repair_dangling_caused_by(
    storage,
    dangling_rel_rows: list,
    live_ent_ids: set[int],
    counts: dict,
    violations: list[str],
    fixed: list[str],
) -> None:
    """Partition dangling relationship rows, fix caused_by, record others as violations."""
    dangling_caused_by_rids: list[int] = []
    dangling_other_count = 0
    for row in dangling_rel_rows:
        src = row.get("source_entity_id")
        tgt = row.get("target_entity_id")
        if src in live_ent_ids and tgt in live_ent_ids:
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
        _delete_records(storage, "relationship", dangling_caused_by_rids, "dangling caused_by")
        fixed.append(
            f"Deleted {dangling_caused_by} caused_by relationship rows referencing "
            f"non-existent entity IDs"
        )
        logger.info("check_invariants: auto-fixed %d dangling caused_by rows", dangling_caused_by)
        counts["caused_by_dangling"] = 0

    # Renamed from "relationship_dangling" in v4.9: caused_by got its own count key
    # above; this key now represents non-caused_by dangling relationships only.
    counts["relationship_dangling_other"] = dangling_other_count
    if dangling_other_count:
        violations.append(
            f"{dangling_other_count} relationship rows (non-caused_by) reference "
            f"non-existent entity IDs"
        )


@observe(tier="stage", metric="tools.admin_invariants._check_relationships")
def _check_relationships(
    storage,
    query_timeout: int,
    counts: dict,
    violations: list[str],
    fixed: list[str],
    timed_out: list[str],
) -> None:
    """Check caused_by (FIXABLE) and other relationship dangling endpoints (NOT fixable)."""
    try:
        ent_count = _count_q(storage, query_timeout, "SELECT count() AS c FROM entity GROUP ALL")
        counts["entity"] = ent_count

        live_ent_ids_rows = _q_t(storage, query_timeout, "SELECT VALUE meta::id(id) FROM entity")
        live_ent_ids: set[int] = {int(x) for x in live_ent_ids_rows if x is not None}

        if not live_ent_ids and ent_count > 0:
            logger.critical(
                "check_invariants: live_ent_ids is empty but ent_count=%d — "
                "possible transient query glitch; skipping dangling-relationship detection "
                "this cycle to avoid mass deletion",
                ent_count,
            )
            return

        dangling_rel_rows = _q_t(
            storage,
            query_timeout,
            "SELECT meta::id(id) AS rid, relationship_type, source_entity_id, target_entity_id "
            "FROM relationship",
        )
        _repair_dangling_caused_by(
            storage, dangling_rel_rows, live_ent_ids, counts, violations, fixed
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


@observe(tier="stage", metric="tools.admin_invariants._prune_caused_by_rows")
def _prune_caused_by_rows(storage, query_timeout: int, excess: int, ceiling: int) -> int:
    """Prune *excess* oldest caused_by rows. Returns count actually pruned."""
    oldest_rows_all = _q_t(
        storage,
        query_timeout,
        "SELECT meta::id(id) AS rid, created_at FROM relationship "
        "WHERE relationship_type = 'caused_by' "
        "ORDER BY created_at ASC",
    )
    oldest_rows = oldest_rows_all[:excess]
    pruned = 0
    for row in oldest_rows:
        rid = row.get("rid")
        if rid is None:
            continue
        try:
            # Car 4: route through delete_relationship so both endpoint entities'
            # graph-cache versions are bumped (a raw DELETE would leave the pruned
            # edge in cached adjacency — the read is pure-structural, no recheck).
            storage.delete_relationship(int(rid))
            pruned += 1
        except Exception as del_exc:
            logger.warning("check_invariants: failed to prune caused_by row %s: %s", rid, del_exc)
    return pruned


@observe(tier="stage", metric="tools.admin_invariants._check_caused_by_ceiling")
def _check_caused_by_ceiling(
    storage,
    query_timeout: int,
    _settings,
    counts: dict,
    fixed: list[str],
    timed_out: list[str],
) -> None:
    """Prune oldest caused_by rows if ceiling is exceeded."""
    try:
        caused_by_ceiling = _settings.MAX_CAUSED_BY_ROWS
        if caused_by_ceiling <= 0:
            return
        caused_by_count = _count_q(
            storage,
            query_timeout,
            "SELECT count() AS c FROM relationship WHERE relationship_type = 'caused_by' GROUP ALL",
        )
        counts["caused_by"] = caused_by_count
        if caused_by_count <= caused_by_ceiling:
            return
        excess = caused_by_count - caused_by_ceiling
        pruned = _prune_caused_by_rows(storage, query_timeout, excess, caused_by_ceiling)
        if pruned:
            fixed.append(f"Pruned {pruned} oldest caused_by rows (ceiling={caused_by_ceiling})")
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


@observe(tier="stage", metric="tools.admin_invariants._check_wiki_crossref")
def _check_wiki_crossref(
    storage,
    query_timeout: int,
    counts: dict,
    fixed: list[str],
    timed_out: list[str],
) -> None:
    """Check and auto-repair dangling wiki_crossref rows."""
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
        if not dangling_xref:
            return
        for ref in dangling_xrefs:
            try:
                storage._q(
                    "DELETE wiki_crossref WHERE from_slug = $fs AND to_slug = $ts",
                    {"fs": ref.get("from_slug"), "ts": ref.get("to_slug")},
                )
            except Exception as del_exc:
                logger.warning("check_invariants: failed to delete wiki_crossref row: %s", del_exc)
        fixed.append(
            f"Deleted {dangling_xref} wiki_crossref rows referencing non-existent page slugs"
        )
        logger.info("check_invariants: auto-fixed %d dangling wiki_crossref rows", dangling_xref)
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


@observe(tier="hot", metric="tools.admin_invariants._parse_memory_id")
def _parse_memory_id(suffix: str) -> int | None:
    """Return int(suffix) or None if suffix is not a valid integer."""
    if not suffix:
        return None
    try:
        return int(suffix)
    except ValueError:
        return None


@observe(tier="hot", metric="tools.admin_invariants._collect_orphan_entity_ids")
def _collect_orphan_entity_ids(mem_entity_rows: list, mem_ids_set: set[int]) -> list[int]:
    """Return entity IDs from memory:<N> rows where N is not a live memory ID."""
    orphan_eids: list[int] = []
    for row in mem_entity_rows:
        name = row.get("name", "")
        suffix = name.split(":", 1)[1] if ":" in name else ""
        mid = _parse_memory_id(suffix)
        if mid is None:
            continue
        if mid not in mem_ids_set:
            eid = row.get("eid")
            if eid is not None:
                orphan_eids.append(eid)
    return orphan_eids


@observe(tier="stage", metric="tools.admin_invariants._check_memory_entity_orphans")
def _check_memory_entity_orphans(
    storage,
    query_timeout: int,
    mem_count: int,
    counts: dict,
    fixed: list[str],
    timed_out: list[str],
) -> None:
    """Check and auto-repair memory:<N> entity rows that reference non-existent memories."""
    try:
        mem_entity_rows = storage._q(
            "SELECT meta::id(id) AS eid, name FROM entity "
            "WHERE string::starts_with(name, 'memory:')"
        )
        mem_ids_set: set[int] = set()
        if mem_count > 0:
            id_rows = storage._q("SELECT VALUE meta::id(id) FROM memory")
            mem_ids_set = {int(x) for x in id_rows if x is not None}
        orphan_eids = _collect_orphan_entity_ids(mem_entity_rows, mem_ids_set)
        orphan_count = len(orphan_eids)
        counts["memory_entity_orphans"] = orphan_count
        if not orphan_count:
            return
        _delete_records(storage, "entity", orphan_eids, "orphan entity")
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


@observe(tier="stage", metric="tools.admin_invariants._check_row_count_ceilings")
def _check_row_count_ceilings(
    storage,
    query_timeout: int,
    counts: dict,
    violations: list[str],
    timed_out: list[str],
) -> None:
    """Check row-count ceilings for action_log, episode, wiki_page (non-fixable)."""
    _CEILINGS = {
        "action_log": 100_000,
        "episode": 10_000,
        "wiki_page": 5_000,
    }
    for table, ceiling in _CEILINGS.items():
        try:
            n = _count_q(storage, query_timeout, f"SELECT count() AS c FROM {table} GROUP ALL")
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


@observe(tier="stage", metric="tools.admin_invariants._check_msl_ceiling")
def _check_msl_ceiling(
    storage,
    query_timeout: int,
    _settings,
    mem_count: int,
    counts: dict,
    violations: list[str],
    timed_out: list[str],
) -> None:
    """Check memory_similarity_link row-count ceiling (dynamic, non-fixable)."""
    try:
        msl_count = _count_q(
            storage, query_timeout, "SELECT count() AS c FROM memory_similarity_link GROUP ALL"
        )
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


@observe(tier="stage", metric="tools.admin_invariants._check_engram_slot_distribution")
def _check_engram_slot_distribution(
    storage,
    query_timeout: int,
    mem_count: int,
    violations: list[str],
    fixed: list[str],
    timed_out: list[str],
) -> None:
    """Check engram slot distribution and attempt rebalancing if over-occupied."""
    if mem_count <= 0:
        return
    try:
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


@observe(tier="stage", metric="tools.admin_invariants._check_engram_slot_integrity")
def _check_engram_slot_integrity(
    storage,
    query_timeout: int,
    _settings,
    counts: dict,
    violations: list[str],
    timed_out: list[str],
) -> None:
    """Check engram_slot table integrity (non-fixable — structural)."""
    try:
        engram_count = _count_q(
            storage, query_timeout, "SELECT count() AS c FROM engram_slot GROUP ALL"
        )
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


@observe(tier="stage", metric="tools.admin_invariants._check_db_size")
def _check_db_size(storage, _settings) -> dict:
    """Collect DB-size telemetry; log warning throttled to once per hour. Returns db_size dict."""
    import datetime as _dt

    try:
        db_size = storage.get_db_size()
        if db_size["size_warning"]:
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
    return db_size


@observe(tier="stage", metric="tools.admin_invariants._check_per_table_size")
def _check_per_table_size(storage, query_timeout: int) -> dict:
    """Collect per-table size breakdown. Returns per_table dict."""
    per_table: dict[str, dict] = {}
    for _tbl, _content_field in _st._PER_TABLE_FIELDS.items():
        try:
            if _content_field:
                _rows = _q_t(
                    storage,
                    query_timeout,
                    f"SELECT count() AS c, "
                    f"math::sum(string::len({_content_field})) AS content_bytes "
                    f"FROM {_tbl} GROUP ALL",
                )
            else:
                _rows = _q_t(storage, query_timeout, f"SELECT count() AS c FROM {_tbl} GROUP ALL")
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
    return per_table


# ── orchestrator ─────────────────────────────────────────────────────────────


@observe(tier="stage", metric="tools.admin_invariants._run_check_invariants")
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
    import sys as _sys

    # Look up settings via yadgar.core.server so patch("yadgar.core.server.settings", ...) takes effect.
    # Falls back to the module-level settings if the server module isn't loaded yet.
    _srv = _sys.modules.get("yadgar.core.server")
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

    # ── 0. Base count (needed by several checks below) ───────────────────────
    # memory table row count (used repeatedly — short query, no special timeout)
    mem_count = _count_q(storage, query_timeout, "SELECT count() AS c FROM memory GROUP ALL")
    counts["memory"] = mem_count

    # ── 1. Dangling links ────────────────────────────────────────────────────

    # memory_similarity_link dangling endpoints — FIXABLE (safe DELETE).
    # Uses Python-side set-difference to avoid O(N*M) correlated subquery on SurrealDB v3.
    live_ids: set[int] = set()
    _check_memory_similarity_link(storage, query_timeout, live_ids, counts, fixed, timed_out)

    # memory_transition dangling — safe to delete: orphan rows have no valid endpoints
    _check_memory_transition(storage, query_timeout, counts, fixed, timed_out)

    # memory_archive dangling — NOT fixable (archival records)
    _check_memory_archive(storage, query_timeout, counts, violations, timed_out)

    # caused_by dangling entity endpoints — FIXABLE (safe DELETE: no information loss)
    # Other relationship types — NOT fixable (structural data)
    _check_relationships(storage, query_timeout, counts, violations, fixed, timed_out)

    # caused_by row-count ceiling — prune oldest by created_at when exceeded.
    _check_caused_by_ceiling(storage, query_timeout, _settings, counts, fixed, timed_out)

    # wiki_crossref dangling slugs — FIXABLE (safe DELETE, slugs are just links)
    _check_wiki_crossref(storage, query_timeout, counts, fixed, timed_out)

    # ── 2. memory:N orphan entities — FIXABLE (safe DELETE, purely derived data) ─
    _check_memory_entity_orphans(storage, query_timeout, mem_count, counts, fixed, timed_out)

    # ── 3. Row-count ceilings (non-fixable — structural) ────────────────────
    _check_row_count_ceilings(storage, query_timeout, counts, violations, timed_out)

    # memory_similarity_link ceiling (dynamic, non-fixable)
    _check_msl_ceiling(storage, query_timeout, _settings, mem_count, counts, violations, timed_out)

    # ── 4. Engram slot distribution ───────────────────────────────────────────
    _check_engram_slot_distribution(storage, query_timeout, mem_count, violations, fixed, timed_out)

    # ── 5. Engram slot table integrity (non-fixable — structural) ───────────
    _check_engram_slot_integrity(storage, query_timeout, _settings, counts, violations, timed_out)

    # ── 6. DB-size telemetry ─────────────────────────────────────────────────
    db_size = _check_db_size(storage, _settings)

    # ── 7. Per-table size breakdown ──────────────────────────────────────────
    # Uses the module-level _PER_TABLE_FIELDS constant (shared with memory_stats).
    per_table = _check_per_table_size(storage, query_timeout)
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


@observe(tier="boundary", metric="backend.admin.check_invariants")
def check_invariants(payload: dict) -> dict:
    """Run consistency checks + auto-repair over the memory store. Storage-write half.

    payload: {} (no args)
    Returns {"ok", "violations", "fixed", "counts", ...}. The auto-repair DELETEs
    happen inside ``_run_check_invariants``; this op is the backend entry point
    the core ``check_invariants`` shell forwards to.
    """
    return _run_check_invariants(_get_storage())
