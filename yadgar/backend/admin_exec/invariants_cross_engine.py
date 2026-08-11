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
REASON_EMBEDDED_MODE = "embedded_mode_no_migrations"

# ── check names ──────────────────────────────────────────────────────────────

CHECK_ALEMBIC_CHAIN_SHAPE = "alembic_chain_shape"
CHECK_ENGINE_TWO_SCHEMA_HEAD = "engine_two_schema_head"
CHECK_SURREAL_SCHEMA_HEAD = "surreal_schema_head"
CHECK_CONFIG_ROW_BASELINE = "config_row_baseline"
CHECK_PAGE_ROW_DESYNC = "page_row_desync"
CHECK_SUPERSEDED_ADR_EXCLUSION = "superseded_adr_exclusion"

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
        CHECK_SUPERSEDED_ADR_EXCLUSION,
    }
)

CONFIG_TABLE = "config"
ADR_TABLE = "adr"
REASON_ADR_TABLE_ABSENT = "adr_table_absent"

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
    if not getattr(storage, "_db_url", None):
        # ``_run_migrations`` early-returns on a falsy ``_db_url``
        # (``_shared/storage/__init__.py``): the embedded SurrealDB v2 package
        # predates HNSW and the chain is server-mode only. So an empty
        # ``schema_version`` is the DEFINED state here, not drift — reporting a
        # violation would be a false red on every embedded dev/test stack. It is
        # still not ``ok``: the check genuinely cannot assert.
        return {
            "status": STATUS_UNAVAILABLE,
            "reason": REASON_EMBEDDED_MODE,
            "detail": {"message": "embedded SurrealDB — migrations do not run in this mode"},
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


# ── assertion 3: cross-engine page/row desync — ADR-0209 LIVE comparison ─────


# Tables where ADR-0209's ``content_hash`` is mirrored: a wiki body page (slug
# keyed) lives next to a ledger row (name keyed). The probe below reads both
# sides and reports a violation when the hash disagrees. ADR-0198 names the
# spine ledger tables; only those with a corresponding wiki body participate.
_HASH_MIRRORED_TABLES: tuple[tuple[str, str, str], ...] = (
    # (sql_table_name, wiki_slug_prefix, engine_method_name)
    ("agent_pattern", "agent-prompt-", "list_agent_prompt_rows"),
    ("agent_discipline", "agent-discipline-", "list_agent_discipline_rows"),
)


@observe(tier="stage", metric="backend.invariants.cross_engine.compare_page_row")
def _compare_page_row(*, tbl: str, slug_prefix: str, row: dict, storage: Any) -> dict | None:
    """Compare one ledger row's ``content_hash`` against its wiki body page.

    Returns one of:
      - ``{"reason": "matched", ...}`` — hashes agree; caller records it.
      - ``{"reason": "wiki_page_missing" | "content_hash_mismatch", ...}`` —
        a real disagreement; caller records a violation.
      - ``None`` — no row data; caller skips.

    Extracted from ``check_page_row_desync`` to keep the parent's cyclomatic
    below the 15 HARD cap: each row's branching (lookup, hash, compare) is
    naturally three-way and would push the orchestrator above the cap if kept
    inline. The helper preserves the tripwire semantic — each return shape is
    the same shape ``check_page_row_desync`` previously appended to
    ``violations`` or ``compared`` — and stays private to this module.
    """
    import hashlib

    name = row.get("name")
    body_slug = row.get("body_slug") or f"{slug_prefix}{name}"
    row_hash = row.get("content_hash") or ""
    try:
        page = storage.get_wiki_page_by_slug(body_slug)
    except Exception:  # noqa: BLE001
        page = None
    if page is None:
        return {
            "table": tbl,
            "name": name,
            "body_slug": body_slug,
            "reason": "wiki_page_missing",
        }
    content = page.get("content", "")
    page_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    if page_hash != row_hash:
        return {
            "table": tbl,
            "name": name,
            "body_slug": body_slug,
            "reason": "content_hash_mismatch",
            "page_hash": page_hash[:16],
            "row_hash": row_hash[:16],
        }
    return {"reason": "matched", "name": name, "body_slug": body_slug}


@observe(tier="stage", metric="backend.invariants.cross_engine.page_row_desync")
def _resolve_storage(storage: Any) -> Any:
    """Resolve the storage handle for the page-row-desync check.

    Car J (0047): the module's local import of ``_get_storage`` from
    ``yadgar._shared.runtime.lifecycle`` is not patchable through the module
    (local import = unbound symbol on the module). When the orchestrator at
    ``run_cross_engine_checks`` already has a storage handle in ctx, threading
    it through the registry entry avoids the unpatchable seam. The
    live-process path keeps working because the default is None — the runtime
    production code never passed storage through; pytest passes a fake.
    """
    if storage is not None:
        return storage
    from yadgar._shared.runtime.lifecycle import _get_storage  # noqa: PLC0415

    return _get_storage()


@observe(tier="stage", metric="backend.invariants.cross_engine.compare_table")
async def _compare_table(
    *,
    tbl: str,
    slug_prefix: str,
    list_method: str,
    engine: Any,
    storage: Any,
) -> dict | None:
    """Walk one ledger table's rows and compare each against the wiki body.

    Returns one of:
      - ``{"violations": [...], "compared": [...]}`` — list complete.
      - ``{"unavailable": {"table": tbl, "error": str(exc)}}`` — the list
        call failed; the orchestrator decides how to surface the error.
    """
    rows = await getattr(engine, list_method)()
    violations: list[dict] = []
    compared: list[dict] = []
    for row in rows:
        entry = _compare_page_row(tbl=tbl, slug_prefix=slug_prefix, row=row, storage=storage)
        if entry is None:
            continue
        if entry.get("reason") == "matched":
            compared.append({"table": tbl, "name": entry["name"], "body_slug": entry["body_slug"]})
        else:
            violations.append(entry)
    return {"violations": violations, "compared": compared}


@observe(tier="stage", metric="backend.invariants.cross_engine.page_row_desync")
async def check_page_row_desync(engine: Any, storage: Any = None) -> dict:
    """ADR-0209's mirrored ``content_hash`` — LIVE for 0047 Car I.

    Compares the wiki body page's bytes (sha256 of the content column) against
    the ledger row's ``content_hash`` column for every ``agent_pattern`` /
    ``agent_discipline`` row. Disagreement IS the violation (ADR-0209, D40).
    Probes once per ledger table; surfaces per-row disagreement in ``detail``.

    ``adr`` is listed in ``SPINE_LEDGER_TABLES`` but excluded here because adr
    pages live in ``wiki_page`` alongside other types and do not carry a
    ``content_hash`` mirror (the adr→wiki-page invariant lives elsewhere —
    see ``yadgar.core.server.tools.project._build_adr_log``). Car J keeps the
    list scoped to the agent-prompt/discipline mirrors where the contract is
    written.

    Engine-#2 absence still returns ``unavailable`` (car H's vacuous-pass
    guard — never silently pass on absent data).
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

    if not any(tbl in tables for tbl, _slug_prefix, _method in _HASH_MIRRORED_TABLES):
        return {
            "status": STATUS_UNAVAILABLE,
            "reason": REASON_SPINE_NOT_SHIPPED,
            "detail": {
                "absent_tables": sorted(tbl for tbl, _slug, _method in _HASH_MIRRORED_TABLES),
                "message": (
                    "ADR-0209's content_hash mirror tables are absent; spine train "
                    "table create (0047 Car A / Car I) hasn't shipped."
                ),
            },
        }

    storage = _resolve_storage(storage)
    violations: list[dict] = []
    compared: list[dict] = []
    present_tables: list[str] = []
    for tbl, slug_prefix, list_method in _HASH_MIRRORED_TABLES:
        if tbl not in tables:
            continue
        present_tables.append(tbl)
        try:
            outcome = await _compare_table(
                tbl=tbl,
                slug_prefix=slug_prefix,
                list_method=list_method,
                engine=engine,
                storage=storage,
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "status": STATUS_UNAVAILABLE,
                "reason": REASON_QUERY_FAILED,
                "detail": {"table": tbl, "error": str(exc)},
            }
        if outcome is None:
            continue
        violations.extend(outcome["violations"])
        compared.extend(outcome["compared"])

    if violations:
        return {
            "status": STATUS_VIOLATION,
            "message": (
                f"{len(violations)} page/row desync violation(s) across "
                f"{sorted({v['table'] for v in violations})} — see detail"
            ),
            "detail": {
                "violations": violations,
                "compared": len(compared),
                "present_tables": present_tables,
            },
        }
    return {
        "status": STATUS_OK,
        "detail": {
            "compared": len(compared),
            "tables": [t for t, _slug, _method in _HASH_MIRRORED_TABLES],
            "present_tables": present_tables,
        },
    }


# ── assertion 4: the superseded-ADR exclusion set matches SQL (Car C8) ───────


@observe(tier="stage", metric="backend.invariants.cross_engine.superseded_sql_truth")
async def _superseded_rows_by_project(engine: Any) -> dict[str, list[dict]]:
    """Read EVERY superseded ``adr`` row, grouped by project, with its OWN SQL.

    THE INDEPENDENCE IS THE POINT. The recall path loads its exclusion set
    through ``list_adr_rows``; this reads ``list_superseded_adr_rows``, a
    SEPARATELY WRITTEN corpus-wide query that exists for this check alone. A
    check calling the same accessor the mechanism calls would compare a
    function against itself and pass for every bug that function can have —
    the vacuous-pass shape this module was written to eliminate. The two
    queries live in the same class only because D20 requires every ledger row
    access to go through ``MariaStorageEngine``.

    Grouping by project also comes free: the loader is project-scoped, so the
    check must be able to enumerate the projects it should be asked about
    rather than being told which ones to look at.
    """
    rows = await engine.list_superseded_adr_rows()

    by_project: dict[str, list[dict]] = {}
    for row in rows:
        by_project.setdefault(str(row.get("project_id") or ""), []).append(row)
    return by_project


@observe(tier="stage", metric="backend.invariants.cross_engine.superseded_project")
async def _check_superseded_for_project(
    engine: Any, project_id: str, rows: list[dict]
) -> list[str]:
    """Return this project's violation messages (empty list = agreement).

    Three assertions, each its own message because each has a different fix:

    (a) COVERAGE — ``adr.body_slug`` is ``nullable=True`` in migration 002. A
        superseded row with no slug CANNOT be excluded by a slug predicate; the
        exclusion silently does not apply to it. The fix is stamping the row.
    (b) ROUND-TRIP — the params the PRODUCTION clause builder actually binds,
        reached through ``RecallScope`` exactly as recall reaches it, must equal
        this check's own slug set. This is what catches a dataclass hop that
        drops ``excluded_slugs``: the loader can be perfect and the clause still
        carry nothing. The fix is in the plumbing, not the data.
    (c) EMISSION — a non-empty set must produce a ``slug NOT IN`` fragment. The
        fix is restoring the arm a refactor deleted.
    """
    from yadgar._shared.storage.directory import RecallScope  # noqa: PLC0415
    from yadgar.backend.retrieval.superseded import load_superseded_slugs  # noqa: PLC0415

    violations: list[str] = []
    expected = {str(r["body_slug"]) for r in rows if r.get("body_slug")}

    # (a) coverage
    unstamped = sorted(str(r.get("id")) for r in rows if not r.get("body_slug"))
    if unstamped:
        violations.append(
            f"{len(unstamped)} superseded adr row(s) in {project_id!r} carry no body_slug "
            f"and therefore CANNOT be excluded from recall (adr ids {unstamped}) — "
            "they rank normally today"
        )

    loaded = await load_superseded_slugs(engine, project_id=project_id)

    # (b) round-trip THROUGH the production clause builder.
    sql, params = RecallScope(project_id=project_id, excluded_slugs=tuple(loaded)).clause()
    bound: set[str] = set()
    for key, value in params.items():
        if key.endswith("_excl_slugs"):
            bound = {str(v) for v in value}
    if bound != expected:
        missing = sorted(expected - bound)
        extra = sorted(bound - expected)
        violations.append(
            f"superseded-ADR exclusion for {project_id!r} DISAGREES with the ledger: "
            f"SQL says {len(expected)} superseded page(s), the recall clause binds "
            f"{len(bound)}; missing={missing} unexpected={extra} — superseded ADRs "
            "matching 'missing' rank normally in recall right now"
        )

    # (c) emission
    if expected and "slug NOT IN" not in sql:
        violations.append(
            f"the recall scope clause for {project_id!r} emits no slug-exclusion arm "
            f"while {len(expected)} superseded ADR(s) exist — the WHERE arm is gone"
        )
    return violations


@observe(tier="stage", metric="backend.invariants.cross_engine.superseded_adr_exclusion")
async def check_superseded_adr_exclusion(engine: Any) -> dict:
    """Car C8 (0047 §5 C8) — SQL status ↔ recall exclusion consistency.

    NOTHING ELSE ENFORCES THIS, AND THE FAILURE IS INVISIBLE. A stale or
    silently-empty exclusion set does not raise, does not empty a result list,
    and does not look wrong: superseded ADRs simply rank normally again. Every
    other symptom this repo has learned to watch for is absent. That is the
    ADR-0080 lesson applied to C8's own mechanism, which is why the check is a
    first-class member of ``REQUIRED_CHECKS`` (nightly) rather than a unit test.

    Absence stays ``unavailable``, never ``ok`` — same rule as every sibling.
    """
    if engine is None:
        return {
            "status": STATUS_UNAVAILABLE,
            "reason": REASON_ENGINE_TWO_ABSENT,
            "detail": {"message": "engine #2 is not composed — adr.status is unreadable"},
        }
    try:
        tables = set(await engine.list_tables())
    except Exception as exc:  # noqa: BLE001 — a read failure is UNAVAILABLE, not ok
        return {
            "status": STATUS_UNAVAILABLE,
            "reason": REASON_QUERY_FAILED,
            "detail": {"error": str(exc)},
        }
    if ADR_TABLE not in tables:
        return {
            "status": STATUS_UNAVAILABLE,
            "reason": REASON_ADR_TABLE_ABSENT,
            "detail": {"message": f"the {ADR_TABLE!r} ledger table does not exist"},
        }

    try:
        by_project = await _superseded_rows_by_project(engine)
    except Exception as exc:  # noqa: BLE001
        return {
            "status": STATUS_UNAVAILABLE,
            "reason": REASON_QUERY_FAILED,
            "detail": {"error": str(exc)},
        }

    violations: list[str] = []
    for project_id in sorted(by_project):
        try:
            violations.extend(
                await _check_superseded_for_project(engine, project_id, by_project[project_id])
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "status": STATUS_UNAVAILABLE,
                "reason": REASON_QUERY_FAILED,
                "detail": {"project_id": project_id, "error": str(exc)},
            }

    rows_total = sum(len(v) for v in by_project.values())
    detail = {
        "superseded_rows": rows_total,
        "projects": sorted(by_project),
    }
    if violations:
        return {
            "status": STATUS_VIOLATION,
            "message": "; ".join(violations),
            "detail": {**detail, "violations": violations},
        }
    # A zero-row corpus reports ok WITH the count, not on silence: the read ran
    # and returned evidence. "No superseded ADRs" and "the read never happened"
    # must not look the same, which is this module's founding complaint.
    return {"status": STATUS_OK, "detail": detail}


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
    (CHECK_PAGE_ROW_DESYNC, lambda ctx: check_page_row_desync(ctx["engine"], ctx["storage"])),
    # Car C8 (0047 §5 C8): SQL adr.status ↔ the recall exclusion set.
    (CHECK_SUPERSEDED_ADR_EXCLUSION, lambda ctx: check_superseded_adr_exclusion(ctx["engine"])),
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
