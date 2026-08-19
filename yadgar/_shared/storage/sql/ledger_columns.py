"""Engine-#2 ledger projection lists + the two retirement-clock helpers.

WHY THIS IS A SEPARATE MODULE, AND WHY IT IS NOT A MIXIN
--------------------------------------------------------
``mariadb.py`` sits against I13's HARD 1000-LOC file cap (989 before C15a),
and a HARD cap is not a baseline candidate — the rule is to refactor. The
obvious refactor, lifting the ledger CRUD into a ``_LedgerMixin``, is
explicitly forbidden twice over:

  * ``scripts/check_ledger_chokepoint.py`` sanctions raw ledger SQL by CLASS
    NAME — "inside a method of a class literally named ``MariaStorageEngine``"
    — so ledger SQL in any other class fails D20's guard, and
  * that guard's own docstring records why: PR #32's ``_LedgerMixin`` landed
    behind ``_RuntimeConfigMixin`` in the MRO and was dead code with green
    tests.

So the SQL stays where it is and only the things that are NOT SQL move here:
column projection lists, status literals, a timestamp helper, and (Car B1) one
WHERE-clause builder. No class, no STATEMENT, nothing for the chokepoint to
scan — ``adr_tier_where`` returns a bare ``AND (...)`` fragment, never a
``SELECT``, and the statement it is appended to stays inside
``MariaStorageEngine`` where D20 requires it. ``mariadb.py`` gets its headroom
back.

THE PROJECTION LISTS ARE ONE DEFINITION EACH, ON PURPOSE
--------------------------------------------------------
Each list was previously written out verbatim in every reader of its table
(three for ``task``, two for ``adr``, three for ``agent_pattern``). That is
not a style question. C15a added ``task.completed_at`` / ``adr.superseded_at``
and the nightly archive sweep FILTERS on them, so a reader that missed the
edit would return rows without the column, ``row.get(...)`` would be ``None``,
and the filter would silently degrade to "archive nothing" — the same defect
as SurrealDB revision 031's idempotency filter, which was dead code precisely
because it never projected the column it filtered on. One constant per table
makes that class of miss impossible rather than merely unlikely.

NOT used by ``list_superseded_adr_rows``: that reader projects three columns
deliberately and its docstring forbids sharing symbols with the loader it
exists to independently cross-check (Car C8 / ADR-0195).
"""

from __future__ import annotations

import datetime

from yadgar._shared.observability.observe import observe

#: ``task`` reader projection — ``list_task_rows``,
#: ``list_task_rows_all_projects``, ``get_task_row``.
#:
#: DO NOT NARROW THIS. The nightly archive sweep reads ``body_slug``,
#: ``completed_at`` and ``project_id`` off rows it gets from ``list_task_rows``
#: / ``list_task_rows_all_projects``, and its failure mode is "archives
#: nothing, reports success" — see the module docstring above, which exists
#: because a reader missing a filtered column degrades SILENTLY. The lean
#: shape is a SEPARATE constant, opted into per call.
TASK_COLUMNS = (
    "id, project_id, title, status, state, active_form, "
    "plan_path, body_slug, completed_at, created_at, updated_at"
)

#: The LEAN ``task`` list projection — what a caller listing tasks actually
#: reads (``list_task_rows`` / ``list_task_rows_all_projects``, opt-in via
#: ``summary=True``; the ``task_list`` MCP tool's default, ``verbose=False``).
#:
#: WHY IT EXISTS: measured 2026-08-16 on the live corpus — 79 open rows
#: rendered 24,889 chars through ``TASK_COLUMNS``, 315 chars/row, against ~90
#: for these three columns. ``project_id`` is constant across a project-scoped
#: result and ``state`` / ``active_form`` / ``plan_path`` / ``body_slug`` / the
#: three timestamps are read by NO consumer of a list result. The single-row
#: read (``get_task_row``) is deliberately NOT affected: it stays full.
TASK_COLUMNS_SUMMARY = "id, title, status"

#: ``adr`` reader projection — ``list_adr_rows``, ``get_adr_row``.
ADR_COLUMNS = (
    "id, project_id, title, status, decided_on, subsystem, "
    "tier, body_slug, superseded_at, created_at, updated_at"
)

#: ``agent_pattern`` reader projection — ``list_agent_prompt_rows``,
#: ``get_agent_prompt_row``, and the composes-side read.
AGENT_PATTERN_COLUMNS = (
    "id, name, body_slug, purpose, status, baseline_hash, "
    "content_hash, uses, created_at, updated_at"
)

