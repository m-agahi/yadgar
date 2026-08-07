# ruff: noqa: PLR0913  — _build_adr_body intentionally has 11 params (ADR schema
#   has 10 mandatory content fields + adr_id). PERMANENT — see .complexity-allowlist.json.
"""ADR rendering, tag helpers, supersede handling, and write-path assembly.

Extracted from adr.py (car/adr-split). The public surface of this module
is re-exported from yadgar.core.server.tools.adr for backward compatibility.
"""

from __future__ import annotations

import re

from yadgar._shared.contracts.models import ADR
from yadgar._shared.observability.observe import observe

# Imported lazily at call-time to match the pattern in other tool modules.
from yadgar.core.server.tools.admin_other import wiki_update
from yadgar.core.server.tools.adr_index import adr_page_slug
from yadgar.core.server.tools.wiki import _resolve_page_id_by_slug

# ── Validation constants ───────────────────────────────────────────────────────

# Valid ADR status values.
_VALID_STATUSES: frozenset[str] = frozenset(
    {"open", "accepted", "superseded", "rejected", "deprecated"}
)

# Required caller-supplied content fields (directory is handled separately).
_REQUIRED_FIELDS: tuple[str, ...] = (
    "title",
    "status",
    "date",
    "context",
    "decision",
    "rationale",
    "alternatives",
    "consequences",
    "revisit_trigger",
    "supersedes",
)


# ── Body builder ───────────────────────────────────────────────────────────────


def _build_adr_body(
    adr_id: str,
    title: str,
    status: str,
    date: str,
    context: str,
    decision: str,
    rationale: str,
    alternatives: str,
    consequences: str,
    revisit_trigger: str,
    supersedes: str,
) -> str:
    """Return the per-ADR page CONTENT (H1 + Context/Decision/Consequences + bullets).

    The page_type `adr` requires sections Context / Decision / Consequences (for
    wiki_lint). The 9 flat bullets carry the full structured record; the three
    required ## sections satisfy the page_type template.
    """
    record = ADR(
        adr_id=adr_id,
        title=title,
        status=status,
        date=date,
        context=context,
        decision=decision,
        rationale=rationale,
        alternatives=alternatives,
        consequences=consequences,
        revisit_trigger=revisit_trigger,
        supersedes=supersedes,
    )
    bullets = record.to_markdown_body()
    return (
        f"# {adr_id}: {title}\n\n"
        f"{bullets}\n"
        f"## Context\n\n{context}\n\n"
        f"## Decision\n\n{decision}\n\n"
        f"## Consequences\n\n{consequences}\n"
    )


def _adr_tags(adr_id: str, status: str) -> list[str]:
    """Tags for a per-ADR page. adr_id is 'ADR-NNNN'."""
    nnnn = adr_id.split("-")[1]
    return ["adr", "decisions", f"adr-status:{status}", f"adr-{nnnn}"]


# ── Supersede handling ─────────────────────────────────────────────────────────


@observe(tier="stage", metric="tools.adr._parse_supersedes")
def _parse_supersedes(supersedes: str) -> list[str]:
    """Parse a 'supersedes' field into a list of 'ADR-NNNN' ids ([] for none)."""
    if not supersedes or supersedes.strip().lower() == "none":
        return []
    ids = re.findall(r"ADR-(\d{4})", supersedes)
    return [f"ADR-{n}" for n in ids]


@observe(tier="stage", metric="tools.adr._flip_superseded_target")
def _flip_superseded_target(resolved: str, target_id: str, new_adr_id: str) -> None:
    """Flip a superseded ADR page's status tag to 'superseded' (best-effort).

    Reads the target page canonically, replaces its adr-status:* tag with
    adr-status:superseded, and records the superseding id in an adr-superseded-by
    tag. Never raises — a missing target must not abort the new ADR write.
    """
    try:
        slug = adr_page_slug(resolved, target_id)
        page_id, page = _resolve_page_id_by_slug(slug, directory=resolved)
        if page_id is None or page is None:
            return
        tags = list(page.get("tags") or [])
        tags = [t for t in tags if not t.startswith("adr-status:")]
        tags.append("adr-status:superseded")
        nnnn = new_adr_id.split("-")[1]
        sb_tag = f"adr-superseded-by:{nnnn}"
        if sb_tag not in tags:
            tags.append(sb_tag)
        wiki_update(page_id, {"tags": tags})
    except Exception:  # noqa: BLE001 — supersede patch is best-effort
        return


# ── adr_add helpers (write-path assembly) ─────────────────────────────────────


def _canonical_adr_payload(
    slug: str,
    content: str,
    category: str,
    tags: list[str],
    directory: str,
    *,
    replace_slug: str | None = None,
) -> dict:
    """Build a canonical wiki_add payload for the ADR write path.

    ``title`` == ``slug`` so ``_slugify(title)`` yields the deterministic slug.
    ``force=True`` bypasses the drainer sim gate (canonical ADR/index pages are
    legitimately near-duplicate). ``page_type="adr"`` satisfies the
    ``_wiki_write_canonical`` CANONICAL_PAGE_TYPES allowlist assertion.
    """
    return {
        "wiki_schema_version": 2,
        "slug": slug,
        "title": slug,
        "content": content,
        "category": category,
        "tags": tags,
        "source_memory_ids": None,
        "confidence": "high",
        "append": False,
        "force": True,
        "replace_slug": replace_slug,
        "directory_context": directory,
        "page_type": "adr",
    }


@observe(tier="stage", metric="tools.adr._assemble_index_rows")
def _assemble_index_rows(
    existing_index: str,
    new_row: dict,
    new_adr_id: str,
    target_ids: list[str],
) -> list[dict]:
    """Return the full ADR index row-set after adding ``new_row`` + supersede back-links.

    Two-pass: append the new row, then flip each supersede target's status to
    'superseded' and record the new ADR's NNNN in its ``superseded_by`` column.
    """
    from yadgar.core.server.tools.adr_index import parse_index_rows  # noqa: PLC0415

    rows = parse_index_rows(existing_index)
    rows.append(new_row)
    if not target_ids:
        return rows
    by_id = {r["adr_id"]: r for r in rows}
    nnnn = new_adr_id.split("-")[1]
    for tid in target_ids:
        row = by_id.get(tid)
        if row is None:
            continue
        row["status"] = "superseded"
        prev = row.get("superseded_by", "-")
        row["superseded_by"] = nnnn if prev in ("-", "") else f"{prev},{nnnn}"
    return rows
