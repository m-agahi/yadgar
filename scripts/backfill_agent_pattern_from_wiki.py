#!/usr/bin/env python3
"""Bug-bag-2 train 2026-08-23 — agent_pattern/agent_discipline ledger backfill.

Walks ``wiki_page`` rows whose slug carries the ``agent-prompt-`` /
``agent-discipline-`` prefix and inserts a row into the corresponding
``agent_pattern`` (resp. ``agent_discipline``) SQL ledger table for every wiki
page that does NOT already have one. Idempotent.

Tasks 200 / 268 / 90. The SQL TOC + the discovery surface read
``agent_pattern``, and the discovery surface's absence-of-row failure mode is
silent: every ``agent_dispatch_prelude`` call missed every wiki-canonicalised
pattern that was never mirrored into the ledger. The seed script
(``seed_agent_prompts``) seeds the starter library; this script picks up the
wiki-canonicalised material those starters miss — patterns added via
``agent_prompt_save`` after the seed ran, or material a migration deposited
directly into wiki_page.

RUN IT THROUGH THE PROCEDURE, NOT BY HAND
=========================================

``docs/prompts/backfill-agent-pattern-ledger.md`` is the human-run procedure
this script serves (ADR-0005 — corpus backfills are procedures, not
automation). There is deliberately NO ``yadgar backfill`` CLI flag: a flag is
"a command that can duplicate the corpus in one invocation", which is the risk
ADR-0005 exists to prevent.

WHAT IT SCANS
=============

``storage.list_wiki_pages(slug_prefix=..., limit=...)``, once per page type in
scope, using the slug-prefix map the cross-engine invariant already treats as
authoritative (``yadgar/backend/admin_exec/invariants_cross_engine.py``
``_HASH_MIRRORED_TABLES``). Those two keywords are the WHOLE signature this
script uses — see ``StorageEngine.list_wiki_pages`` in
``yadgar/_shared/storage/wiki.py``. ``_SCAN_KWARGS`` names them and the test
suite pins that set against the real method, because the previous revision of
this script invented three parameters (``project_id``, ``page_type``,
``from_slug``) that the engine has never had and therefore raised
``TypeError`` on the dry run, before any write.

THE LEDGER TABLES ARE NOT PROJECT-SCOPED
========================================

``agent_pattern`` and ``agent_discipline`` have no ``project_id`` column
(``migrations/versions/002_ledger_tables.py``); both carry ``UNIQUE(name)``
across the whole install. So there is NO project scope on this script at all,
and none is offered: a knob that cannot narrow the write is a knob that lies.
The wiki reader's own directory filter would not help either — it matches
``directory_context IN (D, 'global')`` and every page ``agent_prompt_save`` /
``discipline_save`` writes declares ``directory="global"``, so it cannot
exclude a single agent-library page. Backfilling one project's pages changes
every project's discovery surface. That is a property of the schema, not of
this script — the procedure states it as a STOP. (ADR-0225 also retires
``directory`` as a scoping concept outright; the residue sweep enforces it.)

DRY-RUN BY DEFAULT — ``--apply`` is required to write anything.

IDEMPOTENT — a second run reports 0 inserted rows. Each wiki page is keyed by
its ``slug`` against the ledger row's ``name`` (the convention core
``agent_prompt_save`` / ``discipline_save`` write — slug is
``agent-prompt-<pattern>`` / ``agent-discipline-<name>``, ledger ``name`` is
the bare ``<pattern>`` / ``<name>`` in BOTH cases, see
``yadgar/core/server/tools/agent_prompts.py``).

OUT OF SCOPE
============

* ``agent_pattern_composes`` edge rows. The script seeds ``agent_pattern`` /
  ``agent_discipline`` rows only; the composition edges (which disciplines a
  pattern composes) come from a later car.
* ``baseline_hash`` derivation. The core write path derives
  ``baseline_hash`` on the first write; this backfill leaves it NULL and the
  operator's first pattern mutation will set it (the schema column is
  nullable).
* Repairing a ``content_hash`` disagreement. Already-present rows whose hash
  disagrees with the wiki body are REPORTED (``content_hash_mismatches``) and
  never rewritten — the row may be newer than the page.

Usage:
    uv run scripts/backfill_agent_pattern_from_wiki.py            # dry run
    uv run scripts/backfill_agent_pattern_from_wiki.py --apply    # write
    uv run scripts/backfill_agent_pattern_from_wiki.py --page-type agent_discipline
    uv run scripts/backfill_agent_pattern_from_wiki.py --limit 50

Exit codes:
  0  completed (dry run, or apply succeeded)
  1  one or more inserts failed
  2  fatal error (DB unreachable, ledger read failed, bad args)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from pathlib import Path

# Suppress the OTLP exporter BEFORE any yadgar import (same guard as the
# rest of the scripts/ package — keeps the offline suite green when no
# collector is running).
os.environ.setdefault("YADGAR_OTLP_ENDPOINT", "")

_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

logging.basicConfig(
    level=logging.INFO,
    format='{"ts": "%(asctime)s", "level": "%(levelname)s", "event": "%(message)s"}',
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("backfill_agent_pattern_from_wiki")


#: Page type -> wiki slug prefix. Mirrors ``_HASH_MIRRORED_TABLES`` in
#: ``yadgar/backend/admin_exec/invariants_cross_engine.py``, which is the
#: authority for the page<->row pairing the cross-engine invariant compares.
PAGE_TYPE_SLUG_PREFIX: dict[str, str] = {
    "agent_pattern": "agent-prompt-",
    "agent_discipline": "agent-discipline-",
}

#: Page type -> the admin op that lists its ledger rows.
PAGE_TYPE_LIST_OP: dict[str, str] = {
    "agent_pattern": "list_agent_prompt_rows",
    "agent_discipline": "list_agent_discipline_rows",
}

#: The EXACT keyword set ``_scan_wiki_pages`` forwards to
#: ``StorageEngine.list_wiki_pages``. A test pins this against
#: ``inspect.signature`` of the real method so the script can never again call
#: a signature that only its own fake implements.
_SCAN_KWARGS: frozenset[str] = frozenset({"slug_prefix", "limit"})

#: Skip buckets reported per run. Every drop path increments exactly one of
#: these, so ``scanned`` reconciles against the sum (ADR-0420: a report an
#: operator decides on must attribute every row it did not act on).
SKIP_BUCKETS: tuple[str, ...] = (
    "skipped_unknown_page_type",
    "skipped_page_type_filtered",
    "skipped_non_string_content",
    "skipped_empty_slug",
)


class LedgerReadError(RuntimeError):
    """The ledger-side read failed, so the already-present set is unknown.

    NEVER degrade this to an empty set. ``_existing_ledger_rows`` drives
    idempotency: an empty set means "no row exists for any page", so the apply
    path would treat the ENTIRE corpus as insertable and upsert every row —
    ADR-0005's duplicate-the-corpus failure mode reproduced through a new
    mechanism. ``UNIQUE(name)`` makes it an upsert rather than a duplicate, so
    it would not error; it would silently overwrite every row's ``purpose``
    and ``status`` with this script's defaults.
    """


# ── Pure helpers (unit-test targets) ─────────────────────────────────────


def _slug_to_name(slug: str) -> str:
    """Derive the ledger ``name`` from a wiki agent-library slug.

    BOTH page types strip their prefix. ``agent_prompt_save`` writes slug
    ``agent-prompt-<pattern>`` / ledger ``name=<pattern>``, and
    ``discipline_save`` writes slug ``agent-discipline-<name>`` / ledger
    ``name=<name>`` (``agent_prompts.py`` — the ``_forward_admin`` payloads
    pass the bare name in both cases). A discipline-side passthrough would
    seed ``name="agent-discipline-x"`` where core writes ``name="x"``, so the
    idempotency key would never match core's own rows and every run would
    re-insert.

    NOT reversible from the name alone, and deliberately so: stripping either
    prefix loses which one it was, so ``name`` cannot say whether to re-attach
    ``agent-prompt-`` or ``agent-discipline-``. ``_classify_page_type`` is what
    disambiguates, and every row this script builds carries both the resolved
    ``page_type`` and the original ``body_slug`` — the round trip runs through
    those, never through the name.

    Pages outside both conventions pass through unchanged; the backfill then
    seeds them under that literal name, which is the operator's authority over
    their own slug scheme.
    """
    for prefix in PAGE_TYPE_SLUG_PREFIX.values():
        if slug.startswith(prefix):
            return slug[len(prefix) :]
    return slug


def _content_hash(text: str) -> str:
    """Stable sha256 hex digest of a wiki body.

    The ledger column ``content_hash`` is the ``_content_hash`` algorithm from
    ``yadgar/core/server/tools/agent_prompts.py`` — re-deriving it here (no
    import) so the script can run in an environment that does not have the
    full app composed. Two implementations of the same hash would defeat the
    cross-engine invariant that compares them — this one matches by literal
    construction.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _classify_page_type(slug: str, page_type: str | None) -> str:
    """Decide whether a wiki page is an agent_pattern or agent_discipline target.

    Returns one of ``"agent_pattern"``, ``"agent_discipline"``, or ``""``
    (skip). An explicit ``page_type`` wins. Absent it, the SLUG PREFIX decides
    — the scan is prefix-driven, so a row that reached this function without a
    type still carries the prefix that selected it. Only when neither is
    conclusive does it fall back to ``agent_pattern``, because the legacy
    ``agent_prompt_save`` write path predates the type split.
    """
    if page_type in PAGE_TYPE_SLUG_PREFIX:
        return str(page_type)
    if page_type not in (None, "", "agent_prompt"):
        return ""
    for candidate, prefix in PAGE_TYPE_SLUG_PREFIX.items():
        if slug.startswith(prefix):
            return candidate
    return "agent_pattern"