#: ``agent_discipline`` reader projection — ``list_agent_discipline_rows``.
AGENT_DISCIPLINE_COLUMNS = (
    "id, name, body_slug, purpose, always_applied, position, "
    "status, baseline_hash, content_hash, created_at, updated_at"
)

# ── the two retirement clocks (C15a) ────────────────────────────────────────
#
# The nightly archive sweep retires a row 90 days after it STOPS being live,
# and until C15a neither table carried the instant at which that happened. Car
# K therefore aged tasks off ``updated_at`` and ADRs off ``created_at``, and
# both measure the wrong interval:
#
#   * ``updated_at`` bumps on every edit, so touching a completed task reset
#     its 90-day clock — the regression the parent plan's §14.2 named.
#   * ``created_at`` measures age-since-authoring, so an ADR written 120 days
#     ago and superseded today swept on the very NEXT nightly run, with no
#     grace period at all.
#
# WHERE EACH IS STAMPED, and the two rules that are easy to get backwards:
#
#   * ``task.completed_at`` — ``create_task_row`` (a row born completed) and
#     ``update_task_row`` (a transition INTO ``completed``). An explicit
#     ``completed_at=`` from the caller always wins, so a backfill or import
#     can assert the real instant. Leaving ``completed`` for another status
#     does NOT clear the stamp: reopening is a new life and the next
#     completion re-stamps it.
#   * ``adr.superseded_at`` — ``_flip_adr_status``, and ONLY on the flip to
#     ``'superseded'``. That same method later flips the row to
#     ``'archived'``; re-stamping there would push the clock forward at the
#     exact moment it stops mattering.
#
# Both columns are NULLable and the sweep reads NULL as "this clock never
# started", never as "infinitely old" — a row that has not completed cannot
# have run out of retention. ``superseded_at`` stays NULL for the ``rejected``
# / ``deprecated`` rows the sweep also collects (nothing supersedes them);
# those fall back to ``created_at``, which is why adding the column changed
# their behaviour not at all.
#
# The statuses are named constants rather than inlined at the write sites so
# the stamp and the sweep's own selection filter cannot drift apart into
# "stamped but never selected".
STATUS_COMPLETED = "completed"
STATUS_SUPERSEDED = "superseded"

# ── D27 tier classification (ledger task 191) ───────────────────────────────
#
# ``adr.tier`` is NULLable and a large cohort of rows carries NULL: every row
# written before Car A6 landed the write-side default, plus every row
# ``seed_adr_rows`` inserts (it never stamps the column). ``list_adr_rows``
# translates a ``tier`` filter into ``tier = :tier``, and SQL's three-valued
# logic makes NULL match NEITHER ``'binding'`` NOR ``'historical'`` — so those
# rows are absent from the DEFAULT ``adr_list`` call, which is the one every
# consumer makes. Measured on the live corpus 2026-08-19: 237 rows unfiltered,
# 233 under ``tier='binding'``, 0 under ``tier='historical'``.
#
# A NULL-tier row is therefore classified by its STATUS, using the SAME D27
# mapping the write side applies (``core.server.tools.adr._tier_for_status``,
# Car A6). Deliberately NOT a blanket "NULL means binding": that would put
# superseded / rejected / deprecated rows into the default list, which is
# exactly what D27 excludes them from. Status-derivation instead makes every
# NULL row reachable through exactly ONE filter value, and it agrees with what
# ``seed_adr_tier_subsystem`` would stamp — so running that backfill later
# changes nothing observable.
#
# ``adr_tier_for_flip`` BELOW IS A FOURTH CLASSIFIER AND IS DELIBERATELY NOT
# THE SAME SHAPE — do not "unify" them. The three copies named below are
# TWO-way (anything unrecognised → binding), which is right for a CREATE. The
# flip classifier is THREE-way: it declines to classify a status D27 does not
# name, because ``_flip_adr_status``'s other caller writes ``'archived'`` over
# rows that are already historical and a two-way rule would un-tier the whole
# archived cohort on a cron (ledger task 197).
#
# THIRD COPY, ON PURPOSE. ``core.server.tools.adr._HISTORICAL_STATUSES`` and
# ``backend.admin_exec.seed_adr_tier_subsystem`` already carry the same
# frozenset, and the former's comment records why they are duplicated rather
# than shared: ``core`` does not import ``backend``, and now neither imports
# ``_shared.storage.sql``. D27 in
# ``docs/plans/task-table-refactor-2026-07-29.md:295`` is the source of truth
# for all three.
TIER_BINDING = "binding"
TIER_HISTORICAL = "historical"

