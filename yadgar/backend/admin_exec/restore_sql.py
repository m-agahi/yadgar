"""Engine #2's RESTORE path and its verification gate (car G, ADR-0195/0196).

THIS MODULE *IS* THE RESTORE PATH
---------------------------------
Before this car engine #2 had no restore at all — car F produced artifacts
nothing could replay. So the placement claim here is STRUCTURAL rather than
positional: there is exactly one way a ``mariadb-dump`` artifact gets back into
engine #2, it is ``mariadb_restore_verify``, and the enumeration runs inside it
before the restore can be called good. No caller can reach the replay without
passing the gate, because the replay is not exported as anything a caller can
invoke on its own.

That is deliberately the same discipline the 2026-06-16 guard follows in the
vacuum: ``_capture_table_counts`` is taken BEFORE anything destructive and
``_build_and_verify_side_db`` must return True before ``_atomic_swap`` is even
reached (``core/vacuum/__init__.py``). Verification that only exists as a pytest
against a scratch container is unproven exactly where the incident happened.

WHAT THIS IS NOT: DISASTER RECOVERY
-----------------------------------
Bounding the claim above, because "the restore path" reads wider than it is.
This op answers "does this artifact restore to the same corpus" — it replays
into a THROWAWAY schema, enumerates it, drops it, and writes nothing to the live
one. Bringing engine #2 back FROM a dump into production is not built here and
cannot be reached from here: the app account's grant covers ``<db>`` and
``<db>\\_restorecheck\\_%`` and nothing else, so the scratch-only property is
enforced by the server rather than by this code being careful. Recovery belongs
with ``mariadb-backup`` (physical, full + incremental), which ADR-0212 defers to
the spine train.

ENUMERATION, NOT AGGREGATE COMPARISON
-------------------------------------
On 2026-06-16 a partial restore of 1,484 of 3,622 memories SATISFIED a ``>=``
check and 3,622 memories were destroyed. A count can be satisfied by the wrong
rows, or by a coincidence of deletions and insertions. So the row check compares
per-row IDENTITY: each row is reduced to a SHA2 digest of its columns, and the
two sides are folded into one query whose result is the SYMMETRIC DIFFERENCE —
digests whose multiplicity differs between source and restored. Zero rows out
is the only pass, and a row present on EITHER side alone fails it. Counts are
reported as ``detail`` evidence and are never the check.

Both databases live in the same mariadbd instance, so the comparison is one
server-side query rather than two transfers: the output is bounded by the number
of DISAGREEING rows, not by corpus size.

WHY THE ARTIFACT'S OWN ``USE``/``CREATE DATABASE`` STATEMENTS ARE STRIPPED
--------------------------------------------------------------------------
Car F dumps with ``--databases``, so the artifact opens by CREATE-ing and USE-ing
the LIVE schema. Replaying it verbatim would restore over production — an
incident strictly worse than the one this car exists to prevent. Two independent
belts, because a filter with a hole would be silent:

1. ``filter_dump_statements`` DROPS the source database's own ``USE`` and
   ``CREATE DATABASE`` lines (drop, never rewrite — nothing is injected into the
   stream) and RAISES on any statement in that family naming anything else. The
   restore target is passed to the client positionally instead.
2. ``check_source_untouched`` fingerprints the live schema BEFORE the replay and
   again after, and a difference is a violation. That belt does not care WHY the
   filter leaked; it converts "my filter has a hole" from a silent catastrophe
   into a loud refusal.

FAIL CLOSED — ``unavailable`` IS NOT ``ok``
-------------------------------------------
The report is tri-state on car H's pattern (``invariants_cross_engine.py``), and
its three statuses mean the same things. The COMPOSITION differs and the
difference is the point: car H is a reporting arm where ``unavailable`` must not
turn every core-only install red, whereas this is a GATE. A verification that
cannot run tells you nothing about the artifact, so the op refuses on anything
that is not ``ok``. Reporting honestly and acting conservatively are separate
jobs: ``verify_restore`` does the first, ``mariadb_restore_verify`` the second.

WHY THIS RUNS IN THE BACKEND CONTAINER
--------------------------------------
Car F's reasoning carries over unchanged: ``client.cnf`` holds a
CONTAINER-ABSOLUTE socket path, mariadbd runs ``--skip-networking`` so there is
no TCP fallback, and the ``mariadb`` client ships only with the
``mariadb-server`` apt install baked into ``Dockerfile.backend``. Like car F this
module shells a binary and imports NOTHING from the ``sql`` extra, so the
yadgar-ci ``--extra sql`` rebuild is not a prerequisite for it.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
import uuid
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from yadgar._shared.observability.observe import observe
from yadgar._shared.storage.sql.config import MariaClientConfig, read_client_option_file
from yadgar.backend.admin_exec.backup_sql import _dump_dir
from yadgar.backend.admin_exec.invariants_cross_engine import (
    STATUS_OK,
    STATUS_UNAVAILABLE,
    STATUS_VIOLATION,
)

__all__ = [
    "RestoreVerificationError",
    "check_column_sets",
    "check_row_identity",
    "check_source_untouched",
    "check_table_set",
    "filter_dump_statements",
    "mariadb_restore_verify",
    "verify_restore",
]

logger = logging.getLogger(__name__)

# ── reasons (never collapsed — an operator must tell these apart) ─────────────

REASON_CLIENT_ABSENT = "mariadb_client_absent"
REASON_ARTIFACT_ABSENT = "artifact_absent"
REASON_ARTIFACT_REJECTED = "artifact_rejected"
REASON_QUERY_FAILED = "query_failed"
REASON_REPLAY_FAILED = "replay_failed"
REASON_NO_BASELINE = "no_source_baseline"
REASON_TABLES_UNCOMPARABLE = "tables_not_comparable"

# ── check names ──────────────────────────────────────────────────────────────

CHECK_TABLE_SET = "table_set"
CHECK_COLUMN_SETS = "column_sets"
CHECK_ROW_IDENTITY = "row_identity"
CHECK_SOURCE_UNTOUCHED = "source_untouched"

# The arm's contract with itself, exactly as car H states it: a name that never
# reported is a VIOLATION rather than a gap, so deleting a check cannot quietly
# shrink the report into a pass.
REQUIRED_CHECKS = frozenset(
    {CHECK_TABLE_SET, CHECK_COLUMN_SETS, CHECK_ROW_IDENTITY, CHECK_SOURCE_UNTOUCHED}
)

# Scratch schema names are ``<database>_restorecheck_<hex>``. The infix is
# load-bearing in production: ``entrypoint-backend.sh`` grants the app account
# ``<db>\_restorecheck\_%`` and NOTHING wider, so a bug in the name construction
# cannot reach any other schema — the server refuses it.
SCRATCH_INFIX = "_restorecheck_"

# Divergent row digests carried in the report. The check is the EXISTENCE of any
# divergence; this only bounds how many get named for triage.
MAX_REPORTED_DIVERGENCES = 20

# Wall-clock ceiling on one replay. The window is a full MCP outage (ADR-0210),
# so a wedged client must not hold it open forever.
REPLAY_TIMEOUT_SEC = 900.0

# Ceiling on a single verification query.
QUERY_TIMEOUT_SEC = 120.0

# Separator inside the per-row digest input. Never appears in HEX() output nor in
# the NULL sentinel, so the concatenation is unambiguous — two different rows
# cannot produce the same pre-image by shifting bytes across the boundary.
_DIGEST_SEP = "0x1F"

# HEX() emits ``[0-9A-F]*``, so this literal cannot collide with any encoded
# value. That is what keeps NULL distinguishable from the empty string — the
# distinction CONCAT_WS itself throws away, since it skips NULLs silently.
_NULL_SENTINEL = "'NULL'"


class RestoreVerificationError(RuntimeError):
    """A restore that was not proven good. Carries the full tri-state report.

    Raised for BOTH ``violation`` and ``unavailable`` — see the module docstring
    for why a gate collapses those two into one refusal while car H's reporting
    arm keeps them apart.
    """

    def __init__(self, message: str, report: dict) -> None:
        super().__init__(message)
        self.report = report


# ── identifier quoting ───────────────────────────────────────────────────────


@observe(tier="hot")
def _q(identifier: str) -> str:
    """Backtick-quote a schema/table/column name, doubling any embedded backtick."""
    return "`" + identifier.replace("`", "``") + "`"


@observe(tier="hot")
def _lit(value: str) -> str:
    """Single-quote a string literal for a WHERE clause (identifiers only)."""
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


# ── the SQL seam (faked wholesale by the unit tests) ─────────────────────────


@observe(tier="stage")
def _client_binary() -> str | None:
    """Absolute path to the ``mariadb`` client, or None when it is not installed."""
    import shutil  # noqa: PLC0415 — mirrors backup_sql's lazy resolution

    return shutil.which("mariadb")


@observe(tier="stage")
def _run_sql(cfg: MariaClientConfig, sql: str, database: str | None = None) -> list[list[str]]:
    """Run one statement and return its rows as lists of raw strings.

    ``--batch --skip-column-names`` gives tab-separated output. Every column this
    module selects is a HEX digest, a decimal number or an identifier, none of
    which can contain a tab or a newline — so the batch escaping this would
    otherwise have to undo never fires. ``--raw`` is deliberately NOT passed:
    escaping is what keeps a pathological identifier from splitting one row
    across two, and there is nothing here for it to corrupt.

    ``--defaults-file`` MUST come first; the client rejects it anywhere else in
    argv (the same constraint car F's dump argv is built around), and the
    password therefore never enters this process list.
    """
    binary = _client_binary()
    if binary is None:
        raise RuntimeError("mariadb client not found on PATH")
    argv = [
        binary,
        f"--defaults-file={cfg.option_file}",
        "--batch",
        "--skip-column-names",
        "-e",
        sql,
    ]
    if database:
        argv.append(database)
    completed = subprocess.run(  # noqa: S603 — fixed argv, no shell
        argv, capture_output=True, timeout=QUERY_TIMEOUT_SEC, check=False
    )
    if completed.returncode != 0:
        stderr = completed.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"mariadb exited {completed.returncode}: {stderr[:400]}")
    text = completed.stdout.decode(errors="replace")
    return [line.split("\t") for line in text.splitlines() if line]


# ── artifact filtering ───────────────────────────────────────────────────────


@observe(tier="stage")
def filter_dump_statements(lines: Iterable[str], source_db: str) -> Iterator[str]:
    """Strip the artifact's own schema-selection statements. LAZY by contract.

    A generator, not a list: an artifact is streamed straight into the client's
    stdin so its size never becomes this process's memory.
    ``test_filter_streams_rather_than_materialising`` pins that by pulling one
    item from an ENDLESS source: a list-returning implementation hangs there.

    Drops (never rewrites) ``CREATE DATABASE ... `<source_db>` ...`` and
    ``USE `<source_db>`;``. Anything else in that family RAISES: a redirect this
    module did not author is loud rather than silently passed through to a client
    that would happily obey it.
    """
    wanted = source_db.lower()
    for line in lines:
        stripped = line.strip()
        lowered = stripped.lower()
        if lowered.startswith("use "):
            if _named_db(stripped[len("use ") :]) != wanted:
                raise RuntimeError(
                    f"restore artifact carries {stripped[:120]!r} — a USE naming a database "
                    f"other than {source_db!r}. Refusing: replaying it would point the "
                    "restore at a schema this arm did not create."
                )
            continue
        if lowered.startswith("create database"):
            if _named_db(stripped[len("create database") :]) != wanted:
                raise RuntimeError(
                    f"restore artifact carries {stripped[:120]!r} — a CREATE DATABASE naming "
                    f"a database other than {source_db!r}. Refusing."
                )
            continue
        yield line


@observe(tier="hot")
def _named_db(tail: str) -> str:
    """Lower-cased database name out of the tail of a USE / CREATE DATABASE line.

    Tolerates the optimizer comments mariadb-dump interleaves
    (``/*!32312 IF NOT EXISTS*/``) and both quoted and bare names. An unparseable
    tail deliberately returns something that matches NO database, so the caller
    raises rather than dropping a statement it did not understand.
    """
    text = tail
    while "/*" in text and "*/" in text:
        head, _, rest = text.partition("/*")
        text = head + rest.partition("*/")[2]
    text = text.strip().removeprefix("IF NOT EXISTS").removeprefix("if not exists").strip()
    if text.startswith("`"):
        return text[1:].partition("`")[0].lower()
    token = text.split(";")[0].split()[0] if text.split(";")[0].split() else ""
    return token.strip("\"'").lower()


# ── row digests ──────────────────────────────────────────────────────────────


@observe(tier="stage")
def _row_digest_expr(columns: list[str]) -> str:
    """SQL expression reducing one row to a SHA2-256 hex digest of ALL its columns.

    ``HEX(CAST(col AS BINARY))`` rather than the value itself so that a tab, a
    newline or the separator inside a value cannot shift bytes across a column
    boundary and make two different rows digest the same. ``IFNULL(..., 'NULL')``
    restores the NULL/'' distinction CONCAT_WS discards.
    """
    parts = ", ".join(f"IFNULL(HEX(CAST({_q(c)} AS BINARY)), {_NULL_SENTINEL})" for c in columns)
    return f"SHA2(CONCAT_WS({_DIGEST_SEP}, {parts}), 256)"


@observe(tier="stage")
def _list_tables(cfg: MariaClientConfig, database: str) -> list[str]:
    """Base-table names in *database*, sorted. Views are excluded deliberately."""
    rows = _run_sql(
        cfg,
        "SELECT TABLE_NAME FROM information_schema.tables WHERE TABLE_SCHEMA = "
        f"{_lit(database)} AND TABLE_TYPE = 'BASE TABLE' ORDER BY TABLE_NAME",
    )
    return [r[0] for r in rows if r and r[0]]


@observe(tier="stage")
def _columns(cfg: MariaClientConfig, database: str, table: str) -> list[str]:
    """Column names of *table* in ordinal order."""
    rows = _run_sql(
        cfg,
        "SELECT COLUMN_NAME FROM information_schema.columns WHERE TABLE_SCHEMA = "
        f"{_lit(database)} AND TABLE_NAME = {_lit(table)} ORDER BY ORDINAL_POSITION",
    )
    return [r[0] for r in rows if r and r[0]]


# ── check 1: the table set, both directions ──────────────────────────────────


@observe(tier="stage", metric="backend.restore.check.table_set")
def check_table_set(cfg: Any, source_db: str, restored_db: str) -> dict:
    """The two schemas hold EXACTLY the same base tables.

    Both directions are failures and are reported separately. ``missing`` is a
    table the restore did not create — the coarse shape of 06-16. ``extra`` is a
    table the source does not have, which means the artifact is stale or the
    scratch schema was not clean; a check that only looked for absences would
    pass that.
    """
    try:
        src = _list_tables(cfg, source_db)
        dst = _list_tables(cfg, restored_db)
    except Exception as exc:  # noqa: BLE001 — a read failure is UNAVAILABLE, not ok
        return {
            "status": STATUS_UNAVAILABLE,
            "reason": REASON_QUERY_FAILED,
            "detail": {"error": str(exc)},
        }

    missing = sorted(set(src) - set(dst))
    extra = sorted(set(dst) - set(src))
    if missing or extra:
        return {
            "status": STATUS_VIOLATION,
            "message": (
                f"restored schema does not hold the same tables as the source: "
                f"{len(missing)} missing {missing}, {len(extra)} unexpected {extra}"
            ),
            "detail": {"missing": missing, "extra": extra, "source": src, "restored": dst},
        }
    return {"status": STATUS_OK, "detail": {"tables": src, "count": len(src)}}


# ── check 2: per-table columns ───────────────────────────────────────────────


@observe(tier="stage", metric="backend.restore.check.column_sets")
def check_column_sets(cfg: Any, source_db: str, restored_db: str, tables: list[str]) -> dict:
    """Each shared table has the same columns in the same order.

    A PRECONDITION, not a schema-diff feature. The row digest is built from the
    SOURCE column list, so a restored table missing a column would make the row
    query ERROR rather than report — and an error routed through the generic
    handler reads as ``unavailable`` when the truth is a violation. Comparing the
    lists first turns that into a clean, correctly-typed failure.
    """
    mismatches: dict[str, dict[str, list[str]]] = {}
    try:
        for table in tables:
            src = _columns(cfg, source_db, table)
            dst = _columns(cfg, restored_db, table)
            if src != dst:
                mismatches[table] = {"source": src, "restored": dst}
    except Exception as exc:  # noqa: BLE001
        return {
            "status": STATUS_UNAVAILABLE,
            "reason": REASON_QUERY_FAILED,
            "detail": {"error": str(exc)},
        }

    if mismatches:
        return {
            "status": STATUS_VIOLATION,
            "message": (
                f"{len(mismatches)} restored table(s) have a different column list than the "
                f"source: {sorted(mismatches)}"
            ),
            "detail": {"mismatches": mismatches},
        }
    return {"status": STATUS_OK, "detail": {"tables": len(tables)}}


# ── check 3: per-row identity — THE gate ─────────────────────────────────────


@observe(tier="stage", metric="backend.restore.check.row_identity")
def check_row_identity(cfg: Any, source_db: str, restored_db: str, tables: list[str]) -> dict:
    """Every row of every table appears the SAME number of times on both sides.

    One query per table folds both sides into a multiset of per-row digests and
    returns only the digests whose multiplicity DISAGREES. That is the
    enumeration: a missing row, an extra row and a MUTATED row (identical count,
    different content) are all divergences, and none of the three is visible to a
    count comparison. ``count(restored) >= count(expected)`` is the check that
    passed on 2026-06-16 while 1,484 of 3,622 memories were present.

    ``HAVING sn <> tn`` is the whole gate. Weakening it to ``>`` restores the
    06-16 shape exactly, and the test that catches that is
    ``test_restore_with_extra_rows_is_rejected`` — the direction a one-sided
    comparison cannot see. Verified by actually making the change and watching it
    go red; the missing-rows test stays GREEN under that mutation, which is
    precisely why a suite testing only the 06-16 direction would have shipped it.
    """
    diverged: dict[str, list[dict[str, Any]]] = {}
    counts: dict[str, dict[str, int]] = {}
    try:
        for table in tables:
            columns = _columns(cfg, source_db, table)
            if not columns:
                return {
                    "status": STATUS_UNAVAILABLE,
                    "reason": REASON_TABLES_UNCOMPARABLE,
                    "detail": {"table": table, "message": "no columns to digest"},
                }
            expr = _row_digest_expr(columns)
            src, dst = _q(source_db), _q(restored_db)
            tbl = _q(table)
            rows = _run_sql(
                cfg,
                "SELECT d, SUM(s) AS sn, SUM(t) AS tn FROM ("
                f"SELECT {expr} AS d, 1 AS s, 0 AS t FROM {src}.{tbl}"
                " UNION ALL "
                f"SELECT {expr} AS d, 0 AS s, 1 AS t FROM {dst}.{tbl}"
                ") u GROUP BY d HAVING sn <> tn ORDER BY d "
                f"LIMIT {MAX_REPORTED_DIVERGENCES + 1}",
            )
            if rows:
                diverged[table] = [
                    {"digest": r[0], "source_rows": int(r[1]), "restored_rows": int(r[2])}
                    for r in rows[:MAX_REPORTED_DIVERGENCES]
                    if len(r) >= 3
                ]
            counts[table] = _pair_counts(cfg, source_db, restored_db, table)
    except Exception as exc:  # noqa: BLE001
        return {
            "status": STATUS_UNAVAILABLE,
            "reason": REASON_QUERY_FAILED,
            "detail": {"error": str(exc)},
        }

    if diverged:
        return {
            "status": STATUS_VIOLATION,
            "message": (
                f"{len(diverged)} table(s) diverge row-for-row between source and restored "
                f"({sorted(diverged)}) — the restore is PARTIAL, stale or mutated"
            ),
            "detail": {"tables": diverged, "counts": counts},
        }
    # Counts are EVIDENCE that the comparison ran over real rows, never the check
    # itself. Without them an "ok" over an empty corpus is indistinguishable from
    # an "ok" over a comparison that never executed.
    return {"status": STATUS_OK, "detail": {"counts": counts, "tables": len(tables)}}


@observe(tier="stage")
def _pair_counts(cfg: Any, source_db: str, restored_db: str, table: str) -> dict[str, int]:
    """Row counts for both sides of *table*, for the report's ``detail`` only."""
    rows = _run_sql(
        cfg,
        f"SELECT (SELECT COUNT(*) FROM {_q(source_db)}.{_q(table)}), "
        f"(SELECT COUNT(*) FROM {_q(restored_db)}.{_q(table)})",
    )
    if not rows or len(rows[0]) < 2:
        return {"source": -1, "restored": -1}
    return {"source": int(rows[0][0]), "restored": int(rows[0][1])}


# ── check 4: the live schema did not move — the filter's second belt ─────────


@observe(tier="stage")
def _fingerprint(cfg: MariaClientConfig, database: str) -> dict[str, list[str]]:
    """Per-table (count, xor, sum) over row digests — a TAMPER TRIPWIRE.

    Deliberately an aggregate, and that is not the 06-16 shape: this does not
    decide whether a restore is good. Its only job is to notice that the LIVE
    schema changed while the replay ran, which would mean the artifact's
    ``USE``/``CREATE DATABASE`` filter leaked. Any write moves the count or the
    content, and XOR plus SUM together survive reordering while still catching a
    swap of two rows' values.
    """
    out: dict[str, list[str]] = {}
    for table in _list_tables(cfg, database):
        expr = _row_digest_expr(_columns(cfg, database, table))
        rows = _run_sql(
            cfg,
            "SELECT COUNT(*), IFNULL(BIT_XOR(CONV(SUBSTR(d,1,16),16,10)),0), "
            "IFNULL(SUM(CONV(SUBSTR(d,1,16),16,10)),0) FROM ("
            f"SELECT {expr} AS d FROM {_q(database)}.{_q(table)}) u",
        )
        out[table] = list(rows[0]) if rows else []
    return out


@observe(tier="stage", metric="backend.restore.check.source_untouched")
def check_source_untouched(cfg: Any, source_db: str, before: dict | None) -> dict:
    """The LIVE schema is byte-for-byte what it was before the replay ran.

    Without a baseline this cannot assert — and because the op fails closed, an
    ``unavailable`` here REFUSES the restore rather than waving it through. That
    is intended: skipping the baseline is not a way to make the arm quieter.
    """
    if before is None:
        return {
            "status": STATUS_UNAVAILABLE,
            "reason": REASON_NO_BASELINE,
            "detail": {"message": "no pre-replay fingerprint — cannot prove the source is intact"},
        }
    try:
        after = _fingerprint(cfg, source_db)
    except Exception as exc:  # noqa: BLE001
        return {
            "status": STATUS_UNAVAILABLE,
            "reason": REASON_QUERY_FAILED,
            "detail": {"error": str(exc)},
        }

    changed = sorted(
        name for name in set(before) | set(after) if before.get(name) != after.get(name)
    )
    if changed:
        return {
            "status": STATUS_VIOLATION,
            "message": (
                f"the LIVE schema {source_db!r} changed while the restore replayed "
                f"(tables {changed}) — the artifact's schema-selection filter leaked, or a "
                "writer ran under the maintenance gate"
            ),
            "detail": {"changed": changed, "before": before, "after": after},
        }
    return {"status": STATUS_OK, "detail": {"tables": len(after)}}


# ── aggregation ──────────────────────────────────────────────────────────────


@observe(tier="stage", metric="backend.restore.aggregate")
def _aggregate(produced: dict[str, dict]) -> dict:
    """Fold per-check outcomes, enforcing FULL coverage of ``REQUIRED_CHECKS``.

    Car H's fold, and for its reason: a name that never reported is recorded as a
    violation rather than dropped, which is the difference between "every check
    passed" and "no check ran".
    """
    checks = dict(produced)
    violations: list[str] = []
    unavailable: list[str] = []

    for name in sorted(REQUIRED_CHECKS - set(checks)):
        checks[name] = {
            "status": STATUS_VIOLATION,
            "message": f"restore check {name!r} did not report — it was never run",
            "detail": {"missing": True},
        }

    for name in sorted(checks):
        outcome = checks[name]
        if outcome["status"] == STATUS_VIOLATION:
            violations.append(f"restore[{name}]: {outcome.get('message', 'failed')}")
        elif outcome["status"] == STATUS_UNAVAILABLE:
            unavailable.append(f"{name}({outcome.get('reason', 'unspecified')})")

    if violations:
        status = STATUS_VIOLATION
    elif unavailable:
        status = STATUS_UNAVAILABLE
    else:
        status = STATUS_OK

    return {
        "status": status,
        "checks": checks,
        "violations": violations,
        "unavailable": unavailable,
    }


@observe(tier="boundary", metric="backend.restore.verify")
def verify_restore(
    cfg: Any, source_db: str, restored_db: str, source_before: dict | None = None
) -> dict:
    """Enumerate *restored_db* against *source_db* and report tri-state.

    The order is a dependency chain, not a preference: the row check needs the
    shared table list, and the column check guards the row check's query from
    erroring on a shape mismatch. Later checks are skipped as UNAVAILABLE when an
    earlier one denies them their precondition — never as ``ok``.
    """
    results: dict[str, dict] = {}
    results[CHECK_TABLE_SET] = check_table_set(cfg, source_db, restored_db)

    table_outcome = results[CHECK_TABLE_SET]
    shared: list[str] = []
    if table_outcome["status"] == STATUS_OK:
        shared = list(table_outcome["detail"]["tables"])
    else:
        detail = table_outcome.get("detail", {})
        shared = sorted(set(detail.get("source", [])) & set(detail.get("restored", [])))

    if table_outcome["status"] == STATUS_UNAVAILABLE:
        blocked = {
            "status": STATUS_UNAVAILABLE,
            "reason": table_outcome.get("reason", REASON_QUERY_FAILED),
            "detail": {"message": "the table list could not be read — nothing to compare"},
        }
        results[CHECK_COLUMN_SETS] = dict(blocked)
        results[CHECK_ROW_IDENTITY] = dict(blocked)
    else:
        results[CHECK_COLUMN_SETS] = check_column_sets(cfg, source_db, restored_db, shared)
        comparable = (
            shared
            if results[CHECK_COLUMN_SETS]["status"] == STATUS_OK
            else [
                t
                for t in shared
                if t not in results[CHECK_COLUMN_SETS].get("detail", {}).get("mismatches", {})
            ]
        )
        results[CHECK_ROW_IDENTITY] = check_row_identity(cfg, source_db, restored_db, comparable)

    results[CHECK_SOURCE_UNTOUCHED] = check_source_untouched(cfg, source_db, source_before)
    return _aggregate(results)


# ── the replay (reachable ONLY through the gated op) ─────────────────────────


@observe(tier="stage")
def _create_scratch(cfg: MariaClientConfig, scratch_db: str) -> None:
    """Create the throwaway schema the artifact is replayed into."""
    _run_sql(cfg, f"CREATE DATABASE {_q(scratch_db)}")


@observe(tier="stage")
def _drop_scratch(cfg: MariaClientConfig, scratch_db: str) -> None:
    """Drop the throwaway schema. Best-effort — never masks the real failure."""
    try:
        _run_sql(cfg, f"DROP DATABASE IF EXISTS {_q(scratch_db)}")
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "could not drop the restore-verification scratch schema %s: %s", scratch_db, exc
        )


