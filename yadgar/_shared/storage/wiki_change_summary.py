"""Wiki change-summary helpers (Car 2 v5.113).

Pure difflib-based diff summarisation for wiki_page_version rows. No LLM (I9).
Section detection: markdown ## / ### headings at column 0 within 5 lines above
changed content. Extracted from ``wiki.py`` (I13 file_loc cap).
"""

from __future__ import annotations

import difflib
import re as _re

from yadgar._shared.observability.observe import observe

_HEADING_RE = _re.compile(r"^##+ (.+)")


@observe(tier="hot")
def _diff_context_line(diff_line: str) -> str:
    """Strip unified-diff prefix (+/-/@/ ) to get the raw text for heading detection."""
    if diff_line.startswith("@"):
        return diff_line.lstrip("+-@ ")
    return diff_line[1:] if diff_line else ""


@observe(tier="hot")
def _find_nearby_heading(diff: list[str], i: int, touched: list[str]) -> None:
    """Look back up to 5 diff lines for a ## heading; append to touched if found."""
    for j in range(max(0, i - 5), i):
        m = _HEADING_RE.match(_diff_context_line(diff[j]))
        if m:
            heading = m.group(1).strip()
            if heading not in touched:
                touched.append(heading)
            return


@observe(tier="hot")
def compute_change_summary(old_content: str, new_content: str) -> str:
    """Generate a concise diff summary for a wiki page version.

    Format: "+N -M lines | sections: 'Foo', 'Bar' | size: X → Y bytes"
    Capped at 300 chars. No LLM — pure difflib (I9: no LLM on write path).

    Section detection: markdown ## / ### headings at column 0 within 5 lines
    above changed (added/removed) content.
    """
    old_lines = old_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)
    diff = list(difflib.unified_diff(old_lines, new_lines, lineterm=""))

    added = 0
    removed = 0
    touched_sections: list[str] = []

    for i, line in enumerate(diff):
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
            _find_nearby_heading(diff, i, touched_sections)
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1

    size_old = len(old_content.encode())
    size_new = len(new_content.encode())

    parts = [f"+{added} -{removed} lines"]
    if touched_sections:
        section_str = ", ".join(f"'{s}'" for s in touched_sections[:5])
        parts.append(f"sections: {section_str}")
    parts.append(f"size: {size_old} → {size_new} bytes")

    summary = " | ".join(parts)
    if len(summary) > 300:
        summary = summary[:299] + "…"
    return summary
