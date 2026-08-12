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
column projection lists, two status literals, and a timestamp helper. No
class, no statement, nothing for the chokepoint to scan — and ``mariadb.py``
gets its headroom back.

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

#: ``task`` reader projection — ``list_task_rows``,
#: ``list_task_rows_all_projects``, ``get_task_row``.
TASK_COLUMNS = (
    "id, project_id, title, status, state, active_form, "
    "plan_path, body_slug, completed_at, created_at, updated_at"
)

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


def now_utc() -> datetime.datetime:
    """Naive UTC ``datetime`` for the DATETIME retirement-clock columns.

    ``task.completed_at`` / ``adr.superseded_at`` are plain ``DATETIME``,
    matching ``created_at`` / ``updated_at``, whose MySQL-side
    ``CURRENT_TIMESTAMP`` defaults are also naive. Converting to UTC and THEN
    dropping the tzinfo keeps the stored instant correct while handing the
    driver the tz-less object the column expects.
    """
    return datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