def _build_rows_for_apply(
    wiki_rows: list[dict],
    *,
    page_type_filter: str,
) -> tuple[list[dict], dict[str, int]]:
    """Filter ``wiki_rows`` to the candidate set, and count every row dropped.

    Returns ``(rows, skips)``. ``skips`` carries one counter per drop reason
    (``SKIP_BUCKETS``) so ``scanned`` reconciles exactly — the previous
    revision dropped rows on three paths that nothing counted, and the caller
    then re-classified every row a SECOND time to derive one of the three,
    which could disagree with what this pass actually did.

    Each returned dict carries the keys the apply path needs:
    ``name``, ``body_slug``, ``content``, ``content_hash``, ``page_type``.
    """
    out: list[dict] = []
    skips = dict.fromkeys(SKIP_BUCKETS, 0)
    for row in wiki_rows:
        slug = row.get("slug") or ""
        page_type = _classify_page_type(slug, row.get("page_type"))
        if not page_type:
            skips["skipped_unknown_page_type"] += 1
            continue
        if page_type_filter != "both" and page_type != page_type_filter:
            skips["skipped_page_type_filtered"] += 1
            continue
        content = row.get("content")
        if not isinstance(content, str):
            skips["skipped_non_string_content"] += 1
            continue
        if not slug:
            skips["skipped_empty_slug"] += 1
            continue
        out.append(
            {
                "name": _slug_to_name(slug),
                "body_slug": slug,
                "content": content,
                "content_hash": _content_hash(content),
                "page_type": page_type,
            }
        )
    return out, skips


