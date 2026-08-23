#!/usr/bin/env python3
"""Bug-bag-2 train 2026-08-23 — agent_pattern/agent_discipline ledger backfill.

Walks ``wiki_page`` rows tagged ``agent_pattern`` (and, optionally,
``agent_discipline``) and inserts a row into the corresponding ``agent_pattern``
(resp. ``agent_discipline``) SQL ledger table for every wiki page that does
NOT already have one. Idempotent.

Tasks 200 / 268 / 90. The SQL TOC + the discovery surface read ``agent_pattern``,
and the discovery surface's absence-of-row failure mode is silent: every
``agent_dispatch_prelude`` call missed every wiki-canonicalised pattern that
was never mirrored into the ledger. Until this car ran, the gap was invisible
because nothing surfaced the count. The seed script (``seed_agent_prompts``)
seeds the starter library; this script picks up the wiki-canonicalised
material those starters miss — patterns added via ``agent_prompt_save`` after
the seed ran, or material a migration deposited directly into wiki_page.

WHAT IT SCANS
=============

``wiki_page.page_type IN ('agent_pattern', 'agent_discipline')`` for a project.
Both page types share the same body-page model; only the ledger destination
differs. Pages outside those page_types are SKIPPED — discipline prose
attached to a pattern page, or any other wiki content, never becomes a ledger
row through this script.

DRY-RUN BY DEFAULT — ``--apply`` is required to write anything.

IDEMPOTENT — a second run reports 0 inserted rows. Each wiki page is keyed by
its ``slug`` against the ledger row's ``name`` (the convention the core
``agent_prompt_save`` writes — slug == ``agent-prompt-<pattern>``, name ==
``<pattern>``, see ``yadgar/core/server/tools/agent_prompts.py``).

OUT OF SCOPE
============

* ``agent_pattern_composes`` edge rows. The script seeds ``agent_pattern`` /
  ``agent_discipline`` rows only; the composition edges (which disciplines a
  pattern composes) come from a later car.
* ``baseline_hash`` derivation. The core write path derives
  ``baseline_hash`` on the first write; this backfill leaves it NULL and the
  operator's first pattern mutation will set it (the schema column is
  nullable).
* Cross-project migration. One project per run; pass ``--project-id``.

Usage:
    uv run scripts/backfill_agent_pattern_from_wiki.py            # dry run
    uv run scripts/backfill_agent_pattern_from_wiki.py --apply    # write
    uv run scripts/backfill_agent_pattern_from_wiki.py --page-type agent_discipline
    uv run scripts/backfill_agent_pattern_from_wiki.py --project-id m-agahi/yadgar
    uv run scripts/backfill_agent_pattern_from_wiki.py --limit 50 --from-slug agent-prompt-m

Exit codes:
  0  completed (dry run, or apply succeeded)
  1  one or more inserts failed
  2  fatal error (DB unreachable, bad args)
"""

from __future__ import annotations

import argparse
import hashlib
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


# ── Pure helpers (unit-test targets) ─────────────────────────────────────


