#!/usr/bin/env python3
"""Skip-inventory gate: every skip reason must match a sanctioned entry.

Usage (two modes):
  1. Pipe mode (CI): parse pytest -rs output from stdin
       pytest <paths> -rs --tb=no -q | python scripts/check_skip_inventory.py

  2. Run mode: run pytest and check in one shot
       python scripts/check_skip_inventory.py --run <pytest-args...>

Exit 0 → all skips sanctioned.
Exit 1 → unsanctioned skip(s) found — prints offenders.

Inventory: yadgar/tests/skip_inventory.json
  Each entry has a "reason_pattern" substring matched against the skip reason.
  A skip is sanctioned if its reason contains any entry's reason_pattern (case-insensitive).
  Multiple skips with different reasons all matching the same pattern are fine.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Inventory loader
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_INVENTORY_PATH = _REPO_ROOT / "yadgar" / "tests" / "skip_inventory.json"

# Pytest -rs format: "SKIPPED [N] path/to/test.py:lineno: reason text"
# The path has colons (file:line:), so we match the LAST ": " separator to extract reason.
# Pattern: SKIPPED [N] <anything>:<lineno>: <reason>
_SKIP_LINE_RE = re.compile(r"^SKIPPED\s+\[\d+\]\s+.+:\d+:\s+(.+)$")


def _load_inventory(path: Path = _INVENTORY_PATH) -> list[str]:
    """Return list of sanctioned reason_pattern strings (lowercase)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return [e["reason_pattern"].lower() for e in data["entries"]]


def _extract_skip_reasons(lines: list[str]) -> list[tuple[str, str]]:
    """Parse pytest -rs output; return list of (raw_line, reason) tuples."""
    results = []
    for line in lines:
        m = _SKIP_LINE_RE.match(line.strip())
        if m:
            results.append((line.rstrip(), m.group(1).strip()))
    return results


def _is_sanctioned(reason: str, patterns: list[str]) -> bool:
    """Return True if reason contains any sanctioned pattern (substring, case-insensitive).

    patterns should be lowercase (from _load_inventory); this also lowercases
    the reason for case-insensitive comparison.
    """
    reason_lower = reason.lower()
    return any(pat.lower() in reason_lower for pat in patterns)


def check(lines: list[str], inventory_path: Path = _INVENTORY_PATH) -> tuple[bool, list[str]]:
    """Check skip reasons in lines against inventory.

    Returns (ok, offenders) where offenders is list of unsanctioned lines.
    Pure function — injectable for testing.
    """
    patterns = _load_inventory(inventory_path)
    skips = _extract_skip_reasons(lines)
    offenders = []
    for raw_line, reason in skips:
        if not _is_sanctioned(reason, patterns):
            offenders.append(f"  UNSANCTIONED: {raw_line}")
    return len(offenders) == 0, offenders


# ---------------------------------------------------------------------------
# Inventory governance (ADR-0087) — mirrors the I30/I33 allowlist pattern
# ---------------------------------------------------------------------------

# Substring matching makes very short patterns act as de-facto wildcards.
_MIN_PATTERN_LEN = 12
# Justification floor, same bar as I30 complexity / I33 observe-exempt rationales.
_MIN_NOTE_LEN = 40


def validate_inventory(data: dict, repo_root: Path = _REPO_ROOT) -> list[str]:
    """Validate the inventory dict; return list of error strings (empty = valid).

    ADR-0087 governance on top of the required-fields check:
      - note (the human justification) must be >= 40 chars
      - no wildcard entries: reason_pattern non-empty, no '*', >= 12 chars
      - stale-entry hard-fail: the referenced test file must exist AND contain
        the reason_pattern (case-insensitive) — an entry whose test was deleted
        or whose reason drifted no longer sanctions anything and must be removed
    Pure function — repo_root injectable for testing.
    """
    required = {"id", "file", "verdict", "reason_pattern", "note"}
    errors: list[str] = []
    for i, entry in enumerate(data.get("entries", [])):
        eid = entry.get("id", "?")
        missing = required - set(entry.keys())
        if missing:
            errors.append(f"  entry[{i}] ({eid!r}): missing fields {missing}")
            continue
        if entry.get("verdict") not in {"LEGIT-CONDITIONAL", "DEAD", "MIS-GATED"}:
            errors.append(f"  entry[{i}] ({eid!r}): invalid verdict {entry.get('verdict')!r}")
        note = entry.get("note", "")
        if len(note) < _MIN_NOTE_LEN:
            errors.append(
                f"  entry[{i}] ({eid!r}): note is {len(note)} chars — a justification "
                f"of >= {_MIN_NOTE_LEN} chars is required (ADR-0087)"
            )
        pattern = entry.get("reason_pattern", "")
        if "*" in pattern:
            errors.append(f"  entry[{i}] ({eid!r}): wildcard reason_pattern not allowed")
        elif len(pattern) < _MIN_PATTERN_LEN:
            errors.append(
                f"  entry[{i}] ({eid!r}): reason_pattern is too short "
                f"({len(pattern)} < {_MIN_PATTERN_LEN} chars) — substring matching "
                "makes short patterns act as wildcards"
            )
        else:
            test_file = repo_root / entry["file"]
            if not test_file.is_file():
                errors.append(
                    f"  entry[{i}] ({eid!r}): STALE — file {entry['file']} no longer exists"
                )
            elif pattern.lower() not in test_file.read_text(encoding="utf-8").lower():
                errors.append(
                    f"  entry[{i}] ({eid!r}): STALE — reason_pattern {pattern!r} "
                    f"matches nothing in {entry['file']}"
                )
    return errors


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    args = sys.argv[1:]

    if "--validate-inventory" in args:
        # Validate inventory JSON: parse + required fields + ADR-0087 governance
        try:
            data = json.loads(_INVENTORY_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, FileNotFoundError) as e:
            print(f"check-skip-inventory: ERROR — inventory invalid: {e}", file=sys.stderr)
            return 1
        errors = validate_inventory(data)
        if errors:
            print("check-skip-inventory: ERROR — inventory validation failed:", file=sys.stderr)
            for e in errors:
                print(e, file=sys.stderr)
            return 1
        print(
            f"check-skip-inventory: OK — inventory valid ({len(data.get('entries', []))} entries)"
        )
        return 0

    if "--run" in args:
        # Run mode: invoke pytest as subprocess, capture output
        run_idx = args.index("--run")
        pytest_args = args[run_idx + 1 :]
        if not pytest_args:
            print(
                "check-skip-inventory: ERROR: --run requires pytest args",
                file=sys.stderr,
            )
            return 1
        result = subprocess.run(
            [sys.executable, "-m", "pytest"] + pytest_args + ["--tb=no", "-q", "-rs"],
            capture_output=True,
            text=True,
        )
        lines = result.stdout.splitlines()
    else:
        # Pipe mode: read from stdin
        lines = sys.stdin.read().splitlines()

    if not lines:
        # No output / no skips — nothing to check
        print("check-skip-inventory: OK — no skips found")
        return 0

    ok, offenders = check(lines)
    if not ok:
        print(
            "check-skip-inventory: ERROR — unsanctioned skips found.",
            file=sys.stderr,
        )
        print(
            "Add an entry to yadgar/tests/skip_inventory.json or fix the skip gate.",
            file=sys.stderr,
        )
        for o in offenders:
            print(o, file=sys.stderr)
        return 1

    skip_count = len(_extract_skip_reasons(lines))
    if skip_count > 0:
        print(f"check-skip-inventory: OK — {skip_count} skip(s) all sanctioned")
    else:
        print("check-skip-inventory: OK — no skips found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