@observe(tier="stage")
def _replay_artifact(
    cfg: MariaClientConfig, artifact: Path, scratch_db: str, source_db: str
) -> int:
    """Stream *artifact* through the filter into *scratch_db*. Returns lines fed.

    STREAMED, never read whole. A logical dump of a real corpus is arbitrarily
    large and this process shares a container with the backend; materialising it
    would trade a memory ceiling for a restore that cannot run when it is most
    needed. stderr goes to a TEMP FILE rather than a pipe for the same class of
    reason — a pipe's buffer is finite and a chatty client would deadlock against
    a writer that is still feeding stdin.
    """
    binary = _client_binary()
    if binary is None:
        raise RuntimeError("mariadb client not found on PATH")
    argv = [binary, f"--defaults-file={cfg.option_file}", scratch_db]

    fed = 0
    # ``with`` on BOTH: Popen's context manager is what closes the stdin pipe and
    # reaps the child on every exit path. Without it a raise mid-feed leaves the
    # object to the garbage collector, which surfaces as an unraisable
    # ``Popen.__del__`` warning — noise on a path whose whole job is to make
    # failures legible.
    with (
        tempfile.TemporaryFile() as errfile,
        subprocess.Popen(  # noqa: S603 — fixed argv, no shell
            argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errfile
        ) as proc,
    ):
        assert proc.stdin is not None  # noqa: S101 — guaranteed by stdin=PIPE
        try:
            with artifact.open("r", encoding="utf-8", errors="replace") as handle:
                for line in filter_dump_statements(handle, source_db):
                    proc.stdin.write(line.encode("utf-8", errors="replace"))
                    fed += 1
        except BrokenPipeError:
            # The client died mid-feed; its own exit status and stderr say why,
            # and both are read below.
            pass
        finally:
            proc.stdin.close()

        try:
            proc.wait(timeout=REPLAY_TIMEOUT_SEC)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=30)
            raise RuntimeError(f"restore replay timed out after {REPLAY_TIMEOUT_SEC}s") from None

        if proc.returncode != 0:
            errfile.seek(0)
            stderr = errfile.read().decode(errors="replace").strip()
            raise RuntimeError(f"restore replay exited {proc.returncode}: {stderr[:400]}")
    return fed