def _page_types_in_scope(page_type: str) -> tuple[str, ...]:
    """``"both"`` is LOOP CONTROL, never a filter value.

    The previous revision passed the literal string ``"both"`` through to the
    storage layer as if it were a page type, which it is not.
    """
    if page_type == "both":
        return tuple(PAGE_TYPE_SLUG_PREFIX)
    return (page_type,)


# ── Side-effecting apply path ─────────────────────────────────────────────


def _scan_wiki_pages(
    storage,
    *,
    page_types: tuple[str, ...],
    limit: int,
) -> list[dict]:
    """Read candidate wiki pages, one ``list_wiki_pages`` call per page type.

    ``StorageEngine.list_wiki_pages`` cannot filter on ``page_type``, so the
    scan narrows by SLUG PREFIX instead — the same key the cross-engine
    invariant pairs page to row on. Results are de-duplicated by slug because
    the two prefixes are disjoint today but the loop should not depend on it.

    The reader's directory filter is deliberately NOT used: the destination
    tables are install-wide, so narrowing the read would only hide rows from
    the census while the write stayed global.

    ``limit`` is a per-page-type cap and its meaning follows the reader's
    ``ORDER BY updated_at DESC``: it is "the N most recently updated pages",
    not a stable window. It exists to keep an exploratory dry run cheap on a
    large corpus, not to page through one.
    """
    seen: set[str] = set()
    out: list[dict] = []
    for page_type in page_types:
        kwargs = {
            "slug_prefix": PAGE_TYPE_SLUG_PREFIX[page_type],
            "limit": limit if limit > 0 else None,
        }
        assert set(kwargs) == set(_SCAN_KWARGS)  # noqa: S101 — pinned by test
        for row in storage.list_wiki_pages(**kwargs) or []:
            slug = row.get("slug") or ""
            if slug in seen:
                continue
            seen.add(slug)
            out.append(row)
    return out