def _slug_to_name(slug: str) -> str:
    """Derive the ledger ``name`` from a wiki ``agent-prompt-<name>`` slug.

    Mirrors the convention in ``yadgar/core/server/tools/agent_prompts.py``
    where the slug is ``agent-prompt-<pattern>`` and the ledger ``name`` is
    ``<pattern>``. Idempotent and reversible: ``_name_to_slug(_slug_to_name(s)) == s``
    for every slug that matches the convention.

    Pages outside the convention (e.g. ``agent-prompt-<name>-v2``,
    discipline pages with non-standard slugs) pass through unchanged; the
    backfill then seeds them under that literal name, which is the operator's
    authority over their own slug scheme.
    """
    if slug.startswith("agent-prompt-"):
        return slug[len("agent-prompt-") :]
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
    (skip). Empty page_type defaults to ``agent_pattern`` because the
    ``agent_prompt_save`` write path tagged pages as ``agent_pattern``
    first; a row without page_type most likely predates the type split.

    OUT OF SCOPE for this car: a slug-based fallback (e.g. a discipline
    whose slug starts with ``agent-discipline-``). The discipline write path
    sets ``page_type='agent_discipline'`` explicitly, so a missing type
    means "we don't know" — not "default to discipline".
    """
    if page_type in {"agent_pattern", "agent_discipline"}:
        return page_type
    if page_type in (None, "", "agent_prompt"):  # legacy type predates the split
        return "agent_pattern"
    return ""


def _build_rows_for_apply(
    wiki_rows: list[dict],
    *,
    page_type_filter: str,
) -> list[dict]:
    """Filter ``wiki_rows`` to the candidate set the apply path will INSERT.

    Idempotency: callers pass in wiki rows already known-absent from the
    ledger. The function does not query the ledger; it only filters by
    ``page_type`` so the apply path is one read + one INSERT per page.

    Each returned dict carries the keys the apply path needs:
    ``name``, ``body_slug``, ``content``, ``content_hash``, ``page_type``.
    Pages whose page_type is outside ``page_type_filter`` are dropped.
    """
    out: list[dict] = []
    for row in wiki_rows:
        page_type = _classify_page_type(
            row.get("slug", ""),
            row.get("page_type"),
        )
        if not page_type:
            continue
        if page_type_filter != "both" and page_type != page_type_filter:
            continue
        content = row.get("content")
        if not isinstance(content, str):
            continue
        slug = row.get("slug") or ""
        if not slug:
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
    return out


# ── Side-effecting apply path ─────────────────────────────────────────────


def _scan_wiki_pages(
    storage,
    *,
    project_id: str | None,
    page_type_filter: str,
    from_slug: str,
    limit: int,
) -> list[dict]:
    """Read candidate wiki pages for one project from the wiki store.

    Returns a list of dicts with ``slug``, ``content``, ``page_type``. The
    wiki-side schema is whatever ``storage.list_wiki_pages`` /
    ``storage.get_wiki_pages_for_project`` returns today — pure CRU on
    SurrealDB via the engine wrapper.
    """
    # Pin the projection we need to keep the script cheap on a corpus with
    # thousands of non-agent wiki pages.
    pages = storage.list_wiki_pages(
        project_id=project_id,
        page_type=page_type_filter,
        from_slug=from_slug,
        limit=limit,
    )
    return list(pages or [])


def _existing_ledger_names(
    storage,
    *,
    page_type: str,
    project_id: str | None,
) -> set[str]:
    """Read the set of ``name`` values already present on the ledger side.

    Drives idempotency: the apply path skips any wiki page whose derived
    ``name`` is in this set. The engine exposes ``list_agent_prompt_rows`` /
    ``list_agent_discipline_rows`` (both added/ensured in C5); the result is
    an in-memory ``{name, ...}`` set keyed by ``name``.

    Cross-project rows are returned too (the SQL ledger tables are not
    project-scoped today — ``agent_pattern`` has no ``project_id`` column
    in migration 002). A wiki page that already has a row under ANY project
    counts as "already present" and is skipped; the operator's authority over
    the wiki page is what produced the conflict and only an explicit
    operator can resolve it.
    """
    if page_type == "agent_pattern":
        rows = storage.list_agent_prompt_rows()
    elif page_type == "agent_discipline":
        rows = storage.list_agent_discipline_rows()
    else:
        return set()
    return {str(r.get("name", "")) for r in (rows or []) if r.get("name") is not None}


def _insert_one(
    storage,
    *,
    row: dict,
    project_id: str | None,
) -> None:
    """Forward one ``save_agent_pattern_row`` / ``save_agent_discipline_row`` admin op.

    ``save_agent_pattern_row`` is a registered admin op (engine #2 car I);
    it writes via the ledger wrapper which is the same path core
    ``agent_prompt_save`` calls. The discipline side is symmetric.
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
    if project_id:
        payload["project_id"] = project_id
    _forward_admin(op, payload)


def backfill(
    storage,
    *,
    apply_changes: bool,
    project_id: str | None = None,
    page_type: str = "both",
    limit: int = 0,
    from_slug: str = "",
) -> dict:
    """Scan wiki, classify, INSERT — or report what would have been INSERTed.

    Returns the same shape the CLI prints. ``rows_failed`` only counts when
    ``apply_changes=True`` — on a dry run there are no failures because
    nothing runs.
    """
    scan = _scan_wiki_pages(
        storage,
        project_id=project_id,
        page_type_filter=page_type,
        from_slug=from_slug,
        limit=limit,
    )
    if page_type == "both":
        types_in_scope: tuple[str, ...] = ("agent_pattern", "agent_discipline")
    else:
        types_in_scope = (page_type,)
    existing_by_type = {
        t: _existing_ledger_names(storage, page_type=t, project_id=project_id)
        for t in types_in_scope
    }
    candidates = _build_rows_for_apply(scan, page_type_filter=page_type)
    # Already-present filter is per-page-type because the two tables share
    # the wiki slug space but live in distinct SQL tables.
    present_count = sum(
        1 for r in candidates if r["name"] in existing_by_type.get(r["page_type"], set())
    )
    insertable = [
        r for r in candidates if r["name"] not in existing_by_type.get(r["page_type"], set())
    ]

    tallies = {
        "scanned": len(scan),
        "rows_inserted": 0,
        "rows_already_present": present_count,
        "rows_failed": 0,
        "rows_skipped_unknown_page_type": sum(
            1 for r in scan if not _classify_page_type(r.get("slug", ""), r.get("page_type"))
        ),
    }
    flagged: list[dict] = []
    content_hash_mismatches: list[dict] = []
    next_id_basis = 0

    if not apply_changes:
        logger.info(
            "DRY-RUN: scanned=%d candidates=%d already_present=%d skipped_unknown=%d",
            tallies["scanned"],
            len(candidates),
            tallies["rows_already_present"],
            tallies["rows_skipped_unknown_page_type"],
        )
        return {
            **tallies,
            "flagged": flagged,
            "content_hash_mismatches": content_hash_mismatches,
            "next_id_basis": next_id_basis,
            "gate": {"exact_match": False},
            "resume_after_slug": insertable[-1]["body_slug"] if insertable else "",
        }

    last_slug = ""
    for row in insertable:
        try:
            _insert_one(storage, row=row, project_id=project_id)
        except Exception as exc:
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
        last_slug = row["body_slug"]

    return {
        **tallies,
        "flagged": flagged,
        "content_hash_mismatches": content_hash_mismatches,
        "next_id_basis": next_id_basis,
        "gate": {"exact_match": tallies["rows_failed"] == 0},
        "resume_after_slug": last_slug,
    }


# ── CLI ──────────────────────────────────────────────────────────────────


def _print_report(result: dict, *, dry_run: bool, page_type: str) -> None:
    """Print a one-line summary + per-page-type breakdown."""
    mode = "DRY RUN" if dry_run else "APPLY"
    print(
        f"[{mode}] page_type={page_type} "
        f"scanned={result.get('scanned')} "
        f"inserted={result.get('rows_inserted')} "
        f"already_present={result.get('rows_already_present')} "
        f"failed={result.get('rows_failed')} "
        f"skipped_unknown_page_type={result.get('rows_skipped_unknown_page_type')} "
        f"gate={result.get('gate')}",
        file=sys.stderr,
    )
    for entry in result.get("flagged") or []:
        print(
            f"  FLAGGED: {entry.get('slug')} ({entry.get('page_type')}): {entry.get('error')}",
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
        "--project-id",
        default=None,
        help="Optional project scope (matches wiki_page.project_id).",
    )
    parser.add_argument(
        "--page-type",
        default="both",
        choices=("agent_pattern", "agent_discipline", "both"),
        help="Which ledger table to backfill (default: both).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Cap on the number of wiki pages scanned (0 = no cap).",
    )
    parser.add_argument(
        "--from-slug",
        default="",
        help="Resume after this slug (for multi-run backfills over a large corpus).",
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
    except Exception as exc:
        logger.error("cannot open storage: %s", exc)
        return 2

    try:
        result = backfill(
            storage,
            apply_changes=args.apply,
            project_id=args.project_id,
            page_type=args.page_type,
            limit=args.limit,
            from_slug=args.from_slug,
        )
    finally:
        try:
            storage.close()
        except Exception:
            pass

    _print_report(result, dry_run=not args.apply, page_type=args.page_type)
    print(json.dumps(result, default=str))  # noqa: F821 — json is imported below
    return 1 if (args.apply and result.get("rows_failed")) else 0


# json is imported late so the script's pure helpers can be unit-tested
# without requiring yadgar import-time side effects.
import json  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