@observe(tier="stage")
def _unavailable(reason: str, message: str) -> dict:
    """A whole-arm ``unavailable`` report, shaped like a normal one.

    Built through ``_aggregate`` so the coverage guard fills in every required
    check rather than the caller hand-rolling a shorter dict that would then need
    its own handling one layer up.
    """
    return _aggregate(
        {
            name: {"status": STATUS_UNAVAILABLE, "reason": reason, "detail": {"message": message}}
            for name in REQUIRED_CHECKS
        }
    )


@observe(tier="boundary", metric="backend.admin.mariadb_restore_verify")
def mariadb_restore_verify(payload: dict) -> dict:
    """Restore a ``mariadb-dump`` artifact into a scratch schema and ENUMERATE it.

    payload:
        ``filename`` — BASENAME of an artifact under the container's own dump
        directory. A path is REJECTED; car F's rule is that the payload never
        carries one, because a host-supplied absolute path resolves inside the
        container's namespace and silently means something else.

    Returns:
        ``{"ok": True, "status": "ok", "artifact": str, "scratch_database": str,
        "lines_replayed": int, "checks": {...}, "violations": [], "unavailable": []}``

    Raises:
        RestoreVerificationError: on ``violation`` OR ``unavailable``, carrying
            the full report on ``.report``. A restore this arm could not PROVE
            good is refused — see the module docstring on failing closed.
    """
    raw = str(payload.get("filename") or "")
    if not raw or raw != Path(raw).name or raw in {".", ".."}:
        raise RestoreVerificationError(
            f"restore artifact must be a basename under the container's dump directory, "
            f"got {raw!r}",
            _unavailable(REASON_ARTIFACT_REJECTED, f"not a basename: {raw!r}"),
        )

    if _client_binary() is None:
        raise RestoreVerificationError(
            "mariadb client not found on PATH. Engine #2's restore arm runs INSIDE the "
            "backend container, where the mariadb-server apt install (Dockerfile.backend) "
            "provides it; a host process has neither the binary nor a reachable socket.",
            _unavailable(REASON_CLIENT_ABSENT, "the mariadb client binary is not installed"),
        )

    cfg = read_client_option_file()
    artifact = _dump_dir(cfg.option_file) / raw
    if not artifact.is_file():
        raise RestoreVerificationError(
            f"restore artifact {raw!r} is not present at {artifact}",
            _unavailable(REASON_ARTIFACT_ABSENT, f"no such artifact: {artifact}"),
        )

    scratch_db = f"{cfg.database}{SCRATCH_INFIX}{uuid.uuid4().hex[:12]}"
    try:
        before = _fingerprint(cfg, cfg.database)
    except Exception as exc:  # noqa: BLE001
        raise RestoreVerificationError(
            f"could not fingerprint the live schema before replaying {raw!r}: {exc}",
            _unavailable(REASON_QUERY_FAILED, str(exc)),
        ) from exc

    try:
        _create_scratch(cfg, scratch_db)
        try:
            fed = _replay_artifact(cfg, artifact, scratch_db, cfg.database)
        except Exception as exc:
            # A replay that ERRORED still may have half-written the scratch, and
            # the source belt has not run yet — report it as a first-class
            # refusal rather than letting the enumeration describe wreckage.
            raise RestoreVerificationError(
                f"replaying {raw!r} into {scratch_db} failed: {exc}",
                _unavailable(REASON_REPLAY_FAILED, str(exc)),
            ) from exc
        report = verify_restore(cfg, cfg.database, scratch_db, before)
    finally:
        _drop_scratch(cfg, scratch_db)

    if report["status"] != STATUS_OK:
        for violation in report["violations"]:
            logger.critical("restore verification: %s", violation)
        if report["unavailable"]:
            logger.critical(
                "restore verification could not run: %s", ", ".join(report["unavailable"])
            )
        raise RestoreVerificationError(
            f"restore verification for {raw!r} did NOT pass (status={report['status']}): "
            + "; ".join(report["violations"] or report["unavailable"]),
            report,
        )

    logger.info(
        "engine #2 restore verified by enumeration",
        extra={
            "component": "admin.mariadb_restore_verify",
            "action": "mariadb_restore_verify",
            "outcome": "ok",
            "artifact": raw,
        },
    )
    return {
        "ok": True,
        "artifact": raw,
        "scratch_database": scratch_db,
        "lines_replayed": fed,
        "database": cfg.database,
        **report,
    }
