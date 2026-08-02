# SPDX-License-Identifier: Apache-2.0
"""Spine ledger seed — Car G one-shot admin ops.

Seeds the `adr` table from existing per-ADR wiki pages (D35b — pages
over index, because §1.5 proved the index can miss rows). The seed is
idempotent (re-running converges) and ships with an exact-equality
verification gate (D35c).

D35b: ONE-SHOT, not dual-write. Source of truth for the ADR seed is
the per-ADR PAGES, not the index. Cutover is a single atomic flip of
the read path.

D35c: EXACT equality on a stated predicate. `>=` is not a gate — the
2026-06-16 vacuum bug destroyed 3,622 memories because a partial
restore (1,484 of 3,622) passed a `>=` check.
"""

from __future__ import annotations

import logging
import re

from yadgar._shared.observability.observe import observe
from yadgar._shared.runtime.lifecycle import _get_storage

logger = logging.getLogger(__name__)

_ADR_SLUG_NUMBER_RE = re.compile(r"_adr-(\d+)$")


def _extract_adr_number(slug: str) -> int | None:
    """Extract the ADR number from a slug like 'm-agahi_yadgar_adr-0194'."""
    m = _ADR_SLUG_NUMBER_RE.search(slug or "")
    if m is None:
        return None
    num_str = m.group(1)
    if not num_str.isdigit():
        return None
    return int(num_str)


@observe(tier="boundary", metric="backend.admin.seed_adr_from_pages")
def seed_adr_from_pages(
    *,
    directory: str,
    project_id: str,
    dry_run: bool = False,
) -> dict:
    """Seed the `adr` table from existing per-ADR wiki pages.

    Args:
        directory: Absolute project path.
        project_id: Git-derived identity key (D13/D14).
        dry_run: If True, report candidates without writing.

    Returns:
        {seeded: N, skipped: M, dry_run: bool, candidates: K}
    """
    storage = _get_storage()

    # Read pages (source of truth per D35b).
    pages = storage.list_wiki_pages(
        directory=directory,
        slug_prefix="yadgar_adr-",
    )
    # Read existing rows.
    existing_rows = storage.list_adr_rows(project_id=project_id, limit=10000)
    existing_numbers = {r["number"] for r in existing_rows}

    candidates: list[dict] = []
    for page in pages:
        slug = page.get("slug", "")
        number = _extract_adr_number(slug)
        if number is None:
            continue
        candidates.append({"number": number, "slug": slug, "title": page.get("title", "")})

    if dry_run:
        return {
            "dry_run": True,
            "candidates": len(candidates),
            "seeded": 0,
            "skipped": 0,
        }

    seeded = 0
    skipped = 0
    for c in candidates:
        if c["number"] in existing_numbers:
            skipped += 1
            continue
        try:
            storage.create_adr_row(
                project_id=project_id,
                origin="yadgar",
                number=c["number"],
                title=c["title"],
                status="accepted",
                body_slug=c["slug"],
            )
            seeded += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("seed_adr failed number=%s: %s", c["number"], exc)
            skipped += 1

    return {
        "dry_run": False,
        "candidates": len(candidates),
        "seeded": seeded,
        "skipped": skipped,
    }


@observe(tier="boundary", metric="backend.admin.verify_adr_seed")
def verify_adr_seed(
    *,
    page_numbers: set[int],
    row_numbers: set[int],
) -> dict:
    """D35c verification gate: exact equality on {number set}.

    Args:
        page_numbers: Set of ADR numbers parsed from per-ADR page slugs.
        row_numbers: Set of ADR numbers from the seeded `adr` table.

    Returns:
        {passed: bool, missing_in_table: [...], extra_in_table: [...]}
    """
    missing = sorted(page_numbers - row_numbers)
    extra = sorted(row_numbers - page_numbers)
    return {
        "passed": not missing and not extra,
        "missing_in_table": missing,
        "extra_in_table": extra,
        "page_count": len(page_numbers),
        "row_count": len(row_numbers),
    }


def _collect_page_numbers(pages: list[dict]) -> set[int]:
    """Helper for the verification gate — extract numbers from page list."""
    numbers: set[int] = set()
    for page in pages:
        slug = page.get("slug", "")
        n = _extract_adr_number(slug)
        if n is not None:
            numbers.add(n)
    return numbers
