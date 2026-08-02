"""ADR index management — slug helpers, ID assignment, index parse/render.

Extracted from adr.py (car/adr-split). The public surface of this module
is re-exported from yadgar.core.server.tools.adr for backward compatibility.
"""

from __future__ import annotations

import os
import re

# ── Constants ─────────────────────────────────────────────────────────────────

# Regex that matches ## ADR-NNNN at column 0 (header-only scan — used by the
# migration to parse the legacy monolith, and by parse_adr_ids).
_ADR_HEADER_RE = re.compile(r"^## ADR-(\d{4})", re.MULTILINE)

# Car G — _INDEX_ROW_RE and _INDEX_HEADER deleted (SQL ledger is ID source).

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


# Car G — index parsing/rendering deleted. The `adr` ledger table is the
# ID source of truth (D35b); parse_index_rows / _render_index_row /
# _build_index_content / _next_adr_id / _committed_page_max_id /
# _index_max_id / _next_adr_id_from_index are all gone. The seed reads
# per-ADR pages directly, not the markdown index.
#
# Slug helpers (adr_log_slug, adr_index_slug, adr_page_slug) and the
# parse_adr_ids monolith-migration helper are RETAINED — they're used
# by the migration script and back-compat read paths.
