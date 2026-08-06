"""The CROSS-ENGINE arm of ``check_invariants`` (engine-#2 car H, ADR-0195).

ADR-0195's consequences name four operational paths that a second engine makes
data-loss-adjacent — backup consistency, the restore verification gate,
migrations, and ``check_invariants`` — and require an engine-#2 arm in each. This
module is the fourth. ``invariants.py`` keeps every SurrealDB-only check exactly
as it was; this file holds only what spans BOTH engines, and the op body stitches
the two together.

WHY THE ARM MUST NEVER SILENTLY PASS
------------------------------------
Two vacuous passes shaped this design and neither was noticed by a test:

* 2026-06-16 — a partial restore (1,484 of 3,622 memories) satisfied a ``>=``
  check and 3,622 memories were destroyed. A comparison that can only fail in one
  direction is not a check.
* This repo's own type ratchet reported clean for its entire life by inferring
  success from an ABSENCE of errors, never from evidence that mypy ran.

So the arm is tri-state — ``ok`` / ``violation`` / ``unavailable`` — and the
third is a first-class outcome, not a synonym for the first. Every check carries
``detail`` with what it actually compared, so ``ok`` is a claim backed by
numbers rather than by silence. A check that fails to report AT ALL is a
``violation``, because absence is exactly the failure mode above.

WHY THIS OP IS ASYNC
--------------------
``asyncmy`` is async-only. Reaching engine #2 from a sync op body would need
``asyncio.run`` inside a worker thread — a private event loop whose
``AsyncAdaptedQueuePool`` would cache connections bound to a loop that dies with
the thread. Car C kept construction connectionless for that reason and car B
widened ``run_admin_op_async`` to admit coroutine bodies precisely so this arm
could exist. ``check_invariants`` is therefore the FIRST async admin op; the
SurrealDB half still runs in ``asyncio.to_thread``, so nothing that used to be
off the loop moved onto it.

WHY UNAVAILABLE DOES NOT FLIP TOP-LEVEL ``ok``
----------------------------------------------
Engine #2 is optional today: ``_init_sql_storage`` degrades to None on a missing
option file or a missing ``sql`` extra, and a core-only install has no MariaDB at
all. If absence turned ``check_invariants`` red, it would be red everywhere and
someone would special-case the arm away — trading a loud signal for no signal.
Loudness instead comes from ``cross_engine`` being present UNCONDITIONALLY with
its own aggregate status, plus a WARNING log. A ``violation``, by contrast, is a
real disagreement and does flip ``ok``.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from yadgar._shared.observability.observe import observe

logger = logging.getLogger(__name__)

# ── outcomes ─────────────────────────────────────────────────────────────────

STATUS_OK = "ok"
STATUS_VIOLATION = "violation"
STATUS_UNAVAILABLE = "unavailable"

# Reasons are NEVER collapsed into one another. "not installed", "installed but
# absent" and "present but the read failed" are the three states an operator
# must be able to tell apart; a single "unavailable" string cannot.
REASON_SQL_EXTRA_ABSENT = "sql_extra_absent"
REASON_ENGINE_TWO_ABSENT = "engine_two_absent"
REASON_STORAGE_ABSENT = "storage_absent"
REASON_QUERY_FAILED = "query_failed"
REASON_CONFIG_TABLE_ABSENT = "config_table_absent"
REASON_SPINE_NOT_SHIPPED = "spine_not_shipped"

# ── check names ──────────────────────────────────────────────────────────────

CHECK_ALEMBIC_CHAIN_SHAPE = "alembic_chain_shape"
CHECK_ENGINE_TWO_SCHEMA_HEAD = "engine_two_schema_head"
CHECK_SURREAL_SCHEMA_HEAD = "surreal_schema_head"
CHECK_CONFIG_ROW_BASELINE = "config_row_baseline"
CHECK_PAGE_ROW_DESYNC = "page_row_desync"

# The arm's own contract with itself. ``run_cross_engine_checks`` compares the
# names it actually produced against this set and raises a VIOLATION on any gap,
# so deleting a check from the registry without deleting it here cannot quietly
# shrink the report. This is the structural half of "positive evidence".
REQUIRED_CHECKS = frozenset(
    {
        CHECK_ALEMBIC_CHAIN_SHAPE,
        CHECK_ENGINE_TWO_SCHEMA_HEAD,
        CHECK_SURREAL_SCHEMA_HEAD,
        CHECK_CONFIG_ROW_BASELINE,
        CHECK_PAGE_ROW_DESYNC,
    }
)

CONFIG_TABLE = "config"

# DECLARED BASELINE, not a snapshot. ADR-0203 makes "config ships empty" a
# load-bearing property of THIS train: task 0095's free-re-key window stays open
# until the first ``config_set``, so an unexpected row means something wrote to
# engine #2 that no code in this tree does.
#
# It is a constant rather than a hard `== 0` forever because the knob train
# legitimately seeds this table. THE CONTRACT IS: whichever commit seeds rows
# MUST move this number in the same commit. That keeps the assertion a CHECK
# rather than a snapshot, and — the point — it is exact in BOTH directions. A
# `>=` would pass a seed that half-landed, which is the precise shape of the
# 2026-06-16 restore that destroyed 3,622 memories.
EXPECTED_CONFIG_ROWS = 0

# ADR-0198's ten-table spine, restricted to the tables that own a BODY PAGE and
# therefore participate in ADR-0209's mirrored ``content_hash``. Probed, never
# created — the spine train (task 0047) owns them.
SPINE_LEDGER_TABLES = ("adr", "agent_discipline", "agent_pattern")


# ── seams (patched by the unit tests; see the module docstring) ──────────────


def _migrate_module() -> Any:
    """The alembic runner. Raises ImportError when the ``sql`` extra is absent.

    A FUNCTION, not a module-scope import: ``sqlalchemy``/``alembic`` live in the
    ``sql`` extra and ``Dockerfile.ci:116`` bakes only ``--extra test --extra
    ml``, so a hard import would break every CI test until yadgar-ci is rebuilt.
    Being a function also gives the tests one seam to raise ImportError from.
    """
    from yadgar._shared.storage.sql import migrate  # noqa: PLC0415 — `sql` extra

    return migrate


def _get_sql_engine() -> Any:
    """The composed ``MariaStorageEngine``, or None when engine #2 is absent."""
    from yadgar._shared.runtime.lifecycle import _get_sql_storage  # noqa: PLC0415

    return _get_sql_storage()


