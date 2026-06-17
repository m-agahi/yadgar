#!/usr/bin/env python3
"""Coverage lint for docs/BEHAVIOR_CONTRACT.md (the behavior-contract self-check).

Three rules, all derived from the contract's own header:

  1. A ✅ row MUST cite a resolvable ``path::node`` test reference. A ✅ without a
     resolvable reference is a rejected claim (belief-without-a-test).
  2. ANY row carrying a ``path::node`` reference (✅ OR a ⏳ with a [r]/[u] tag that
     names a test) MUST resolve to a real test. The tag itself is NOT mandatory —
     this is *validate-if-present*: a dangling reference is a failure, an
     un-referenced [r]/[u] row is fine (it just means "coverage exists, not yet
     wired to a contract e2e"). This extends rule 1 beyond ✅.
  3. The header counts (✅/⏳/❌ and, over the ⏳ rows, [r]/[u]/none) MUST equal the
     actual tally over all BC-* rows. Header drift is a failure — this is the
     finding (header math drifted from content) that v5.71 fixed, made permanent.

Resolution: a reference is ``relative/path.py::Node[::SubNode]``. The path is
resolved relative to the repo root, with a ``yadgar/`` prefix tried as a fallback
(the contract historically cites ``tests/e2e/...`` while the tree is
``yadgar/tests/e2e/...``). A reference resolves if the file exists AND every
``::``-segment after the path appears as a ``class``/``def`` name in that file.

Run as a plain (non-e2e) pytest: ``yadgar/tests/test_contract_coverage.py``.
Or standalone: ``python scripts/check_contract_coverage.py`` (exit 0 = clean).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONTRACT = _REPO_ROOT / "docs" / "BEHAVIOR_CONTRACT.md"

_STATUSES = ("✅", "❌", "⏳")
# A `path.py::Node[::Sub]` reference inside backticks or bare.
_REF_RE = re.compile(r"([A-Za-z0-9_./-]+\.py(?:::[A-Za-z0-9_]+)+)")
# Header count lines we assert against (rule 3).
_STATUS_HDR_RE = re.compile(r"\*\*([0-9]+)\s*✅\s*·\s*([0-9]+)\s*⏳\s*·\s*([0-9]+)\s*❌\.\*\*")
_TAG_HDR_RE = re.compile(r"\*\*([0-9]+)\s*`\[r\]`.*?·\s*([0-9]+)\s*`\[u\]`.*?·\s*([0-9]+)\s*none")


# ---------------------------------------------------------------------------
# Parse rows
# ---------------------------------------------------------------------------
def _row_status(line: str) -> str | None:
    """First status emoji on the line, or None."""
    for emoji in _STATUSES:
        if emoji in line:
            return emoji
    return None


def parse_rows(text: str) -> list[tuple[str, str, str]]:
    """Return (bc_id, status, line) for every BC-* row (list-item OR table)."""
    rows: list[tuple[str, str, str]] = []
    for line in text.splitlines():
        m = re.match(r"^- (BC-[A-Za-z0-9]+)\b", line)
        if not m:
            m = re.match(r"^\|\s*(BC-T\d+)\s*\|", line)
        if not m:
            continue
        status = _row_status(line)
        if status is None:
            continue
        rows.append((m.group(1), status, line))
    return rows


def _row_tag(line: str) -> str:
    """Classify a row's coverage tag: 'r', 'u', or 'none'."""
    if "[r]" in line or "[ci+r]" in line:
        return "r"
    if "[u]" in line:
        return "u"
    return "none"


# ---------------------------------------------------------------------------
# Resolve a `path::node` reference
# ---------------------------------------------------------------------------
def _candidate_paths(rel: str) -> list[Path]:
    return [_REPO_ROOT / rel, _REPO_ROOT / "yadgar" / rel]


def resolve_ref(ref: str) -> str | None:
    """Return None if the reference resolves, else an error string."""
    path_part, *nodes = ref.split("::")
    src = next((p for p in _candidate_paths(path_part) if p.is_file()), None)
    if src is None:
        return f"file not found: {path_part}"
    body = src.read_text(encoding="utf-8")
    for node in nodes:
        if not re.search(rf"\b(?:class|def)\s+{re.escape(node)}\b", body):
            return f"node {node!r} not found in {path_part}"
    return None


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------
def check(text: str) -> list[str]:
    """Return a list of violation strings (empty = clean)."""
    errors: list[str] = []
    rows = parse_rows(text)

    # Rules 1 + 2 — reference resolution.
    for bc_id, status, line in rows:
        refs = _REF_RE.findall(line)
        if status == "✅" and not refs:
            errors.append(f"{bc_id}: ✅ without a `path::node` test reference (rule 1)")
        for ref in refs:
            err = resolve_ref(ref)
            if err:
                errors.append(f"{bc_id}: dangling reference {ref!r} — {err} (rule 2)")

    # Rule 3 — header counts == actual tally.
    n_green = sum(1 for _, s, _ in rows if s == "✅")
    n_pending = sum(1 for _, s, _ in rows if s == "⏳")
    n_broken = sum(1 for _, s, _ in rows if s == "❌")
    tag = {"r": 0, "u": 0, "none": 0}
    for _, s, line in rows:
        if s == "⏳":
            tag[_row_tag(line)] += 1

    sm = _STATUS_HDR_RE.search(text)
    if not sm:
        errors.append("header: status count line (`N ✅ · N ⏳ · N ❌`) not found (rule 3)")
    else:
        hg, hp, hb = (int(x) for x in sm.groups())
        if (hg, hp, hb) != (n_green, n_pending, n_broken):
            errors.append(
                f"header status drift (rule 3): header says {hg} ✅ · {hp} ⏳ · {hb} ❌; "
                f"actual {n_green} ✅ · {n_pending} ⏳ · {n_broken} ❌"
            )
    tm = _TAG_HDR_RE.search(text)
    if not tm:
        errors.append("header: tag count line (`N [r] · N [u] · N none`) not found (rule 3)")
    else:
        hr, hu, hn = (int(x) for x in tm.groups())
        if (hr, hu, hn) != (tag["r"], tag["u"], tag["none"]):
            errors.append(
                f"header tag drift (rule 3): header says {hr} [r] · {hu} [u] · {hn} none; "
                f"actual {tag['r']} [r] · {tag['u']} [u] · {tag['none']} none"
            )
    return errors


def main() -> int:
    if not _CONTRACT.is_file():
        print(f"ERROR: contract not found at {_CONTRACT}", file=sys.stderr)
        return 1
    errors = check(_CONTRACT.read_text(encoding="utf-8"))
    if errors:
        print("BEHAVIOR_CONTRACT coverage lint FAILED:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print("BEHAVIOR_CONTRACT coverage lint OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
