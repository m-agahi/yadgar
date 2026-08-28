#!/usr/bin/env python3
"""Bug-bag-2 train 2026-08-23 — DATA-ONLY repair for task 196.

Repair the 64 migrated ADR body pages that contain HTML entities
(``&amp;`` / ``&lt;`` / ``&gt;`` / ``&quot;`` / ``&#39;`` / ``&nbsp;``) in the
prose. Yadgar escapes NOTHING on the write path (proven — see the wiki-store
CRUD), so the entities were emitted by the upstream tool that created the
pages (migrated from another ADR tool, or by a model in ``adr_add`` args).

Yadgar's markdown renderer treats ``&amp;`` as a literal ``&amp;`` rather
than ``&``, so the prose is unreadable. There is no code fix — yadgar is
correct. The repair is a one-shot data unescape pass over the affected body
pages.

DRY-RUN BY DEFAULT — ``--apply`` is required to write anything. The script
goes through ``update_wiki_page`` (the same surface every other editor uses)
so each repair mints a normal version row + bumps ``updated_at``. Embeddings
and links are re-derived by the normal write path.

IDEMPOTENT — a second run reports 0 changes. Each page is scanned for any of
the five entities; if none remain it is skipped.

Usage:
    uv run scripts/unescape_adr_bodies.py                  # dry run (default)
    uv run scripts/unescape_adr_bodies.py --verbose        # per-page diff
    uv run scripts/unescape_adr_bodies.py --apply          # write
    uv run scripts/unescape_adr_bodies.py --slug-prefix m-agahi_yadgar_

Exit codes:
  0  completed (dry run, or apply succeeded)
  1  one or more pages failed to repair
  2  fatal error (DB unreachable, bad args)
"""

from __future__ import annotations

import argparse
import html
import logging
import os
import sys
from pathlib import Path

# Suppress the OTLP exporter BEFORE any yadgar import (same guard as
# scripts/rederive_wiki_links.py).
os.environ.setdefault("YADGAR_OTLP_ENDPOINT", "")

_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