# ── assertion 1a: the alembic chain's own shape (needs NO database) ──────────


@observe(tier="stage", metric="backend.invariants.cross_engine.alembic_chain_shape")
def check_alembic_chain_shape() -> dict:
    """Exactly one alembic head.

    Split out from the stamped-revision check deliberately. This half needs no
    database, so engine-#2 absence must not blind it — a forked chain is a BUILD
    error that exists in the source tree whether or not MariaDB is running. It
    also guards the next check: ``heads()[0]`` on a forked chain silently picks
    an arbitrary head and would compare the database against the wrong one.
    """
    try:
        found = list(_migrate_module().heads())
    except ImportError as exc:
        return {
            "status": STATUS_UNAVAILABLE,
            "reason": REASON_SQL_EXTRA_ABSENT,
            "detail": {"error": str(exc)},
        }
    except Exception as exc:  # noqa: BLE001 — a broken chain must not kill the arm
        return {
            "status": STATUS_UNAVAILABLE,
            "reason": REASON_QUERY_FAILED,
            "detail": {"error": str(exc)},
        }

    if len(found) != 1:
        return {
            "status": STATUS_VIOLATION,
            "message": (
                f"engine #2 alembic chain has {len(found)} heads ({found}) — "
                "exactly one is required; the chain forked"
            ),
            "detail": {"heads": found},
        }
    return {"status": STATUS_OK, "detail": {"head": found[0], "heads": 1}}


# ── assertion 1b: engine #2 is stamped at that head ──────────────────────────


@observe(tier="stage", metric="backend.invariants.cross_engine.engine_two_schema_head")
async def check_engine_two_schema_head(engine: Any, chain: dict) -> dict:
    """The ``alembic_version`` stamp equals the chain head.

    Alembic's version table lives in MariaDB and SurrealDB's 28 hand-rolled
    migrations are an INDEPENDENT chain (``_shared/storage/migrations.py``, whose
    ``.migration.lock`` is a per-container flock that cannot serialise across
    processes — task 0115). The two need not agree with EACH OTHER; each must be
    at its own head. A mismatch surfaces here rather than at the first write.
    """
    if chain["status"] != STATUS_OK:
        # No trustworthy head to compare against. Inherit the reason rather than
        # inventing one, so the operator sees the ROOT cause.
        return {
            "status": STATUS_UNAVAILABLE,
            "reason": chain.get("reason", REASON_QUERY_FAILED),
            "detail": {"message": "alembic chain shape is not ok — no head to compare against"},
        }
    if engine is None:
        return {
            "status": STATUS_UNAVAILABLE,
            "reason": REASON_ENGINE_TWO_ABSENT,
            "detail": {"message": "engine #2 is not composed — nothing to read a stamp from"},
        }

    head = chain["detail"]["head"]
    try:
        current = await _migrate_module().current_revision(engine.engine)
    except Exception as exc:  # noqa: BLE001 — a read failure is UNAVAILABLE, not ok
        return {
            "status": STATUS_UNAVAILABLE,
            "reason": REASON_QUERY_FAILED,
            "detail": {"error": str(exc), "head": head},
        }

    if current is None:
        return {
            "status": STATUS_VIOLATION,
            "message": (
                "engine #2 has NEVER been migrated (no alembic_version stamp) "
                f"while the chain head is {head}"
            ),
            "detail": {"current": None, "head": head},
        }
    if current != head:
        return {
            "status": STATUS_VIOLATION,
            "message": f"engine #2 is stamped at {current} but the chain head is {head}",
            "detail": {"current": current, "head": head},
        }
    return {"status": STATUS_OK, "detail": {"current": current, "head": head}}