def _existing_ledger_rows(*, page_type: str) -> dict[str, dict]:
    """Read the ledger rows already present, keyed by ``name``.

    Forwards the registered admin op (``list_agent_prompt_rows`` /
    ``list_agent_discipline_rows``) the same way ``_insert_one`` forwards the
    write op — the ledger lives on engine #2 and those coroutines are
    ``MariaStorageEngine`` methods, NOT ``StorageEngine`` ones, so calling
    them off the wiki-side storage handle (as the previous revision did)
    raises ``AttributeError``.

    Raises ``LedgerReadError`` on the ``{"ok": False, "error": ...}`` envelope
    or a missing ``rows`` key. See that class for why degrading to ``{}`` is
    the dangerous option.

    Cross-project rows are returned too — ``agent_pattern`` has no
    ``project_id`` column, so a wiki page that already has a row under ANY
    project counts as "already present" and is skipped.
    """
    from yadgar.core.forward import _forward_admin  # noqa: PLC0415

    op = PAGE_TYPE_LIST_OP.get(page_type)
    if op is None:
        return {}
    result = _forward_admin(op, {}) or {}
    if result.get("ok") is False:
        raise LedgerReadError(f"{op} failed: {result.get('error')!r}")
    rows = result.get("rows")
    if rows is None:
        raise LedgerReadError(f"{op} returned no 'rows' key: {result!r}")
    return {str(r["name"]): r for r in rows if r.get("name") is not None}


def _insert_one(*, row: dict) -> None:
    """Forward one ``save_agent_pattern_row`` / ``save_agent_discipline_row`` admin op.

    No ``project_id`` key: neither op reads one (``backend/admin_exec/
    ledger.py`` — ``save_agent_pattern_row`` forwards exactly ``name``,
    ``body_slug``, ``content_hash``, ``purpose``, ``status``,
    ``baseline_hash``), and neither table has the column. Sending it was a
    scope knob that did nothing.
    """
    from yadgar.core.forward import _forward_admin  # noqa: PLC0415

    if row["page_type"] == "agent_pattern":
        op = "save_agent_pattern_row"
        payload = {
            "name": row["name"],
            "body_slug": row["body_slug"],
            "content_hash": row["content_hash"],
            "status": "active",
        }
    else:
        op = "save_agent_discipline_row"
        payload = {
            "name": row["name"],
            "body_slug": row["body_slug"],
            "content_hash": row["content_hash"],
        }
    _forward_admin(op, payload)


