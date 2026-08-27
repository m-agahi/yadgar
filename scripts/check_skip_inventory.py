#!/usr/bin/env python3
"""Skip-inventory gate: every skip reason must match a sanctioned entry.

Usage (three modes):
  1. Pipe mode (CI): parse pytest -rs output from stdin
       pytest <paths> -rs --tb=no -q | python scripts/check_skip_inventory.py

  2. Run mode: run pytest and check in one shot
       python scripts/check_skip_inventory.py --run <pytest-args...>

  3. Receipt mode: print this interpreter's measured module availability
       python scripts/check_skip_inventory.py --emit-receipt > pytest-rs-<group>.txt
     Run it in the SAME environment as pytest, BEFORE pytest, appending the
     test output after it (`... | tee -a`).

Exit 0 → all skips sanctioned.
Exit 1 → unsanctioned skip(s) found — prints offenders.

Inventory: yadgar/tests/skip_inventory.json
  Each entry has a "reason_pattern" substring matched against the skip reason.
  A skip is sanctioned if its reason contains any entry's reason_pattern (case-insensitive).
  Multiple skips with different reasons all matching the same pattern are fine.

CONDITIONAL SANCTIONING (task 392)
  An entry may carry an optional "sanctioned_when_module_absent" naming an
  importable module. Such an entry sanctions ONLY while that module is measured
  ABSENT. The defect it closes: `sqlalchemy not installed (sql extra)` was
  sanctioned unconditionally, so after task 380 taught the legs to install
  `--extra sql`, a run that skipped all 211 of those tests passed the gate
  exactly like a run that executed them. A gate that cannot tell "ran and
  passed" from "skipped and passed" is not a gate.

THE EXTRAS RECEIPT
  Availability is MEASURED, never declared. A workflow flag asserting "this
  image has sqlalchemy" would be the same class of lie: Dockerfile.ci has no
  auto-sync pipeline, so the assertion can be false. Instead each test job runs
  `--emit-receipt` in its own interpreter and the line lands in the same
  `pytest-rs-<group>.txt` the gate already reads:

      SKIPGATE-MODULES: alembic=present duckdb=absent sqlalchemy=present

  The gate scans lines IN ORDER, so a receipt governs the skip lines that follow
  it until the next receipt. `cat`-ing several groups' files therefore judges
  each group against its own measured environment, and one leg that installs an
  extra cannot excuse — or condemn — another that does not.

  No receipt in scope → conditional entries fall back to sanctioning, so ad-hoc
  pipes over hand-made text keep working. `--require-receipt` (what CI passes)
  turns a missing receipt into a failure, because a run that emitted none is
  unverifiable in exactly the way this gate exists to prevent.
"""

from __future__ import annotations

import importlib.util
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

# pytest colourises its -rs summary whenever it believes a terminal is attached.
# CI pipes into `tee`, so colour is off there — but the agent harness exports
# FORCE_COLOR=3 (scripts/check_type_ratchet.py:76 documents the same trap), and a
# coloured "SKIPPED" line does not match _SKIP_LINE_RE. The gate then reports
# "no skips found" and exits 0 over output full of skips: the exact
# reports-success-while-seeing-nothing failure this gate exists to prevent.
# Strip the escapes rather than trusting the caller's environment.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(line: str) -> str:
    """Remove SGR escapes so a colourised -rs summary still parses."""
    return _ANSI_RE.sub("", line)


# The extras receipt a test job emits into its own -rs stream (see module docstring).
_RECEIPT_PREFIX = "SKIPGATE-MODULES:"
_RECEIPT_RE = re.compile(rf"^{re.escape(_RECEIPT_PREFIX)}\s*(.*)$")

# The inventory field that makes an entry conditional on a module being ABSENT.
_CONDITION_FIELD = "sanctioned_when_module_absent"