logging.basicConfig(
    level=logging.INFO,
    format='{"ts": "%(asctime)s", "level": "%(levelname)s", "event": "%(message)s"}',
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("unescape_adr_bodies")

#: ``&amp;`` MUST be unescaped FIRST so a literal ``&amp;amp;`` (which is the
#: escape of an already-escaped ampersand) does not collapse to ``&amp;`` on
#: the second pass — that is the only entity where ``unescape`` is non-idempotent.
#: ``html.unescape`` does the right thing in a single pass for the rest.
_ENTITY_PATTERN = __import__("re").compile(
    r"&(amp|lt|gt|quot|apos|#39|nbsp);",
    __import__("re").IGNORECASE,
)


def _count_entities(text: str) -> int:
    """Count distinct occurrences of any of the five entities in ``text``.

    ``len(re.findall(...))`` matches the substrings rather than character ranges,
    so ``&amp;amp;`` counts as 2 (one ``&amp;`` inside an already-escaped
    sequence) — that is fine; we only need a nonzero/nonzero tally to decide
    whether a page is dirty.
    """
    return len(_ENTITY_PATTERN.findall(text))


def unescape_body(text: str) -> str:
    """Single-pass ``html.unescape`` — sufficient for the five entities above.

    Order matters: ``html.unescape`` handles ``&amp;`` first internally
    (CPython impl walks the entity table in a fixed order), so a literal
    ``&amp;amp;`` is correctly resolved to ``&amp;`` once. A naive
    ``text.replace("&amp;", "&").replace("&lt;", "<")`` chain would break
    that case (``&amp;amp;`` → ``&amp;`` → ``&``).
    """
    return html.unescape(text)


def repair(
    storage,
    *,
    apply_changes: bool,
    slug_prefix: str | None = None,
    verbose: bool = False,
) -> dict[str, int]:
    """Unescape HTML entities on dirty wiki pages. Returns a tally.

    The tally carries a residue counter so a dry run answers the question that
    actually matters: how many pages are dirty, and how many entities in total
    would the repair collapse? Anything still dirty after the run is a
    REGRESSION — a non-zero ``residue_after`` on ``--apply`` means the repair
    is incomplete and the operator should investigate before the script is
    treated as done.
    """
    pages = storage.list_wiki_pages(slug_prefix=slug_prefix)
    tally = {
        "scanned": 0,
        "dirty": 0,
        "entities_total": 0,
        "repaired": 0,
        "failed": 0,
        "skipped_no_content": 0,
        "residue_after": 0,
    }

    for page in pages:
        tally["scanned"] += 1
        slug = page.get("slug") or ""
        if not slug:
            logger.warning("skipping row with no slug: %r", page.get("id"))
            continue

        content = page.get("content")
        if content is None:
            tally["skipped_no_content"] += 1
            logger.warning("skipping %s: content is absent/None", slug)
            continue
        if not isinstance(content, str):
            logger.warning("skipping %s: content is not a string (%r)", slug, type(content))
            continue

        n = _count_entities(content)
        if n == 0:
            continue
        tally["dirty"] += 1
        tally["entities_total"] += n
        repaired = unescape_body(content)

        if verbose or not apply_changes:
            logger.info(
                "%s %s | entities=%d | sample_before=%r sample_after=%r",
                "[DRY-RUN] would unescape" if not apply_changes else "unescaping",
                slug,
                n,
                content[:120],
                repaired[:120],
            )

        if not apply_changes:
            continue

        try:
            storage.update_wiki_page(slug=slug, fields={"content": repaired})
            # Confirm the residue actually went to zero — ``html.unescape`` is
            # total on the five entities we care about, but ``update_wiki_page``
            # could trim, normalise whitespace, or otherwise rewrite content.
            residue = _count_entities(repaired)
            tally["residue_after"] += residue
            if residue:
                logger.warning("%s: %d entities remain after repair", slug, residue)
            tally["repaired"] += 1
        except Exception as exc:  # BLE001-KEEP: per-page isolation: update_wiki_page reaches storage, whose failures share no common base, and one unrepairable page must not abort the sweep
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
        "--slug-prefix",
        default=None,
        help="Only pages whose slug starts with this (e.g. 'm-agahi_yadgar_').",
    )
    parser.add_argument("--db-url", default=None, help="Override YADGAR_DB_URL.")
    parser.add_argument("--verbose", action="store_true", help="Log every dirty page.")
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
    except Exception as exc:  # BLE001-KEEP: CLI top-level: StorageEngine() reaches config, filesystem and DB connect, which raise with no common base; the contract is exit-2-with-a-message
        logger.error("cannot open storage: %s", exc)
        return 2

    try:
        tally = repair(
            storage,
            apply_changes=args.apply,
            slug_prefix=args.slug_prefix,
            verbose=args.verbose,
        )
    finally:
        try:
            storage.close()
        except Exception:  # BLE001-KEEP: close() in finally: a teardown failure must not replace the real exit status of the sweep above
            pass

    logger.info(
        "%s scanned=%d dirty=%d entities_total=%d repaired=%d failed=%d "
        "skipped_no_content=%d residue_after=%d",
        "APPLIED" if args.apply else "DRY-RUN",
        tally["scanned"],
        tally["dirty"],
        tally["entities_total"],
        tally["repaired"],
        tally["failed"],
        tally["skipped_no_content"],
        tally["residue_after"],
    )
    if not args.apply and tally["dirty"]:
        logger.info(
            "re-run with --apply to write these repairs (expect %d page(s), %d entity collapse(s))",
            tally["dirty"],
            tally["entities_total"],
        )
    if args.apply and tally["residue_after"]:
        logger.error(
            "residue_after=%d — %d entity(s) survived the repair. "
            "Investigate before declaring the run done.",
            tally["residue_after"],
            tally["residue_after"],
        )

    return 1 if tally["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