def _hash_mismatches(candidates: list[dict], existing: dict[str, dict[str, dict]]) -> list[dict]:
    """Already-present rows whose ``content_hash`` disagrees with the wiki body.

    Real operator signal, not decoration: "present" alone reads as done, while
    "present, and the row is pinned to different bytes than the page" is the
    exact desync ``check_page_row_desync`` reports. This script never repairs
    it — the row may legitimately be newer than the page.
    """
    out: list[dict] = []
    for row in candidates:
        ledger_row = existing.get(row["page_type"], {}).get(row["name"])
        if ledger_row is None:
            continue
        row_hash = str(ledger_row.get("content_hash") or "")
        if row_hash and row_hash != row["content_hash"]:
            out.append(
                {
                    "name": row["name"],
                    "body_slug": row["body_slug"],
                    "page_type": row["page_type"],
                    "page_hash": row["content_hash"][:16],
                    "row_hash": row_hash[:16],
                }
            )
    return out


def backfill(
    storage,
    *,
    apply_changes: bool,
    page_type: str = "both",
    limit: int = 0,
) -> dict:
    """Scan wiki, classify, INSERT — or report what would have been INSERTed.

    Returns the same shape the CLI prints. ``rows_failed`` only counts when
    ``apply_changes=True`` — on a dry run there are no failures because
    nothing runs.
    """
    types_in_scope = _page_types_in_scope(page_type)
    scan = _scan_wiki_pages(
        storage,
        page_types=types_in_scope,
        limit=limit,
    )
    existing_by_type = {t: _existing_ledger_rows(page_type=t) for t in types_in_scope}
    candidates, skips = _build_rows_for_apply(scan, page_type_filter=page_type)

    present = [r for r in candidates if r["name"] in existing_by_type.get(r["page_type"], {})]
    insertable = [
        r for r in candidates if r["name"] not in existing_by_type.get(r["page_type"], {})
    ]

    tallies: dict = {
        "scanned": len(scan),
        "rows_inserted": 0,
        "rows_already_present": len(present),
        "rows_failed": 0,
        **skips,
    }
    flagged: list[dict] = []
    content_hash_mismatches = _hash_mismatches(present, existing_by_type)

    if not apply_changes:
        logger.info(
            "DRY-RUN: scanned=%d candidates=%d already_present=%d skipped=%d mismatched=%d",
            tallies["scanned"],
            len(candidates),
            tallies["rows_already_present"],
            sum(skips.values()),
            len(content_hash_mismatches),
        )
        return {
            **tallies,
            "flagged": flagged,
            "content_hash_mismatches": content_hash_mismatches,
            "gate": _dry_run_gate(
                scanned=tallies["scanned"],
                insertable=len(insertable),
                already_present=tallies["rows_already_present"],
                skips=skips,
            ),
            "would_insert": len(insertable),
        }

    for row in insertable:
        try:
            _insert_one(row=row)
        except Exception as exc:  # noqa: BLE001
            tallies["rows_failed"] += 1
            flagged.append(
                {"slug": row["body_slug"], "page_type": row["page_type"], "error": str(exc)}
            )
            logger.warning(
                "insert failed: slug=%s page_type=%s err=%s",
                row["body_slug"],
                row["page_type"],
                exc,
            )
            continue
        tallies["rows_inserted"] += 1

    return {
        **tallies,
        "flagged": flagged,
        "content_hash_mismatches": content_hash_mismatches,
        "gate": _apply_gate(tallies, skips),
        "would_insert": len(insertable),
    }


def _apply_gate(tallies: dict, skips: dict[str, int]) -> dict:
    """``scanned`` reconciles against EVERY bucket, including the skips.

    The three-term identity the CLI docstring used to advertise
    (``inserted + failed + already_present == scanned``) is arithmetically
    incapable of holding whenever a single row was skipped, and the previous
    revision never computed any identity at all — it returned
    ``rows_failed == 0``, which is a different claim wearing the gate's name.
    """
    accounted = (
        tallies["rows_inserted"]
        + tallies["rows_failed"]
        + tallies["rows_already_present"]
        + sum(skips.values())
    )
    return {
        "exact_match": accounted == tallies["scanned"],
        "accounted": accounted,
        "scanned": tallies["scanned"],
    }