def _load_entries(path: Path = _INVENTORY_PATH) -> list[dict]:
    """Return the raw inventory entries."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return list(data["entries"])


def _load_inventory(path: Path = _INVENTORY_PATH) -> list[str]:
    """Return list of sanctioned reason_pattern strings (lowercase)."""
    return [e["reason_pattern"].lower() for e in _load_entries(path)]


# ---------------------------------------------------------------------------
# Extras receipt — measured module availability
# ---------------------------------------------------------------------------


def probe_modules(names: list[str]) -> dict[str, bool]:
    """Return {module: importable?} measured against THIS interpreter.

    find_spec, not import: it answers the same question `pytest.importorskip`
    answers without paying to import torch-sized dependencies.
    """
    result: dict[str, bool] = {}
    for name in names:
        try:
            result[name] = importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):  # fmt: skip
            # A namespace-package parent that is itself missing raises rather
            # than returning None. Unimportable either way.
            result[name] = False
    return result


def emit_receipt_line(names: list[str]) -> str:
    """Render the receipt line for the given modules, measured here and now."""
    probed = probe_modules(sorted(set(names)))
    payload = " ".join(f"{n}={'present' if ok else 'absent'}" for n, ok in sorted(probed.items()))
    return f"{_RECEIPT_PREFIX} {payload}".rstrip()


def _parse_receipt(line: str) -> dict[str, bool] | None:
    """Parse one receipt line → {module: present?}, or None if not a receipt."""
    m = _RECEIPT_RE.match(line.strip())
    if not m:
        return None
    modules: dict[str, bool] = {}
    for token in m.group(1).split():
        name, _, state = token.partition("=")
        if name and state:
            modules[name] = state.strip().lower() == "present"
    return modules


def _condition_modules(entries: list[dict]) -> list[str]:
    """Every module named by a conditional entry — what a receipt must cover."""
    return sorted({e[_CONDITION_FIELD] for e in entries if e.get(_CONDITION_FIELD)})


def _extract_skip_reasons(lines: list[str]) -> list[tuple[str, str]]:
    """Parse pytest -rs output; return list of (raw_line, reason) tuples."""
    results = []
    for line in lines:
        m = _SKIP_LINE_RE.match(_strip_ansi(line).strip())
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


def _sanctioning_entry(
    reason: str, entries: list[dict], modules: dict[str, bool] | None
) -> tuple[dict | None, list[dict]]:
    """Return (entry that sanctions `reason`, entries that matched but declined).

    An entry matches when its reason_pattern is a case-insensitive substring of
    the reason. A matching entry SANCTIONS unless it is conditional on a module
    the receipt in scope measured PRESENT — in which case it declines, and is
    reported so the offender line can say why.
    """
    declined: list[dict] = []
    for entry in entries:
        if not _is_sanctioned(reason, [entry["reason_pattern"]]):
            continue
        module = entry.get(_CONDITION_FIELD)
        # No receipt in scope, or a receipt that says nothing about this module:
        # availability is unknown, so fall back to sanctioning.
        if module and modules is not None and modules.get(module) is True:
            declined.append(entry)
            continue
        return entry, declined
    return None, declined


def check(
    lines: list[str],
    inventory_path: Path = _INVENTORY_PATH,
    require_receipt: bool = False,
) -> tuple[bool, list[str]]:
    """Check skip reasons in lines against inventory.

    Returns (ok, offenders) where offenders is list of unsanctioned lines.
    Pure function — injectable for testing.

    Lines are walked IN ORDER: a `SKIPGATE-MODULES:` receipt governs every skip
    line after it until the next receipt, so concatenated per-group -rs files
    are each judged against their own measured environment.

    require_receipt=True additionally fails when the stream carries no receipt
    at all — an unverifiable run, which is the hole this gate exists to close.
    """
    entries = _load_entries(inventory_path)
    offenders: list[str] = []
    modules: dict[str, bool] | None = None
    saw_receipt = False
    saw_content = False

    for raw in lines:
        line = _strip_ansi(raw).strip()
        if not line:
            continue
        saw_content = True
        receipt = _parse_receipt(line)
        if receipt is not None:
            modules = receipt
            saw_receipt = True
            continue
        m = _SKIP_LINE_RE.match(line)
        if not m:
            continue
        reason = m.group(1).strip()
        entry, declined = _sanctioning_entry(reason, entries, modules)
        if entry is not None:
            continue
        if declined:
            names = ", ".join(sorted({d[_CONDITION_FIELD] for d in declined}))
            ids = ", ".join(d["id"] for d in declined)
            offenders.append(
                f"  UNSANCTIONED ({names} measured PRESENT — inventory entry "
                f"{ids} sanctions this reason only while it is ABSENT, so these "
                f"tests skipped on a leg that installed the dependency): {raw.rstrip()}"
            )
        else:
            offenders.append(f"  UNSANCTIONED: {raw.rstrip()}")

    if require_receipt and saw_content and not saw_receipt:
        offenders.append(
            "  NO EXTRAS RECEIPT: the output carries no "
            f"'{_RECEIPT_PREFIX}' line, so which optional dependencies were "
            "installed is unknown and 'ran and passed' cannot be told apart "
            "from 'skipped and passed'. Emit one with "
            "`python scripts/check_skip_inventory.py --emit-receipt` in the "
            "test job, before pytest, into the same file."
        )
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
      - the optional "sanctioned_when_module_absent" must name an importable
        module (dotted identifier), since the receipt parser keys on it
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
        if _CONDITION_FIELD in entry:
            module = entry.get(_CONDITION_FIELD)
            if not isinstance(module, str) or not module:
                errors.append(
                    f"  entry[{i}] ({eid!r}): {_CONDITION_FIELD} must be a non-empty module name"
                )
            elif not all(part.isidentifier() for part in module.split(".")):
                errors.append(
                    f"  entry[{i}] ({eid!r}): {_CONDITION_FIELD} {module!r} is not a "
                    "valid module name — the extras receipt keys on an importable module"
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


def _cmd_emit_receipt() -> int:
    """--emit-receipt: print module availability measured in THIS interpreter."""
    try:
        entries = _load_entries()
    except (json.JSONDecodeError, FileNotFoundError, KeyError) as exc:
        print(f"check-skip-inventory: ERROR — inventory unreadable: {exc}", file=sys.stderr)
        return 1
    print(emit_receipt_line(_condition_modules(entries)))
    return 0


def _cmd_validate_inventory() -> int:
    """--validate-inventory: parse + required fields + ADR-0087 governance."""
    try:
        data = json.loads(_INVENTORY_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, FileNotFoundError) as exc:
        print(f"check-skip-inventory: ERROR — inventory invalid: {exc}", file=sys.stderr)
        return 1
    errors = validate_inventory(data)
    if errors:
        print("check-skip-inventory: ERROR — inventory validation failed:", file=sys.stderr)
        for err in errors:
            print(err, file=sys.stderr)
        return 1
    print(f"check-skip-inventory: OK — inventory valid ({len(data.get('entries', []))} entries)")
    return 0


def _echo_receipts(lines: list[str]) -> None:
    """Print every receipt found, so the gate's log answers 'did they RUN?'."""
    receipts = [r for r in (_parse_receipt(_strip_ansi(ln)) for ln in lines) if r is not None]
    for receipt in receipts:
        rendered = " ".join(
            f"{n}={'present' if present else 'absent'}" for n, present in sorted(receipt.items())
        )
        print(f"check-skip-inventory: extras receipt — {rendered}")
    if not receipts:
        print("check-skip-inventory: WARNING — no extras receipt in input", file=sys.stderr)


