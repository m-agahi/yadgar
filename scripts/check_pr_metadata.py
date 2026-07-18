#!/usr/bin/env python3
"""Validate PR title and body against the repository's pull-request template.

Exit codes:
    0 — PR metadata is valid
    1 — one or more validation errors found

Environment variables (set by the CI workflow via ``env:``):
    PR_TITLE  — pull-request title string
    PR_BODY   — pull-request body / description string
"""

import os
import re
import sys
from pathlib import Path  # noqa: F401  (keeps parity with sibling scripts)

# Required section names in the PR body (case-insensitive match).
# Detected via EITHER "## <Name>" OR "<summary>...<Name>...</summary>".
REQUIRED_SECTIONS = ("Summary", "What", "Why", "Notes", "Test plan")

_TEMPLATE_PATH = ".forgejo/PULL_REQUEST_TEMPLATE.md"

# Matches "## <Name>" header lines (case-insensitive via re.IGNORECASE).
_H2_PATTERN = re.compile(r"^##\s+(.+)$", re.MULTILINE)

# Matches "<summary>...<Name>...</summary>" — tolerates inline tags + whitespace.
# Non-greedy to avoid swallowing across multiple <details> blocks.
_SUMMARY_TAG_PATTERN = re.compile(
    r"<summary[^>]*>\s*(?:<[^>]+>\s*)*(.+?)\s*(?:<[^>]+>\s*)*</summary>",
    re.IGNORECASE,
)

# Strips HTML comments (must run BEFORE stripping tags — non-greedy, DOTALL).
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

# Strips HTML tags after comments are removed.
_TAG_RE = re.compile(r"<[^>]+>")


def _find_sections(body: str) -> list[tuple[str, int, int]]:
    """Return list of (section_name_lower, match_start, match_end) sorted by start.

    Collects both H2 headers and <summary>…</summary> tags, de-duplicates by
    name (first occurrence wins), and sorts by start position in the body.

    match_start — where the detecting construct begins (used as window end for prior section).
    match_end   — where the detecting construct ends (content of this section starts here).
    """
    found: dict[str, tuple[int, int]] = {}  # name_lower → (match.start, match.end)

    for m in _H2_PATTERN.finditer(body):
        name = m.group(1).strip()
        key = name.lower()
        if key not in found:
            found[key] = (m.start(), m.end())

    for m in _SUMMARY_TAG_PATTERN.finditer(body):
        # Strip any inline tags from the captured label to get the plain name.
        raw = m.group(1)
        name = _TAG_RE.sub("", raw).strip()
        key = name.lower()
        if key not in found:
            found[key] = (m.start(), m.end())

    # Sort by start position.
    return sorted(
        [(k, s, e) for k, (s, e) in found.items()],
        key=lambda t: t[1],
    )


def _clean(text: str) -> str:
    """Strip HTML comments (first), then HTML tags, return remaining text."""
    text = _COMMENT_RE.sub("", text)
    text = _TAG_RE.sub("", text)
    return text


def _section_content(body: str, content_start: int, content_end: int | None) -> str:
    """Extract raw body slice from content_start to content_end, then clean it."""
    raw = body[content_start:content_end]
    return _clean(raw)


def validate_pr_metadata(title: str, body: str) -> list[str]:
    """Return a list of human-readable error strings; empty list means valid.

    Args:
        title: The pull-request title (may include surrounding whitespace).
        body:  The pull-request body / description.
    """
    errors: list[str] = []
    title_stripped = title.strip()

    # -- Title checks --------------------------------------------------------

    if len(title_stripped) < 8:
        errors.append(f"PR title too short (need >=8 chars): {title_stripped!r}")

    if " " not in title_stripped:
        errors.append(f"PR title must be descriptive (multiple words): {title_stripped!r}")

    # -- Body section checks -------------------------------------------------

    # Build a map: name_lower → (content_start, content_end).
    # content_start = end of this section's header construct.
    # content_end   = start of the NEXT section's header construct (or None = EOF).
    found_sections = _find_sections(body)
    found_map: dict[str, tuple[int, int | None]] = {}
    for i, (key, _start, end_pos) in enumerate(found_sections):
        next_start = found_sections[i + 1][1] if i + 1 < len(found_sections) else None
        found_map[key] = (end_pos, next_start)

    for section_name in REQUIRED_SECTIONS:
        key = section_name.lower()
        if key not in found_map:
            errors.append(
                f"PR body missing required section '{section_name}'"
                f" — use the PR template ({_TEMPLATE_PATH})"
            )
            continue

        end_pos, next_end = found_map[key]
        content = _section_content(body, end_pos, next_end)
        non_ws = len(content.replace(" ", "").replace("\n", "").replace("\t", "").replace("\r", ""))
        # Strip all whitespace for the count (equivalent to re.sub r'\s+', '', content))
        non_ws = len(re.sub(r"\s", "", content))
        if non_ws < 20:
            errors.append(
                f"PR body section '{section_name}' has too little content"
                f" ({non_ws} non-whitespace chars, need >=20)"
                f" — use the PR template ({_TEMPLATE_PATH})"
            )

    return errors


def main() -> int:
    """Read PR_TITLE/PR_BODY from the environment, validate, and report."""
    pr_title = os.environ.get("PR_TITLE", "")
    pr_body = os.environ.get("PR_BODY", "")

    errors = validate_pr_metadata(pr_title, pr_body)
    if errors:
        for msg in errors:
            print(f"ERROR: {msg}", file=sys.stderr)
        return 1

    print("PR metadata OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