def _dry_run_gate(
    *,
    scanned: int,
    insertable: int,
    already_present: int,
    skips: dict[str, int],
) -> dict:
    """A dry run cannot satisfy the apply gate — say so instead of printing False.

    Nothing was written, so ``rows_inserted`` is 0 by construction and the
    apply identity necessarily fails. Reporting a bare ``exact_match: False``
    reads as "this backfill will not reconcile", which is a lie about a run
    that has not happened yet. ``would_reconcile`` is the real preview: it
    substitutes the would-be insert count and answers the question the
    operator is actually asking before typing ``--apply``.
    """
    accounted = insertable + already_present + sum(skips.values())
    return {
        "applicable": False,
        "reason": "dry run — nothing written, so the apply identity cannot hold",
        "would_reconcile": accounted == scanned,
        "accounted": accounted,
        "scanned": scanned,
    }


# ── CLI ──────────────────────────────────────────────────────────────────


def _print_report(result: dict, *, dry_run: bool, page_type: str) -> None:
    """Print a one-line summary + the per-bucket breakdown."""
    mode = "DRY RUN" if dry_run else "APPLY"
    skips = " ".join(f"{b}={result.get(b)}" for b in SKIP_BUCKETS)
    print(
        f"[{mode}] page_type={page_type} "
        f"scanned={result.get('scanned')} "
        f"inserted={result.get('rows_inserted')} "
        f"would_insert={result.get('would_insert')} "
        f"already_present={result.get('rows_already_present')} "
        f"failed={result.get('rows_failed')} "
        f"{skips} "
        f"content_hash_mismatches={len(result.get('content_hash_mismatches') or [])} "
        f"gate={result.get('gate')}",
        file=sys.stderr,
    )
    for entry in result.get("flagged") or []:
        print(
            f"  FLAGGED: {entry.get('slug')} ({entry.get('page_type')}): {entry.get('error')}",
            file=sys.stderr,
        )
    for entry in result.get("content_hash_mismatches") or []:
        print(
            f"  HASH MISMATCH: {entry.get('body_slug')} "
            f"page={entry.get('page_hash')} row={entry.get('row_hash')}",
            file=sys.stderr,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__ or "")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the rows. Without this the script is a read-only dry run.",
    )
    parser.add_argument(
        "--page-type",
        default="both",
        choices=("agent_pattern", "agent_discipline", "both"),
        help=(
            "Which ledger table to backfill (default: both). NOTE: these tables "
            "have no project_id column and carry UNIQUE(name) install-wide, so "
            "there is no project scope to choose — one project's backfill "
            "changes every project's discovery surface."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help=(
            "Per-page-type cap on wiki pages scanned (0 = no cap). The reader "
            "orders by updated_at DESC, so this is 'the N most recently updated', "
            "not a stable window — use it to keep a dry run cheap, not to page."
        ),
    )
    parser.add_argument("--db-url", default=None, help="Override YADGAR_DB_URL.")
    args = parser.parse_args(argv)

    os.environ.setdefault("YADGAR_ALLOW_ROOT", "1")
    os.environ.setdefault("YADGAR_DB_USER", "root")
    os.environ.setdefault("YADGAR_DB_PASS", "root")
    if args.db_url:
        os.environ["YADGAR_DB_URL"] = args.db_url

    from yadgar._shared.storage import StorageEngine  # noqa: PLC0415

    db_path = os.environ.get("YADGAR_DB_PATH", "~/.yadgar/surreal_db")
    try:
        storage = StorageEngine(db_path)
    except Exception as exc:  # noqa: BLE001
        logger.error("cannot open storage: %s", exc)
        return 2

    try:
        result = backfill(
            storage,
            apply_changes=args.apply,
            page_type=args.page_type,
            limit=args.limit,
        )
    except LedgerReadError as exc:
        # Fatal, never a partial run: without the already-present set every
        # page reads as absent and the apply path would upsert the corpus.
        logger.error("ledger read failed, refusing to continue: %s", exc)
        return 2
    finally:
        try:
            storage.close()
        except Exception:  # noqa: BLE001
            pass

    _print_report(result, dry_run=not args.apply, page_type=args.page_type)
    print(json.dumps(result, default=str))
    return 1 if (args.apply and result.get("rows_failed")) else 0


if __name__ == "__main__":
    sys.exit(main())
