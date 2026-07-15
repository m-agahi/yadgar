"""ADR index management — slug helpers, ID assignment, index parse/render.

Extracted from adr.py (car/adr-split). The public surface of this module
is re-exported from yadgar.core.server.tools.adr for backward compatibility.
"""

from __future__ import annotations

import os
import re

from yadgar._shared.observability.observe import observe

# ── Constants ─────────────────────────────────────────────────────────────────

# Regex that matches ## ADR-NNNN at column 0 (header-only scan — used by the
# migration to parse the legacy monolith, and by parse_adr_ids).
_ADR_HEADER_RE = re.compile(r"^## ADR-(\d{4})", re.MULTILINE)

# Index table row: | ADR-NNNN | status | date | title | supersedes | superseded-by | slug |
_INDEX_ROW_RE = re.compile(
    r"^\|\s*(ADR-\d{4})\s*\|\s*(?P<status>[^|]*?)\s*\|\s*(?P<date>[^|]*?)\s*\|"
    r"\s*(?P<title>[^|]*?)\s*\|\s*(?P<supersedes>[^|]*?)\s*\|"
    r"\s*(?P<superseded_by>[^|]*?)\s*\|\s*(?P<slug>[^|]*?)\s*\|\s*$",
    re.MULTILINE,
)

_INDEX_HEADER = (
    "| ADR | Status | Date | Title | Supersedes | Superseded-by | Slug |\n"
    "| --- | --- | --- | --- | --- | --- | --- |\n"
)

# Committed per-ADR page slug pattern: <project>-adr-NNNN.
_ADR_PAGE_SLUG_RE = re.compile(r"-adr-(\d{4})$")


# ── Slug helpers ───────────────────────────────────────────────────────────────


def adr_log_slug(resolved: str) -> str:
    """Legacy monolith slug `<project>-adr-log` (migration source + back-compat).

    Retained for the migration script (reads the old monolith) and
    _get_adr_log_updated_at back-compat. New writes never touch this slug.
    """
    return f"{os.path.basename(resolved)}-adr-log"


def adr_index_slug(resolved: str) -> str:
    """The canonical ADR index slug `<project>-adr-index`."""
    return f"{os.path.basename(resolved)}-adr-index"


def adr_page_slug(resolved: str, adr_id: str) -> str:
    """The canonical per-ADR page slug `<project>-adr-NNNN`.

    ``adr_id`` is an "ADR-NNNN" string; the slug lowercases it to `adr-NNNN`
    (slugify maps "ADR-0001" → "adr-0001"), so the stored wiki title must equal
    this slug string to make ``_slugify(title)`` deterministic.
    """
    return f"{os.path.basename(resolved)}-{adr_id.lower()}"


def parse_adr_ids(content: str) -> list[str]:
    """Extract ADR IDs from legacy monolith content, sorted descending.

    Retained for the migration (parses the old `## ADR-NNNN` monolith) and
    _build_adr_log back-compat. Returns ["ADR-NNNN", ...] descending, [] if none.
    """
    matches = _ADR_HEADER_RE.findall(content)
    return [f"ADR-{int(n):04d}" for n in sorted(matches, key=int, reverse=True)]


# ── Index parsing / rendering ──────────────────────────────────────────────────


@observe(tier="stage", metric="tools.adr.parse_index_rows")
def parse_index_rows(content: str) -> list[dict]:
    """Parse the ADR index table into a list of row dicts (in file order).

    Each row: {adr_id, status, date, title, supersedes, superseded_by, slug}.
    Empty list if the index is absent or has no data rows.
    """
    rows: list[dict] = []
    for m in _INDEX_ROW_RE.finditer(content):
        rows.append(
            {
                "adr_id": m.group(1),
                "status": m.group("status").strip(),
                "date": m.group("date").strip(),
                "title": m.group("title").strip(),
                "supersedes": m.group("supersedes").strip(),
                "superseded_by": m.group("superseded_by").strip(),
                "slug": m.group("slug").strip(),
            }
        )
    return rows


