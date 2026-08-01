#!/usr/bin/env python3
"""Roadmap link-liveness lint (Car 0026).

WHY THIS EXISTS (the incident, 2026-07-16 -> 2026-08-01)
---------------------------------------------------------
`docs/plans/ROADMAP.md` is the stated single source of truth for open plans
("Register every new plan in this file. If it's not here, it's not tracked.").
By 2026-08-01 it had drifted for over two weeks: 9 markdown links pointed at
`docs/plans/<slug>.md` for plans that had since shipped and moved to
`docs/plans/archive/<slug>.md`, one link (`port-opencode-2026-07-18.md`)
pointed at a plan that was renamed before it ever shipped and resolves
nowhere at all, and one backticked bare-filename reference
(`BEHAVIOR_CONTRACT.md`) survived the 2026-07-14 docs-reorg that moved it to
`docs/contracts/BEHAVIOR_CONTRACT.md`. None of this was caught because
nothing checked the roadmap's own links against the filesystem.

WHAT THIS GUARD DOES
---------------------
Scans `docs/plans/ROADMAP.md` for every reference to a `.md` or `.html` file,
in BOTH forms actually used in that doc:

  1. Markdown links:  `[text](path)`
  2. Backticked paths: `` `path` ``  (only tokens ending in .md/.html — this
     deliberately excludes prose mentions of source files like
     `` `install_hooks_lib.py` `` and illustrative non-paths like
     `` `PLAN_V5_NN_TOPIC.md` ``, neither of which is a doc-reference the
     roadmap's own convention governs)

Each candidate path is resolved two ways — relative to `docs/plans/` (sibling
links: `archive/foo.md`) and relative to the repo root (root-relative refs:
`docs/CHANGELOG.md`, `benchmarks/reports/foo.md`). A reference is dead only
if NEITHER resolves. Deliberately NO archive/-prefix fallback for a bare
filename: that leniency is exactly what let the 9-dead-link incident hide —
a link written as `foo.md` silently resolving against `archive/foo.md` is
the bug, not a false positive, because it means the roadmap still claims the
plan is open (`docs/plans/foo.md`) when it has actually shipped and moved.
Once a plan ships, its roadmap reference must be rewritten with the
`archive/` prefix; the guard enforces that rather than working around it.

Tokens containing `<`, `>`, or `*` are skipped — these are template
placeholders (`docs/plans/<slug>.md`) or glob patterns (`roadmap/v*.md`)
used in the convention prose, never literal paths on disk.

THE CEILING — read this before trusting a green run
------------------------------------------------------------------------------
This is existence-liveness, NOT content-liveness: a link to a real-but-wrong
file (e.g. a plan that shipped something different than the roadmap claims)
is invisible here. It also only reads `docs/plans/ROADMAP.md` — it does not
recurse into the individual plan docs it links to, and it does not check
external (http) URLs. Content inside fenced code blocks (```...```) is
stripped before scanning so a code sample mentioning a filename is not
treated as a doc reference.

No allowlist. Every reference this guard checks is meant to resolve; if a
new one needs an exception, narrow the checked-reference shape (extend the
exclusions above with a documented reason), not bolt on an allowlist file.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_MD_LINK_RE = re.compile(r"\]\(([^)\s]+)\)")
_BACKTICK_DOC_RE = re.compile(r"`([^`\s]+\.(?:md|html))`", re.IGNORECASE)

_ROADMAP_REL = Path("docs/plans/ROADMAP.md")


def _strip_fences(text: str) -> str:
    return _FENCE_RE.sub("", text)


def extract_references(text: str) -> set[str]:
    """Return the set of unique .md/.html reference strings in *text*."""
    stripped = _strip_fences(text)
    refs: set[str] = set()

    for match in _MD_LINK_RE.finditer(stripped):
        target = match.group(1)
        if target.startswith(("http://", "https://", "#")):
            continue
        if target.lower().endswith((".md", ".html")) and not any(c in target for c in "<>*"):
            refs.add(target)

    for match in _BACKTICK_DOC_RE.finditer(stripped):
        target = match.group(1)
        if target.startswith(("http://", "https://")):
            continue
        if not any(c in target for c in "<>*"):
            refs.add(target)

    return refs


def _candidates(ref: str, repo_root: Path, roadmap_dir: Path) -> list[Path]:
    return [roadmap_dir / ref, repo_root / ref]


def check(repo_root: Path | None = None) -> list[str]:
    """Return a list of violation strings; empty means all references resolve."""
    repo_root = repo_root or Path(__file__).resolve().parent.parent
    roadmap_path = repo_root / _ROADMAP_REL
    if not roadmap_path.is_file():
        return [f"{_ROADMAP_REL}: file not found at {roadmap_path}"]

    roadmap_dir = roadmap_path.parent
    text = roadmap_path.read_text(encoding="utf-8")

    violations: list[str] = []
    for ref in sorted(extract_references(text)):
        cands = _candidates(ref, repo_root, roadmap_dir)
        if not any(c.is_file() for c in cands):
            shown = ", ".join(str(c.relative_to(repo_root)) for c in cands)
            violations.append(f"{_ROADMAP_REL}: dead reference `{ref}` (checked: {shown})")

    return violations


def main(argv: list[str] | None = None) -> int:
    del argv
    violations = check()
    if violations:
        print("ROADMAP link-liveness lint FAILED:", file=sys.stderr)
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        print(
            f"\n{len(violations)} dead reference(s) in {_ROADMAP_REL}. "
            "Fix the path, or move it to the correct archive/ location.",
            file=sys.stderr,
        )
        return 1
    print(f"ROADMAP link-liveness lint OK — all references in {_ROADMAP_REL} resolve.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