# ── assertion 1c: SurrealDB's own hand-rolled chain is at head ───────────────


@observe(tier="stage", metric="backend.invariants.cross_engine.surreal_schema_head")
def check_surreal_schema_head(storage: Any) -> dict:
    """``schema_version`` holds exactly the versions ``_MIGRATIONS`` declares.

    Both directions are violations and they are reported separately:

    * MISSING (in code, not in the DB) — a pending migration. The daemon is
      running against a schema older than its own code.
    * UNKNOWN (in the DB, not in code) — the DB was migrated by a NEWER build and
      the daemon was then rolled back. Verified not to false-red on the live
      corpus: ``schema_version`` there holds 26 rows, all present in
      ``_MIGRATIONS``, and ``017`` — the one reserved id — has never shipped in
      any commit of ``migrations.py``, so no live row can carry it.
    """
    if storage is None:
        return {
            "status": STATUS_UNAVAILABLE,
            "reason": REASON_STORAGE_ABSENT,
            "detail": {"message": "SurrealDB storage is not composed"},
        }
    try:
        from yadgar._shared.storage.migrations import _MIGRATIONS  # noqa: PLC0415

        expected = [str(m["version"]) for m in _MIGRATIONS]
        # Plain ``_q``: this runs inside ``asyncio.to_thread`` and the op already
        # carries a 120 s floor (``core/forward.py:47``), so a per-table timeout
        # would only duplicate a bound that already exists one layer up.
        rows = storage._q("SELECT version FROM schema_version")
    except Exception as exc:  # noqa: BLE001 — a read failure is UNAVAILABLE, not ok
        return {
            "status": STATUS_UNAVAILABLE,
            "reason": REASON_QUERY_FAILED,
            "detail": {"error": str(exc)},
        }

    applied = {str(r.get("version")) for r in rows if r.get("version")}
    missing = [v for v in expected if v not in applied]
    unknown = sorted(applied - set(expected))

    if missing or unknown:
        parts = []
        if missing:
            parts.append(f"{len(missing)} migration(s) pending: {missing}")
        if unknown:
            parts.append(f"{len(unknown)} version(s) unknown to this build: {unknown}")
        return {
            "status": STATUS_VIOLATION,
            "message": "SurrealDB schema is not at head — " + "; ".join(parts),
            "detail": {
                "missing": missing,
                "unknown": unknown,
                "applied": len(applied),
                "expected": len(expected),
            },
        }
    return {
        "status": STATUS_OK,
        "detail": {"applied": len(applied), "expected": len(expected), "head": expected[-1]},
    }


# ── assertion 2: the config-row baseline ─────────────────────────────────────