def main() -> int:
    args = sys.argv[1:]

    if "--emit-receipt" in args:
        return _cmd_emit_receipt()

    require_receipt = "--require-receipt" in args
    if require_receipt:
        args = [a for a in args if a != "--require-receipt"]

    if "--validate-inventory" in args:
        return _cmd_validate_inventory()

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
            # --color=no: this parses pytest's text, and a colourised summary
            # would not match _SKIP_LINE_RE (see _ANSI_RE). Belt and braces —
            # _strip_ansi handles text that arrives coloured anyway.
            [sys.executable, "-m", "pytest"] + pytest_args + ["--tb=no", "-q", "-rs", "--color=no"],
            capture_output=True,
            text=True,
        )
        lines = result.stdout.splitlines()
    else:
        # Pipe mode: read from stdin
        lines = sys.stdin.read().splitlines()

    if not lines:
        if require_receipt:
            print(
                "check-skip-inventory: ERROR — empty input under --require-receipt; "
                "a run that produced no output produced no extras receipt either.",
                file=sys.stderr,
            )
            return 1
        # No output / no skips — nothing to check
        print("check-skip-inventory: OK — no skips found")
        return 0

    ok, offenders = check(lines, require_receipt=require_receipt)
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

    _echo_receipts(lines)

    skip_count = len(_extract_skip_reasons(lines))
    if skip_count > 0:
        print(f"check-skip-inventory: OK — {skip_count} skip(s) all sanctioned")
    else:
        print("check-skip-inventory: OK — no skips found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
