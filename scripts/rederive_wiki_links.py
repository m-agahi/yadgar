#!/usr/bin/env python3
"""Car W3 — bulk re-derive ``wiki_page.links`` + ``wiki_crossref`` from page content.

Repairs the corpus damage from ledger tasks 218 and 216:

  * 218 — ``WikiStore._extract_wikilinks`` ran every ``[[...]]`` body through
    ``slugify``, which collapses ``_`` to ``-``.  Post-ADR-0211 canonical slugs
    (``{project with / -> _}_{name}``) therefore landed corrupted in BOTH
    surfaces: ``[[m-agahi_yadgar_adr-0253]]`` stored as
    ``m-agahi-yadgar-adr-0253``, matching no page.  ``get_wiki_backlinks``
    is an exact ``to_slug`` match, so those backlinks never resolved.
  * 216 — the surgical edit primitives rewrote content without re-deriving
    links, so an added or (worse) removed link never reached ``links``.

Both are fixed forward in ``yadgar/_shared/wiki/store.py``, but a forward fix
does not repair rows already written.  This script re-derives from the page's
CURRENT content — the content is the source of truth; ``links`` and
``wiki_crossref`` are both denormalisations of it.

Idempotent: it compares derived-vs-stored per page and touches only real
diffs, so a second run reports 0 changes.  DRY-RUN BY DEFAULT — ``--apply`` is
required to write anything.

No version rows are created.  A derivation repair is not a content change, the
same rationale ``set_wiki_page_embedding`` already documents for embeddings;
going through ``update_wiki_page`` would mint a spurious version + bump
``updated_at`` on every page in the corpus.

Usage:
    uv run scripts/rederive_wiki_links.py                    # dry run (default)
    uv run scripts/rederive_wiki_links.py --verbose          # dry run, per-page diff
    uv run scripts/rederive_wiki_links.py --apply            # write
    uv run scripts/rederive_wiki_links.py --slug-prefix m-agahi_yadgar_
    uv run scripts/rederive_wiki_links.py --db-url http://localhost:8000

Exit codes:
  0  completed (dry run, or apply succeeded)
  1  one or more pages failed to repair
  2  fatal error (DB unreachable, bad args)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# Suppress the OTLP exporter BEFORE any yadgar import — importing the package
# sets up tracing, and an unreachable collector makes the process hang at exit
# retrying exports (same guard as scripts/scan_db_for_secrets.py).
os.environ.setdefault("YADGAR_OTLP_ENDPOINT", "")

_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

logging.basicConfig(
    level=logging.INFO,
    format='{"ts": "%(asctime)s", "level": "%(levelname)s", "event": "%(message)s"}',
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("rederive_wiki_links")


def _stored_crossrefs(storage, from_slug: str) -> list[str]:
    rows = storage._q(
        "SELECT to_slug FROM wiki_crossref WHERE from_slug = $s",
        {"s": from_slug},
    )
    return sorted(r["to_slug"] for r in rows if r.get("to_slug"))


def rederive(
    storage,
    wiki,
    *,
    apply_changes: bool,
    slug_prefix: str | None = None,
    verbose: bool = False,
) -> dict[str, int]:
    """Re-derive links + crossrefs for every wiki page.  Returns a tally."""
    pages = storage.list_wiki_pages(slug_prefix=slug_prefix)
    tally = {
        "scanned": 0,
        "links_stale": 0,
        "crossrefs_stale": 0,
        "changed": 0,
        "repaired": 0,
        "failed": 0,
    }

    for page in pages:
        tally["scanned"] += 1
        slug = page.get("slug") or ""
        page_id = page.get("id")
        if not slug or page_id is None:
            logger.warning("skipping row with no slug/id: %r", page.get("id"))
            continue

        derived = wiki._extract_wikilinks(page.get("content") or "")
        stored_links = list(page.get("links") or [])

        links_stale = sorted(stored_links) != sorted(derived)
        crossrefs_stale = _stored_crossrefs(storage, slug) != sorted(derived)

        if links_stale:
            tally["links_stale"] += 1
        if crossrefs_stale:
            tally["crossrefs_stale"] += 1
        if not (links_stale or crossrefs_stale):
            continue
        tally["changed"] += 1

        if verbose or not apply_changes:
            logger.info(
                "%s %s | links_stale=%s crossrefs_stale=%s | stored=%r derived=%r",
                "[DRY-RUN] would repair" if not apply_changes else "repairing",
                slug,
                links_stale,
                crossrefs_stale,
                sorted(stored_links),
                derived,
            )

        if not apply_changes:
            continue

        try:
            # Direct UPDATE: no version row, no updated_at bump (see module docstring).
            storage._q(
                "UPDATE type::record('wiki_page', $pid) SET links = $links",
                {"pid": int(page_id), "links": derived},
            )
            storage.replace_wiki_crossrefs(slug, derived)
            tally["repaired"] += 1
        except Exception as exc:
            tally["failed"] += 1
            logger.error("failed to repair %s: %s", slug, exc)

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
        "--slug-prefix", default=None, help="Only pages whose slug starts with this."
    )
    parser.add_argument("--db-url", default=None, help="Override YADGAR_DB_URL.")
    parser.add_argument("--verbose", action="store_true", help="Log every repaired page.")
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
    from yadgar._shared.wiki import WikiStore

    db_path = os.environ.get("YADGAR_DB_PATH", "~/.yadgar/surreal_db")
    try:
        storage = StorageEngine(db_path)
    except Exception as exc:
        logger.error("cannot open storage: %s", exc)
        return 2

    # Embeddings are unused — _extract_wikilinks is pure.
    wiki = WikiStore(storage, None)

    try:
        tally = rederive(
            storage,
            wiki,
            apply_changes=args.apply,
            slug_prefix=args.slug_prefix,
            verbose=args.verbose,
        )
    finally:
        try:
            storage.close()
        except Exception:
            pass

    logger.info(
        "%s scanned=%d changed=%d (links_stale=%d crossrefs_stale=%d) repaired=%d failed=%d",
        "APPLIED" if args.apply else "DRY-RUN",
        tally["scanned"],
        tally["changed"],
        tally["links_stale"],
        tally["crossrefs_stale"],
        tally["repaired"],
        tally["failed"],
    )
    if not args.apply and tally["changed"]:
        logger.info("re-run with --apply to write these repairs")

    return 1 if tally["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