@observe(tier="stage", metric="backend.invariants.cross_engine.config_row_baseline")
async def check_config_row_baseline(engine: Any, engine_head: dict) -> dict:
    """``config`` holds EXACTLY ``EXPECTED_CONFIG_ROWS`` rows — see that constant.

    The table-absent case is split on evidence rather than guessed. Alembic head
    CREATES ``config`` (revision ``0001_config``), so:

    * stamped at head AND no table → a CONTRADICTION between two checks that must
      agree. That is a violation, and a free one — the evidence is already in
      hand from the previous check.
    * not stamped at head AND no table → the schema genuinely has not been built
      yet. Honestly unavailable; the head check already carries the violation.
    """
    if engine is None:
        return {
            "status": STATUS_UNAVAILABLE,
            "reason": REASON_ENGINE_TWO_ABSENT,
            "detail": {"message": "engine #2 is not composed — no config table to count"},
        }
    try:
        tables = await engine.list_tables()
    except Exception as exc:  # noqa: BLE001
        return {
            "status": STATUS_UNAVAILABLE,
            "reason": REASON_QUERY_FAILED,
            "detail": {"error": str(exc)},
        }

    if CONFIG_TABLE not in tables:
        if engine_head.get("status") == STATUS_OK:
            return {
                "status": STATUS_VIOLATION,
                "message": (
                    f"engine #2 is stamped at head ({engine_head['detail']['head']}) but the "
                    f"{CONFIG_TABLE!r} table that head creates does not exist"
                ),
                "detail": {
                    "message": "stamped at head but the table head creates is missing",
                    "tables": tables,
                },
            }
        return {
            "status": STATUS_UNAVAILABLE,
            "reason": REASON_CONFIG_TABLE_ABSENT,
            "detail": {"tables": tables},
        }

    try:
        rows = await engine.count_rows(CONFIG_TABLE)
    except Exception as exc:  # noqa: BLE001
        return {
            "status": STATUS_UNAVAILABLE,
            "reason": REASON_QUERY_FAILED,
            "detail": {"error": str(exc)},
        }

    if rows != EXPECTED_CONFIG_ROWS:
        direction = "above" if rows > EXPECTED_CONFIG_ROWS else "BELOW"
        return {
            "status": STATUS_VIOLATION,
            "message": (
                f"engine #2 {CONFIG_TABLE} holds {rows} row(s), {direction} the declared "
                f"baseline of {EXPECTED_CONFIG_ROWS} — either something wrote rows no code "
                "in this tree writes, or a seed did not fully land"
            ),
            "detail": {"rows": rows, "expected": EXPECTED_CONFIG_ROWS},
        }
    return {"status": STATUS_OK, "detail": {"rows": rows, "expected": EXPECTED_CONFIG_ROWS}}


# ── assertion 3: cross-engine page/row desync — SHAPE ONLY (spine-gated) ─────


@observe(tier="stage", metric="backend.invariants.cross_engine.page_row_desync")
async def check_page_row_desync(engine: Any) -> dict:
    """ADR-0209's mirrored ``content_hash`` — the SHAPE, not yet the comparison.

    ADR-0209 defines ``content_hash`` written BOTH as wiki-page metadata and as a
    ledger-row column, where disagreement between the two copies IS the signal,
    plus a row-side ``baseline_hash``. NEITHER IS IMPLEMENTED: the ledger tables
    belong to the spine train (task 0047), and this car deliberately does not
    invent them.

    So the check runs, probes, and reports honestly that it cannot assert. What it
    does NOT do is return ok — and it is written as a TRIPWIRE rather than a
    comment: the moment any spine ledger table APPEARS, the precondition for the
    real comparison is satisfied and the stub turns itself RED. Without that, the
    spine train would ship the tables and this arm would keep reporting a
    comfortable "unavailable" over data it should have been comparing, which is
    the vacuous pass one layer up.

    Also spine-gated and covered by this same shape, per ADR-0198's consequences:
    ``adr.status='superseded'`` must agree with the ``adr_supersedes`` join table
    (task 0136). It is named here rather than built, for the same reason.
    """
    if engine is None:
        return {
            "status": STATUS_UNAVAILABLE,
            "reason": REASON_ENGINE_TWO_ABSENT,
            "detail": {"message": "engine #2 is not composed — no ledger rows to compare"},
        }
    try:
        tables = set(await engine.list_tables())
    except Exception as exc:  # noqa: BLE001
        return {
            "status": STATUS_UNAVAILABLE,
            "reason": REASON_QUERY_FAILED,
            "detail": {"error": str(exc)},
        }

    present = sorted(tables & set(SPINE_LEDGER_TABLES))
    if present:
        return {
            "status": STATUS_VIOLATION,
            "message": (
                f"spine ledger table(s) {present} exist, so ADR-0209's page/row "
                "content_hash comparison is now assertable — but it is still STUBBED. "
                "Implement it (engine-#2 car H left the shape; the spine train owns "
                "the hashes) rather than letting this arm pass over real data"
            ),
            "detail": {"present_tables": present},
        }
    return {
        "status": STATUS_UNAVAILABLE,
        "reason": REASON_SPINE_NOT_SHIPPED,
        "detail": {
            "absent_tables": sorted(SPINE_LEDGER_TABLES),
            "message": (
                "ADR-0209's content_hash / baseline_hash are not implemented; the ledger "
                "tables are the spine train's (task 0047). This check is shape-only."
            ),
        },
    }


# ── orchestrator ─────────────────────────────────────────────────────────────


