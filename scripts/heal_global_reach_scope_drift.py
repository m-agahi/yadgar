#!/usr/bin/env python3
"""Car W4 — heal the ``storage_scope="global"`` rows that predate the policy.

Repairs the corpus residue behind ledger task 50 ("Cross-project wiki scoping
drift — 9 legacy rows PLUS a live write-side half-heal").

WHAT IS ALREADY FIXED — this script repairs ROWS, not a mechanism:

  * write-side scope enforcement shipped 2026-07-22 (``c318ad7b``, ADR-0158).
    ``WikiStore._apply_storage_scope`` forces ``directory_context='global'`` AND
    adds the Car C7 reach tag for every page_type whose ``WikiPolicy`` declares
    ``storage_scope="global"`` — ``agent_pattern``, ``agent_discipline``,
    ``agent_index`` and legacy ``agent_prompt``.
  * the UPDATE half-heal (task 50's "G2") is closed. ``WikiStore.add``'s update
    branch carries ``directory_context`` in ``updates`` and merges the new tags,
    and the ``branch`` column the original finding turned on was dropped
    outright by ADR-0215 / migration 032. There is no longer a field a re-save
    leaves stale.

MEASURED 2026-08-19 on the live corpus (2523 wiki rows): exactly nine rows are
still drifted, and every one was CREATED between 2026-07-12 and 2026-07-21 —
i.e. before the write-side fix. Their ``updated_at`` of 2026-08-08T18:23 is the
project_id backfill's raw UPDATE, which does not go through ``WikiStore.add``
and so could not heal them. No row created after 2026-07-22 is drifted: the
mechanism holds and this is pure residue.

  8 × ``agent_pattern`` (quinyx/qwfm, quinyx/flux, quinyx/infrastructure)
  1 × ``model-tier-dispatch`` — see the G3 note below.

THE INVARIANT, expressed against the policy table rather than a hardcoded
``agent_prompt`` literal, so a future page class extends coverage for free::

    for any page_type whose WikiPolicy.storage_scope == "global",
    every wiki_page row of that type MUST have
        directory_context = 'global'  AND  'global' IN tags

G3 IS NOT CLOSED BY THIS SCRIPT. ``model-tier-dispatch`` carries
``page_type=None`` → ``DEFAULT_POLICY`` → ``storage_scope="project"``, so the
invariant above does not cover it and nothing ever will until a convention /
cross-project page class exists (``POLICY_BY_TYPE`` has no ``convention``
entry). It is reachable here only via the explicit ``--extra-slug`` escape
hatch, which is deliberately opt-in: healing it is an operator decision about a
page the type system does not classify.

Idempotent: a row already at ``'global'`` with the reach tag is skipped, so a
second run reports 0 repairs. DRY-RUN BY DEFAULT — ``--apply`` is required to
write anything.

No version rows are created. A scope stamp is a metadata repair, not a content
change; going through ``update_wiki_page`` would mint a version row and bump
``updated_at`` on every repaired page (same rationale as
``scripts/rederive_wiki_links.py``). It also bypasses the ``mutability``
write-gate, which is correct for an explicitly-invoked operator repair and is
why the script is dry-run by default.

Usage:
    uv run scripts/heal_global_reach_scope_drift.py                       # dry run
    uv run scripts/heal_global_reach_scope_drift.py --verbose             # per-row
    uv run scripts/heal_global_reach_scope_drift.py --apply               # write
    uv run scripts/heal_global_reach_scope_drift.py --extra-slug model-tier-dispatch
    uv run scripts/heal_global_reach_scope_drift.py --db-url http://localhost:8000

Exit codes:
  0  completed (dry run, or apply succeeded)
  1  one or more rows failed to repair
  2  fatal error (DB unreachable, bad args)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Any

# Suppress the OTLP exporter BEFORE any yadgar import — importing the package
# sets up tracing, and an unreachable collector makes the process hang at exit
# retrying exports (same guard as scripts/rederive_wiki_links.py).
os.environ.setdefault("YADGAR_OTLP_ENDPOINT", "")

_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

logging.basicConfig(
    level=logging.INFO,
    format='{"ts": "%(asctime)s", "level": "%(levelname)s", "event": "%(message)s"}',
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("heal_global_reach_scope_drift")


def _global_scoped_page_types() -> frozenset[str]:
    """Page types whose policy declares ``storage_scope="global"``.

    Read from ``POLICY_BY_TYPE`` rather than listed here, so the set tracks the
    policy table. An empty result means the policy table changed shape and the
    scan would be vacuous — the caller refuses rather than reporting "clean".
    """
    from yadgar._shared.wiki.policy import POLICY_BY_TYPE

    return frozenset(
        page_type
        for page_type, policy in POLICY_BY_TYPE.items()
        if getattr(policy, "storage_scope", None) == "global"
    )


def _is_drifted(row: dict[str, Any], reach_tag: str) -> bool:
    tags = row.get("tags") or []
    return row.get("directory_context") != "global" or reach_tag not in tags


def heal(
    storage,
    *,
    apply_changes: bool,
    extra_slugs: frozenset[str] = frozenset(),
    verbose: bool = False,
) -> dict[str, int]:
    """Scan every wiki row; repair the drifted global-scope ones. Returns a tally."""
    from yadgar._shared.storage.directory import GLOBAL_REACH_TAG

    types = _global_scoped_page_types()
    if not types:
        raise RuntimeError(
            "no page_type declares storage_scope='global' — the policy table "
            "changed shape and this scan would be vacuously clean (ADR-0080)"
        )
    logger.info("global-scope page types: %s", sorted(types))
    if extra_slugs:
        logger.info("extra slugs opted in by the operator: %s", sorted(extra_slugs))

    rows = storage._q(
        "SELECT id, slug, page_type, tags, directory_context, project_id FROM wiki_page"
    )
    tally = {
        "scanned": 0,
        "in_scope": 0,
        "drifted": 0,
        "repaired": 0,
        "failed": 0,
        "extra_slug_drifted": 0,
    }

    for row in rows:
        tally["scanned"] += 1
        slug = row.get("slug") or ""
        page_id = row.get("id")
        by_type = row.get("page_type") in types
        by_slug = slug in extra_slugs
        if not (by_type or by_slug):
            continue
        tally["in_scope"] += 1
        if not _is_drifted(row, GLOBAL_REACH_TAG):
            continue
        tally["drifted"] += 1
        if by_slug and not by_type:
            tally["extra_slug_drifted"] += 1

        tags = list(row.get("tags") or [])
        new_tags = tags if GLOBAL_REACH_TAG in tags else [*tags, GLOBAL_REACH_TAG]

        if verbose or not apply_changes:
            logger.info(
                "%s %s | page_type=%r project_id=%r directory_context=%r -> 'global' "
                "| tags %r -> %r",
                "[DRY-RUN] would heal" if not apply_changes else "healing",
                slug,
                row.get("page_type"),
                row.get("project_id"),
                row.get("directory_context"),
                tags,
                new_tags,
            )

        if not apply_changes:
            continue
        if page_id is None:
            tally["failed"] += 1
            logger.error("cannot heal %s: row has no id", slug)
            continue

        try:
            # Direct UPDATE: no version row, no updated_at bump, bypasses the
            # mutability gate (see module docstring). ``project_id`` is NOT
            # touched — ownership and reach are different axes (§1.4): a library
            # page found everywhere is still FROM the repo that wrote it.
            storage._q(
                "UPDATE type::record('wiki_page', $pid) "
                "SET directory_context = 'global', tags = $tags",
                {"pid": int(page_id), "tags": new_tags},
            )
            tally["repaired"] += 1
        except Exception as exc:  # noqa: BLE001 — per-row failure must not abort the sweep
            tally["failed"] += 1
            logger.error("failed to heal %s: %s", slug, exc)

    return tally


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__ or "")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the repairs. Without this the script is a read-only dry run.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Explicit no-op flag; dry run is already the default.",
    )
    parser.add_argument(
        "--extra-slug",
        action="append",
        default=[],
        metavar="SLUG",
        help=(
            "Heal this slug too, regardless of page_type. The G3 escape hatch for "
            "convention pages the policy table does not classify (e.g. "
            "model-tier-dispatch). Repeatable."
        ),
    )
    parser.add_argument("--db-url", default=None, help="Override YADGAR_DB_URL.")
    parser.add_argument("--verbose", action="store_true", help="Log every healed row.")
    args = parser.parse_args(argv)

    if args.apply and args.dry_run:
        logger.error("--apply and --dry-run are mutually exclusive")
        return 2

    os.environ.setdefault("YADGAR_ALLOW_ROOT", "1")
    os.environ.setdefault("YADGAR_DB_USER", "root")
    os.environ.setdefault("YADGAR_DB_PASS", "root")
    if args.db_url:
        os.environ["YADGAR_DB_URL"] = args.db_url

    from yadgar._shared.storage import StorageEngine

    db_path = os.environ.get("YADGAR_DB_PATH", "~/.yadgar/surreal_db")
    try:
        storage = StorageEngine(db_path)
    except Exception as exc:  # noqa: BLE001 — surfaced as exit code 2
        logger.error("cannot open storage: %s", exc)
        return 2

    try:
        tally = heal(
            storage,
            apply_changes=args.apply,
            extra_slugs=frozenset(args.extra_slug),
            verbose=args.verbose,
        )
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 2
    finally:
        try:
            storage.close()
        except Exception:  # noqa: BLE001 — close is best-effort
            pass

    logger.info(
        "%s scanned=%d in_scope=%d drifted=%d repaired=%d failed=%d extra_slug_drifted=%d",
        "APPLIED" if args.apply else "DRY-RUN",
        tally["scanned"],
        tally["in_scope"],
        tally["drifted"],
        tally["repaired"],
        tally["failed"],
        tally["extra_slug_drifted"],
    )
    if not args.apply and tally["drifted"]:
        logger.info("re-run with --apply to write these repairs")

    return 1 if tally["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