def _index_max_id(content: str) -> int:
    """Max ADR number from index table rows (0 when empty/absent)."""
    ids = [int(r["adr_id"].split("-")[1]) for r in parse_index_rows(content)]
    return max(ids) if ids else 0


@observe(tier="stage", metric="tools.adr._committed_page_max_id")
def _committed_page_max_id(resolved: str) -> int:
    """Max ADR number from COMMITTED `<project>-adr-NNNN` page slugs (0 when none).

    The per-ADR page is the ID-BEARING artifact and is written wait=True (committed
    within the per-project lock). Scanning committed page slugs makes the next-ID
    assignment resilient to a LAGGING index: even if the index write is still queued
    (wait_timeout — the write converges but is not yet in the DB), the freshly
    committed per-ADR page slug is already visible here, so a subsequent adr_add
    never re-assigns a used ID. Fixes the RYW-on-timeout duplicate-ID race.
    """
    project = os.path.basename(resolved)
    prefix = f"{project}-adr-"
    try:
        from yadgar.core.server.tools.wiki import wiki_list  # noqa: PLC0415

        pages = wiki_list(slug_prefix=prefix, directory=resolved, limit=10000)
    except Exception:  # noqa: BLE001 — fall back to index-only on a list failure
        return 0
    max_n = 0
    for p in pages:
        m = _ADR_PAGE_SLUG_RE.search(p.get("slug") or "")
        if m:
            max_n = max(max_n, int(m.group(1)))
    return max_n


@observe(tier="stage", metric="tools.adr._next_adr_id")
def _next_adr_id(resolved: str, index_content: str) -> str:
    """Next sequential ADR id from BOTH the index rows AND committed page slugs.

    Authoritative source = the committed per-ADR page slugs (ID-bearing, wait=True);
    the index is a derived convenience view that may lag when its write is queued.
    Taking the max over both guarantees uniqueness even on an index wait_timeout.
    """
    n = max(_index_max_id(index_content), _committed_page_max_id(resolved))
    return f"ADR-{n + 1:04d}"


@observe(tier="stage", metric="tools.adr._next_adr_id_from_index")
def _next_adr_id_from_index(content: str) -> str:
    """Index-only next-ID (retained for tests / callers that pass only index text).

    Prefer ``_next_adr_id(resolved, content)`` which also scans committed page
    slugs and is resilient to a lagging index.
    """
    n = _index_max_id(content)
    return f"ADR-{n + 1:04d}"


def _render_index_row(
    adr_id: str,
    status: str,
    date: str,
    title: str,
    supersedes: str,
    superseded_by: str,
    slug: str,
) -> str:
    """Render one index table row. Pipes/newlines in free text are sanitised."""

    def _clean(v: str) -> str:
        return str(v).replace("|", "/").replace("\n", " ").strip() or "-"

    return (
        f"| {adr_id} | {_clean(status)} | {_clean(date)} | {_clean(title)} | "
        f"{_clean(supersedes)} | {_clean(superseded_by)} | {slug} |"
    )


@observe(tier="stage", metric="tools.adr._build_index_content")
def _build_index_content(project_name: str, rows: list[dict]) -> str:
    """Build the full index page content from row dicts (ascending by ID)."""
    ordered = sorted(rows, key=lambda r: int(r["adr_id"].split("-")[1]))
    body = _INDEX_HEADER
    for r in ordered:
        body += (
            _render_index_row(
                r["adr_id"],
                r["status"],
                r["date"],
                r["title"],
                r.get("supersedes", "none"),
                r.get("superseded_by", "-"),
                r["slug"],
            )
            + "\n"
        )
    return (
        f"# {project_name} ADR Index\n\n"
        f"Architecture Decision Records for `{project_name}` — one canonical page per ADR "
        f"(`{project_name}-adr-NNNN`). This index is the ID source of truth.\n\n"
        f"{body}"
    )
