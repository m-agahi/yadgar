#!/usr/bin/env python3
"""CHANGELOG `[Unreleased]` version-marker lint (Car 0102).

WHY THIS EXISTS (the incident, 2026-05-16 -> 2026-08-01)
---------------------------------------------------------
`docs/CHANGELOG.md` had not cut a version-numbered `## [x.y.z] - <date>`
section since `[5.106.0]` (2026-07-04). Every release from 5.107.0 through
5.170.x — 55 entries across 40 shipped versions — sat undifferentiated
inside `## [Unreleased]`, each one already carrying its own version inline
as a bold marker (`**v5.167.1 — fix: ...**`). The versions were recorded;
they were just never promoted into their own sections. Nothing caught this
because nothing checked `[Unreleased]` for entries that already claim a
shipped version.

WHAT THIS GUARD DOES
---------------------
Scans the body of the FIRST `## [Unreleased]` section in `docs/CHANGELOG.md`
(from that heading to the next `## [` heading) for top-level bold entries
matching `**vX.Y.Z ...` (a literal inline version marker, e.g.
`**v5.167.1 — fix: ...**`). Any match means a release shipped without its
CHANGELOG section being cut — the entry needs to be promoted to its own
`## [X.Y.Z] - <date>` section (dated from the git tag or release commit,
never invented) and removed from `[Unreleased]`.

Deliberately narrow: only matches bold lines beginning `**v<digit>` at
column 0 — this is the exact shape every historical shipped-but-unpromoted
entry used (see Car 0102). A bullet sub-item, a prose mention of a version
number mid-paragraph, or a bold entry with no version marker at all (e.g.
`**fix: ...**`, which legitimately stays in `[Unreleased]` until it ships)
is never flagged.

Only the FIRST `## [Unreleased]` section is scanned — deeper historical
duplicates of that heading (a pre-existing, unrelated artifact further down
the file) are out of scope for this guard.

THE CEILING — read this before trusting a green run
------------------------------------------------------------------------------
This is marker-detection, not release-tracking: an entry that ships without
ever gaining an inline `**vX.Y.Z` marker is invisible here (nothing to
detect). It also does not verify that a promoted section's date is correct
— only that `[Unreleased]` itself carries no more inline version claims.

No allowlist. Every `[Unreleased]` entry this guard flags is meant to be
promoted; if a new one needs an exception, narrow the checked pattern above
(with a documented reason), not bolt on an allowlist file.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_CHANGELOG_REL = Path("docs/CHANGELOG.md")

_SECTION_HEADING_RE = re.compile(r"^## \[")
_UNRELEASED_HEADING_RE = re.compile(r"^## \[Unreleased\]\s*$")
_VERSION_MARKER_RE = re.compile(r"^\*\*v(\d+\.\d+\.\d+)\b")


def extract_unreleased_body(text: str) -> str:
    """Return the body of the FIRST `## [Unreleased]` section (heading exclusive)."""
    lines = text.splitlines(keepends=True)
    start = None
    for i, line in enumerate(lines):
        if _UNRELEASED_HEADING_RE.match(line):
            start = i + 1
            break
    if start is None:
        return ""

    end = len(lines)
    for i in range(start, len(lines)):
        if _SECTION_HEADING_RE.match(lines[i]):
            end = i
            break

    return "".join(lines[start:end])


def find_version_markers(body: str) -> list[str]:
    """Return the sorted list of inline `vX.Y.Z` markers found in *body*."""
    versions: list[str] = []
    for line in body.splitlines():
        m = _VERSION_MARKER_RE.match(line)
        if m:
            versions.append(m.group(1))
    return versions


def check(repo_root: Path | None = None) -> list[str]:
    """Return a list of violation strings; empty means `[Unreleased]` is clean."""
    repo_root = repo_root or Path(__file__).resolve().parent.parent
    changelog_path = repo_root / _CHANGELOG_REL
    if not changelog_path.is_file():
        return [f"{_CHANGELOG_REL}: file not found at {changelog_path}"]

    text = changelog_path.read_text(encoding="utf-8")
    body = extract_unreleased_body(text)
    versions = find_version_markers(body)

    return [
        f"{_CHANGELOG_REL}: [Unreleased] contains a shipped-version marker `v{v}` "
        f"— promote this entry to its own `## [{v}] - <date>` section."
        for v in versions
    ]


def main(argv: list[str] | None = None) -> int:
    del argv
    violations = check()
    if violations:
        print("CHANGELOG [Unreleased] version-marker lint FAILED:", file=sys.stderr)
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        print(
            f"\n{len(violations)} shipped-version marker(s) still inside [Unreleased] in "
            f"{_CHANGELOG_REL}. Promote each to its own dated `## [x.y.z]` section — "
            "date from the git tag or release commit, never invented.",
            file=sys.stderr,
        )
        return 1
    print(
        f"CHANGELOG [Unreleased] version-marker lint OK — no shipped-version markers in {_CHANGELOG_REL}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