# Substituted for an earlier check's outcome when that check never reported —
# e.g. because it was removed from the registry. A dependent check must then see
# something it will refuse to trust, never a shape it might read as ok.
_NEVER_REPORTED: dict = {"status": STATUS_VIOLATION, "detail": {"missing": True}}

# THE single source of both the arm's membership and its ORDER — two checks
# consume an earlier one's result, so this is a sequence, not a set. Every check
# runs through this table and nothing runs outside it, which is what makes the
# ``REQUIRED_CHECKS`` guard meaningful: delete an entry here without deleting it
# there and the arm reports a violation instead of quietly shrinking.
_CHECK_REGISTRY: tuple[tuple[str, Callable[[dict], dict | Awaitable[dict]]], ...] = (
    (CHECK_ALEMBIC_CHAIN_SHAPE, lambda _ctx: check_alembic_chain_shape()),
    (
        CHECK_ENGINE_TWO_SCHEMA_HEAD,
        lambda ctx: check_engine_two_schema_head(
            ctx["engine"],
            ctx["results"].get(CHECK_ALEMBIC_CHAIN_SHAPE, _NEVER_REPORTED),
        ),
    ),
    # SurrealDB's ``_q`` is blocking; keep it off the event loop exactly as the
    # SurrealDB-only half of the op does.
    (
        CHECK_SURREAL_SCHEMA_HEAD,
        lambda ctx: asyncio.to_thread(check_surreal_schema_head, ctx["storage"]),
    ),
    (
        CHECK_CONFIG_ROW_BASELINE,
        lambda ctx: check_config_row_baseline(
            ctx["engine"],
            ctx["results"].get(CHECK_ENGINE_TWO_SCHEMA_HEAD, _NEVER_REPORTED),
        ),
    ),
    (CHECK_PAGE_ROW_DESYNC, lambda ctx: check_page_row_desync(ctx["engine"])),
)


@observe(tier="boundary", metric="backend.invariants.cross_engine.run")
async def run_cross_engine_checks(storage: Any) -> dict:
    """Run every cross-engine assertion and aggregate them.

    Returns::

        {
          "status": "ok" | "violation" | "unavailable",
          "checks": {<name>: {"status", ["reason"], ["message"], "detail"}},
          "violations": [str, ...],
          "unavailable": [str, ...],
        }

    ``checks`` ALWAYS covers ``REQUIRED_CHECKS`` exactly. The aggregate is the
    worst outcome present, and ``ok`` requires that EVERY check reported ok —
    one unavailable check is enough to withhold it.
    """
    ctx: dict = {"engine": _get_sql_engine(), "storage": storage, "results": {}}

    for name, run in _CHECK_REGISTRY:
        outcome = run(ctx)
        ctx["results"][name] = await outcome if inspect.isawaitable(outcome) else outcome

    return _aggregate(ctx["results"])


@observe(tier="stage", metric="backend.invariants.cross_engine.aggregate")
def _aggregate(produced: dict[str, dict]) -> dict:
    """Fold per-check outcomes into the arm's report, enforcing full coverage.

    A name in ``REQUIRED_CHECKS`` that never reported is recorded as a VIOLATION
    rather than dropped. That is the structural guard: it is the difference
    between "every check passed" and "no check ran", which the type ratchet spent
    its whole life unable to tell apart.
    """
    checks = dict(produced)
    violations: list[str] = []
    unavailable: list[str] = []

    for name in sorted(REQUIRED_CHECKS - set(checks)):
        checks[name] = {
            "status": STATUS_VIOLATION,
            "message": f"cross-engine check {name!r} did not report — it was never run",
            "detail": {"missing": True},
        }

    for name in sorted(checks):
        outcome = checks[name]
        if outcome["status"] == STATUS_VIOLATION:
            msg = outcome.get("message", f"cross-engine check {name!r} failed")
            violations.append(f"cross-engine[{name}]: {msg}")
        elif outcome["status"] == STATUS_UNAVAILABLE:
            unavailable.append(f"{name}({outcome.get('reason', 'unspecified')})")

    if violations:
        status = STATUS_VIOLATION
    elif unavailable:
        status = STATUS_UNAVAILABLE
    else:
        status = STATUS_OK

    for v in violations:
        logger.critical("check_invariants: %s", v)
    if unavailable:
        # Absence does not flip top-level ``ok`` (see the module docstring), so
        # the WARNING is the loudness. Never downgrade this to debug.
        logger.warning(
            "check_invariants: %d cross-engine check(s) COULD NOT RUN: %s",
            len(unavailable),
            ", ".join(unavailable),
        )

    return {
        "status": status,
        "checks": checks,
        "violations": violations,
        "unavailable": unavailable,
    }