#: Statuses whose ADRs are ``historical`` rather than ``binding`` (D27).
HISTORICAL_STATUSES: tuple[str, ...] = ("superseded", "rejected", "deprecated")

#: Statuses whose ADRs are ``binding`` (D27's other half). Enumerated rather
#: than left as "everything not historical" — see ``adr_tier_for_flip``, whose
#: whole point is that the complement is NOT a licence to write ``binding``.
BINDING_STATUSES: tuple[str, ...] = ("open", "accepted")


@observe(exempt="trivial status→tier mapping; no I/O, no error branch worth spanning")
def adr_tier_for_flip(status: str | None) -> str | None:
    """D27 tier for a status CHANGE, or ``None`` for "do not write the column".

    Ledger task 197 (write side): ``_flip_adr_status`` flipped ``status`` and
    left ``tier`` at whatever the row was CREATED with, so every supersede
    wrote a self-contradicting row. Measured on the live corpus 2026-08-19:
    ``adr_list(tier="historical")`` returned ZERO rows for ``m-agahi/yadgar``
    against 20 historical-status rows, all stored ``tier='binding'``.
    ``adr_tier_where`` above rescues the NULL-tier cohort by classifying on
    status; it deliberately does not override an explicit stored value,
    because a stored tier that disagrees with its status is a write bug — this
    one.

    THREE-WAY, AND THAT IS THE WHOLE POINT. The create-side twin
    (``core.server.tools.adr._tier_for_status``) is two-way: anything
    unrecognised → ``binding``. That default is right for a CREATE and
    catastrophic for a FLIP, because ``nightly_sweep._sweep_project_adrs``
    flips retired rows to ``'archived'`` — a status D27 does not classify —
    and those rows are ALREADY superseded / rejected / deprecated. A two-way
    rule would re-tier the entire archived cohort back to ``binding`` on a
    cron, silently, every night. So an unclassified status returns ``None``
    and the caller omits the column from its SET clause entirely, leaving
    Car B1's status-classifying read arm in charge of it.

    Args:
        status: the status being written, or ``None``.

    Returns:
        ``TIER_HISTORICAL`` / ``TIER_BINDING`` for a D27-classified status;
        ``None`` for anything else.
    """
    if status in HISTORICAL_STATUSES:
        return TIER_HISTORICAL
    if status in BINDING_STATUSES:
        return TIER_BINDING
    return None


@observe(
    exempt="pure clause builder; no I/O — returns a WHERE fragment + bind params, and the statement it is appended to is already spanned by list_adr_rows (same shape as _paging_tail)"
)
def adr_tier_where(tier: str) -> tuple[str, dict[str, str]]:
    """SQL fragment + bind params matching *tier*, NULL rows classified by status.

    Returns ``(" AND (...)", params)``. For the two D27 values the fragment
    carries a second arm covering the NULL-tier rows whose ``status`` puts them
    in that tier; for anything else it is a plain equality, so a typo returns
    nothing rather than silently sweeping up every NULL-tier row.

    The status list is enumerated into individual bind params rather than an
    expanding ``bindparam`` because the set is a module constant of fixed size —
    the rendered SQL is then literal, dialect-neutral text.

    NOT called for ``tier=None``: that means "no filter", and the caller omits
    the clause entirely (unchanged behaviour).
    """
    if tier not in (TIER_BINDING, TIER_HISTORICAL):
        return " AND tier = :tier", {"tier": tier}
    names = [f"tier_status_{i}" for i in range(len(HISTORICAL_STATUSES))]
    placeholders = ", ".join(f":{n}" for n in names)
    params = {"tier": tier, **dict(zip(names, HISTORICAL_STATUSES, strict=True))}
    null_arm = "NOT IN" if tier == TIER_BINDING else "IN"
    return (
        f" AND (tier = :tier OR (tier IS NULL AND status {null_arm} ({placeholders})))",
        params,
    )


def now_utc() -> datetime.datetime:
    """Naive UTC ``datetime`` for the DATETIME retirement-clock columns.

    ``task.completed_at`` / ``adr.superseded_at`` are plain ``DATETIME``,
    matching ``created_at`` / ``updated_at``, whose MySQL-side
    ``CURRENT_TIMESTAMP`` defaults are also naive. Converting to UTC and THEN
    dropping the tzinfo keeps the stored instant correct while handing the
    driver the tz-less object the column expects.
    """
    return datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
