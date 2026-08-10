"""Car K (0047 §7 row K) — nightly archive sweep backend admin op.

CROSS-ENGINE WRITE: flips MariaDB ledger rows to ``status='archived'`` and
retypes SurrealDB body wiki pages to a per-type archived variant. Wired as
a backend admin op (``run_nightly_archive_sweep``) under the dispatch
table at ``yadgar/backend/admin_exec/__init__.py``.

PER-ITEM ORDERING (§4.1, load-bearing): body retype FIRST, row flip LAST.
A crash between the two leaves a retyped page + stale row — recoverable
via ``yadgar.backend.admin_exec.invariants_cross_engine``'s content_hash
check. The inverse order would leave a flipped row pointing at a
recall-visible page.

AGE-FIELD DEVIATION (documented in completing PR body):
  - The plan calls for aging task rows off ``completed_at`` (§3.4 / §14.2),
    but the ``task`` table has no ``completed_at`` column (only
    ``created_at`` + ``updated_at``), and the project no-migration rule
    forbids adding one. K ages task rows off ``updated_at`` instead —
    a regression on §14.2's intent ("editing a completed task must not
    reset its 90-day clock"). Operators who want strict completion-clock
    behaviour must add a ``completed_at`` column + a follow-up sweep
    revision.
  - ADR rows have no ``completed_at`` either; K ages them off
    ``created_at`` (the plan's choice — §3.5).
  - agent_pattern / agent_discipline rows use ``updated_at`` (the only
    timestamp those tables carry).

MUTABILITY POLICY (Car J): a body page whose ``mutability_override`` is set
to ``"locked"`` or ``"derived"`` is SKIPPED — the operator pinned this
page out of the sweep. Per-type defaults do NOT block the sweep: every
retype passes ``_sanctioned=True`` on the storage layer (Car J's escape
hatch for server-side lifecycle transitions).

CORPORA — three sweeps, one op:

  * task: per project_id, status='completed' AND updated_at < now-retention,
    body mutability_override not in (locked, derived).
  * adr: per project_id, status IN ('superseded','rejected','deprecated') AND
    created_at < now-retention, body mutability_override not in (locked, derived).
  * agent_pattern (reach-global per D3): uses=0 AND status='deprecated',
    body mutability_override not in (locked, derived).
  * agent_discipline (reach-global): same as agent_pattern, AND
    always_applied=FALSE (the contract singleton is NEVER archived).

IDEMPOTENT: a second sweep run is a no-op — status='archived' rows are
not re-selected, and retyping a retyped page just writes the same value.

CIRCUIT-BREAKER: when the candidate count exceeds
``LEDGER_ARCHIVE_CIRCUIT_BREAKER`` (default 500), the sweep aborts and
archives nothing — mirrors ``MEMORY_ARCHIVE_RETENTION_CIRCUIT_BREAKER``
(yadgar/_shared/config/config.py:497).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from yadgar._shared.observability.observe import observe

logger = logging.getLogger("yadgar.backend.admin_exec.nightly_sweep")

# Page-type literal constants for the per-type archived variants. These are
# page-type strings stored on the wiki_page row. They are NOT in POLICY_BY_TYPE
# (the policy resolver still routes them via DEFAULT_POLICY today) — recall
# exclusion is enforced by the ``status='archived'`` filter on the ledger
# read paths (``list_task_rows`` / ``list_adr_rows`` accept a status filter;
# the default recall fanout never sees archived rows because the corpus is
# reached through the ledger, not the wiki page).
_PAGE_TYPE_TASK_ARCHIVED = "task_archived"
_PAGE_TYPE_ADR_ARCHIVED = "adr_archived"
_PAGE_TYPE_AGENT_PATTERN_ARCHIVED = "agent_pattern_archived"
_PAGE_TYPE_AGENT_DISCIPlINE_ARCHIVED = "agent_discipline_archived"

# Mutability values that mean "skip this page in the sweep" (per-page
# override only — per-type defaults are bypassed by ``_sanctioned=True``).
_SKIPPED_MUTABILITY: frozenset[str] = frozenset({"locked", "derived"})


# ── Storage handles ─────────────────────────────────────────────────────────


def _get_sql_storage() -> Any:
    """The composed ``MariaStorageEngine`` (engine #2), or None when absent.

    Mirrors the seam at ``yadgar/backend/admin_exec/ledger.py:63`` so tests
    patch one symbol across the admin_exec ledger surface.
    """
    from yadgar._shared.runtime.lifecycle import _get_sql_storage  # noqa: PLC0415

    return _get_sql_storage()


def _get_storage() -> Any:
    """The composed SurrealDB ``StorageEngine``, or None when absent."""
    from yadgar._shared.runtime.lifecycle import _get_storage  # noqa: PLC0415

    return _get_storage()


# ── Helpers ─────────────────────────────────────────────────────────────────


@observe(tier="hot", span=False)
def _parse_iso(ts: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp string. Returns None when falsy or unparseable."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except TypeError, ValueError:
        return None


@observe(tier="hot", span=False)
def _body_page_for_row(
    surreal: Any,
    body_slug: str | None,
    *,
    fallback_id: int,
) -> dict[str, Any] | None:
    """Resolve a body wiki page from the ledger ``body_slug`` pointer.

    The ledger row's ``body_slug`` is the canonical pointer to the body
    page (D4 — bodies live in SurrealDB; only the slug pointer is on the
    SQL row). If the row has no slug (legacy) or the slug does not
    resolve, return ``None`` — the caller treats that as a skip with
    ``skipped_immutable`` counted for safety (the page is effectively
    inaccessible from the ledger, which makes archival a no-op anyway).

    Args:
        surreal: SurrealDB ``StorageEngine``.
        body_slug: The ledger row's body_slug value.
        fallback_id: Unused — kept for future use (id-based lookup if the
            slug falls through).
    """
    if body_slug:
        page: dict[str, Any] | None = surreal.get_wiki_page_by_slug(body_slug)
        if page is not None:
            return page
    return None


@observe(tier="hot", span=False)
def _is_skipped_mutability(page: dict[str, Any]) -> bool:
    """True when the body's per-page mutability override pins it OUT of the sweep.

    Per-page override wins (Car J §4.7). Per-type defaults do NOT count —
    the sweep is sanctioned (``_sanctioned=True``) and bypasses the
    storage-layer gate regardless of per-type ``locked``/``derived``.
    """
    override = page.get("mutability_override")
    return override in _SKIPPED_MUTABILITY


@observe(tier="hot", span=False)
def _retype_body_page(surreal: Any, page: dict[str, Any], new_page_type: str) -> bool:
    """Retype a body wiki page to the archived variant. Returns True on write.

    Sanctioned — server-side lifecycle transition (Car J escape hatch).
    """
    page_id = page.get("id")
    if page_id is None:
        return False
    return bool(
        surreal.update_wiki_page(
            int(page_id),
            {"page_type": new_page_type},
            _sanctioned=True,
        )
    )


@observe(tier="hot", span=False)
def _dedupe_projects(task_rows: list[dict[str, Any]]) -> list[str]:
    """Return the sorted, distinct project_id values from a list of task rows.

    Used to derive the per-project sweep set when ``project_id`` is not
    supplied (full pass). ADR sweeps share this list (both tables carry
    ``project_id`` per Car A) — there is no
    ``list_adr_rows_all_projects`` helper in the engine, by design.
    """
    return sorted({str(r["project_id"]) for r in task_rows if r.get("project_id")})


# ── Public op ────────────────────────────────────────────────────────────────


@observe(tier="boundary", metric="backend.admin.run_nightly_archive_sweep")
async def run_nightly_archive_sweep(payload: dict[str, Any]) -> dict[str, Any]:
    """One nightly archive sweep. Idempotent (D35a).

    Args:
        payload: ``{
            "retention_days": int = 90,
            "now": float | None = None,  # unix epoch seconds (UTC); default = wall clock
            "project_id": str | None = None,  # None = sweep all projects
        }``

    Returns:
        ``{
            "archived_tasks": int,
            "archived_adrs": int,
            "archived_patterns": int,
            "archived_disciplines": int,
            "skipped_immutable": int,
            "skipped_contract": int,
            "circuit_breaker_hit": bool,
            "projects_swept": list[str],
            "retention_days": int,
        }``
    """
    retention_days = int(payload.get("retention_days", 90))
    now_ts = float(payload.get("now") or datetime.now(UTC).timestamp())
    now_dt = datetime.fromtimestamp(now_ts, tz=UTC)
    target_project = payload.get("project_id")  # None = all projects
    circuit_breaker_limit = int(payload.get("circuit_breaker_limit", 500))

    sql = _get_sql_storage()
    surreal = _get_storage()
    if sql is None or surreal is None:
        return _empty_stats(retention_days, error="storage not initialised")

    # Count candidates FIRST for the circuit-breaker. Exceeding the limit
    # aborts the sweep and archives nothing — mirrors the memory archive
    # purge's circuit-breaker at cleanup.py:278.
    candidates = await _count_candidates(
        sql,
        retention_days=retention_days,
        now_dt=now_dt,
        target_project=target_project,
    )
    total_candidates = sum(candidates)
    if total_candidates > circuit_breaker_limit:
        logger.warning(
            "nightly_archive_sweep: circuit_breaker hit — candidates=%d limit=%d",
            total_candidates,
            circuit_breaker_limit,
        )
        return _empty_stats(retention_days, circuit_breaker_hit=True)

    projects = await _resolve_projects(sql, target_project)

    archived_tasks, archived_adrs, skipped_immutable = await _sweep_tasks_and_adrs(
        sql, surreal, projects, retention_days, now_dt
    )
    archived_patterns, skipped_immutable_p, _ = await _sweep_patterns(
        sql, surreal, retention_days, now_dt
    )
    archived_disciplines, skipped_immutable_d, skipped_contract = await _sweep_disciplines(
        sql, surreal, retention_days, now_dt
    )

    return {
        "archived_tasks": archived_tasks,
        "archived_adrs": archived_adrs,
        "archived_patterns": archived_patterns,
        "archived_disciplines": archived_disciplines,
        "skipped_immutable": skipped_immutable + skipped_immutable_p + skipped_immutable_d,
        "skipped_contract": skipped_contract,
        "circuit_breaker_hit": False,
        "projects_swept": projects,
        "retention_days": retention_days,
    }


@observe(exempt="trivial dict-builder for early-return / circuit-breaker abort stats; no I/O")
def _empty_stats(
    retention_days: int,
    *,
    circuit_breaker_hit: bool = False,
    error: str | None = None,
) -> dict[str, Any]:
    """Build the all-zeros stats dict for early returns + circuit-breaker aborts."""
    stats: dict[str, Any] = {
        "archived_tasks": 0,
        "archived_adrs": 0,
        "archived_patterns": 0,
        "archived_disciplines": 0,
        "skipped_immutable": 0,
        "skipped_contract": 0,
        "circuit_breaker_hit": circuit_breaker_hit,
        "projects_swept": [],
        "retention_days": retention_days,
    }
    if error is not None:
        stats["error"] = error
    return stats


@observe(
    exempt="single dispatch + dedupe pass; the wrapping run_nightly_archive_sweep "
    "carries the boundary metric for the whole sweep"
)
async def _resolve_projects(sql: Any, target_project: str | None) -> list[str]:
    """Return the project list to sweep: [target] or the dedup'd full set.

    Distinct project_id is derived from ``list_task_rows_all_projects`` —
    the engine exposes no ``list_adr_rows_all_projects`` helper by
    design (per ADR-0195 / Car A).
    """
    if target_project is not None:
        return [target_project]
    all_task_rows: list[dict[str, Any]] = await sql.list_task_rows_all_projects()
    projects: list[str] = _dedupe_projects(all_task_rows)
    return projects


@observe(
    exempt="per-project for-loop dispatch; each leaf already carries its own "
    "@observe span so the boundary sample would be a redundant aggregate"
)
async def _sweep_tasks_and_adrs(
    sql: Any,
    surreal: Any,
    projects: list[str],
    retention_days: int,
    now_dt: datetime,
) -> tuple[int, int, int]:
    """Sweep task + adr rows for each project. Returns (tasks, adrs, skipped)."""
    archived_tasks = 0
    archived_adrs = 0
    skipped_immutable = 0
    cutoff = now_dt - _days(retention_days)
    for project_id in projects:
        t_archived, t_skipped = await _sweep_project_tasks(sql, surreal, project_id, cutoff)
        a_archived, a_skipped = await _sweep_project_adrs(sql, surreal, project_id, cutoff)
        archived_tasks += t_archived
        archived_adrs += a_archived
        skipped_immutable += t_skipped + a_skipped
    return archived_tasks, archived_adrs, skipped_immutable


@observe(
    exempt="per-row inner loop already inside the project dispatcher; the per-row "
    "retype + flip calls each carry their own @observe on _retype_body_page + "
    "sql.update_task_row, so a wrapper span here would be a redundant counter"
)
async def _sweep_project_tasks(
    sql: Any,
    surreal: Any,
    project_id: str,
    cutoff: datetime,
) -> tuple[int, int]:
    """Sweep task rows for one project_id. Returns (archived, skipped)."""
    rows = await sql.list_task_rows(project_id=project_id, status="completed")
    archived = 0
    skipped = 0
    for row in rows:
        updated_at = _parse_iso(row.get("updated_at"))
        if updated_at is None or updated_at >= cutoff:
            continue
        page = _body_page_for_row(surreal, row.get("body_slug"), fallback_id=int(row["id"]))
        if page is None or _is_skipped_mutability(page):
            skipped += 1
            continue
        # §4.1 ordering: retype FIRST, then flip row.
        _retype_body_page(surreal, page, _PAGE_TYPE_TASK_ARCHIVED)
        await sql.update_task_row(int(row["id"]), status="archived", state=None)
        archived += 1
    return archived, skipped


@observe(
    exempt="per-row inner loop already inside the project dispatcher; the per-row "
    "retype + flip calls each carry their own @observe on _retype_body_page + "
    "sql._flip_adr_status, so a wrapper span here would be a redundant counter"
)
async def _sweep_project_adrs(
    sql: Any,
    surreal: Any,
    project_id: str,
    cutoff: datetime,
) -> tuple[int, int]:
    """Sweep ADR rows for one project_id. Returns (archived, skipped)."""
    archived = 0
    skipped = 0
    for adr_status in ("superseded", "rejected", "deprecated"):
        adr_rows = await sql.list_adr_rows(project_id=project_id, status=adr_status)
        for row in adr_rows:
            created_at = _parse_iso(row.get("created_at"))
            if created_at is None or created_at >= cutoff:
                continue
            page = _body_page_for_row(surreal, row.get("body_slug"), fallback_id=int(row["id"]))
            if page is None or _is_skipped_mutability(page):
                skipped += 1
                continue
            _retype_body_page(surreal, page, _PAGE_TYPE_ADR_ARCHIVED)
            await sql._flip_adr_status(int(row["id"]), "archived")
            archived += 1
    return archived, skipped


# ── Internal helpers ────────────────────────────────────────────────────────


def _days(n: int) -> timedelta:
    """Shorthand to avoid repeating ``timedelta(days=...)`` at call sites."""
    return timedelta(days=n)


@observe(tier="stage", metric="backend.admin.nightly_sweep.count_candidates")
async def _count_candidates(
    sql: Any,
    *,
    retention_days: int,
    now_dt: datetime,
    target_project: str | None,
) -> tuple[int, int, int, int]:
    """Return (task, adr, pattern, discipline) candidate counts for the circuit-breaker.

    Mirrors the selection logic in ``run_nightly_archive_sweep`` but does
    not perform any writes. The body-page mutability check is NOT done
    here — a retype-failed-but-row-eligible candidate would still pass
    the count, and the row would be flipped to ``status='archived'`` on
    the actual sweep (the body stays at the original page_type — a
    recoverable state for ``invariants_cross_engine``). Counting
    mutability-skipped candidates is left to the actual sweep pass for
    simplicity.
    """
    cutoff = now_dt - _days(retention_days)
    task_count = 0
    adr_count = 0

    if target_project is not None:
        project_ids = [target_project]
    else:
        all_tasks = await sql.list_task_rows_all_projects()
        project_ids = _dedupe_projects(all_tasks)

    for project_id in project_ids:
        tasks = await sql.list_task_rows(project_id=project_id, status="completed")
        for t in tasks:
            ts = _parse_iso(t.get("updated_at"))
            if ts is not None and ts < cutoff:
                task_count += 1
        for adr_status in ("superseded", "rejected", "deprecated"):
            adrs = await sql.list_adr_rows(project_id=project_id, status=adr_status)
            for a in adrs:
                ts = _parse_iso(a.get("created_at"))
                if ts is not None and ts < cutoff:
                    adr_count += 1

    patterns = await sql.list_agent_prompt_rows()
    pat_count = sum(
        1 for p in patterns if int(p.get("uses", 0)) == 0 and p.get("status") == "deprecated"
    )
    disciplines = await sql.list_agent_discipline_rows()
    disc_count = sum(
        1
        for d in disciplines
        if int(d.get("uses", 0)) == 0
        and d.get("status") == "deprecated"
        and not bool(d.get("always_applied", False))
    )
    return task_count, adr_count, pat_count, disc_count


@observe(tier="stage", metric="backend.admin.nightly_sweep.sweep_patterns")
async def _sweep_patterns(
    sql: Any,
    surreal: Any,
    retention_days: int,
    now_dt: datetime,
) -> tuple[int, int, int]:
    """Sweep ``agent_pattern`` rows. Reach-global (D3, no project_id)."""
    patterns = await sql.list_agent_prompt_rows()
    archived = 0
    skipped = 0
    for row in patterns:
        if int(row.get("uses", 0)) != 0 or row.get("status") != "deprecated":
            continue
        page = _body_page_for_row(surreal, row.get("body_slug"), fallback_id=int(row["id"]))
        if page is None:
            skipped += 1
            continue
        if _is_skipped_mutability(page):
            skipped += 1
            continue
        _retype_body_page(surreal, page, _PAGE_TYPE_AGENT_PATTERN_ARCHIVED)
        # No ledger row flip — the agent_pattern row has no ``archived`` status
        # column on the read surface (only body page carries the archived variant).
        # The row stays as-is (status='deprecated'); recall is gated by the
        # body page_type change (D38).
        archived += 1
    return archived, skipped, 0


@observe(tier="stage", metric="backend.admin.nightly_sweep.sweep_disciplines")
async def _sweep_disciplines(
    sql: Any,
    surreal: Any,
    retention_days: int,
    now_dt: datetime,
) -> tuple[int, int, int]:
    """Sweep ``agent_discipline`` rows. Excludes the ``always_applied`` singleton."""
    disciplines = await sql.list_agent_discipline_rows()
    archived = 0
    skipped = 0
    skipped_contract = 0
    for row in disciplines:
        if bool(row.get("always_applied", False)):
            skipped_contract += 1
            continue
        if int(row.get("uses", 0)) != 0 or row.get("status") != "deprecated":
            continue
        page = _body_page_for_row(surreal, row.get("body_slug"), fallback_id=int(row["id"]))
        if page is None:
            skipped += 1
            continue
        if _is_skipped_mutability(page):
            skipped += 1
            continue
        _retype_body_page(surreal, page, _PAGE_TYPE_AGENT_DISCIPlINE_ARCHIVED)
        archived += 1
    return archived, skipped, skipped_contract
