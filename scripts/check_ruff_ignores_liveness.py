#!/usr/bin/env python3
"""Guard: `pyproject.toml` `[tool.ruff.lint.per-file-ignores]` entries must be live.

WHY THIS EXISTS (Car 7 allowlist-debt audit, 2026-08-13)
----------------------------------------------------------
NO GUARD existed for this table before this script. It rotted to 23 of 28
path entries (25 of 30 (path, code) pairs — two rotten lines each named two
codes) before anyone re-verified it against the repo's own ruff thresholds
(mccabe max-complexity=15, pylint max-args=8):

  * 5 lines (6 pairs) named files under `yadgar/_shared/retrieval/` that had
    already moved to `backend/retrieval` — ruff silently drops a
    per-file-ignores entry whose path matches nothing, so these were dead
    weight with zero signal.
  * 18 more lines (19 pairs) named files that ruff re-checks CLEAN at the
    repo's thresholds without the ignore at all — each one silently
    suppressed the NEXT violation of that code in that file, with no signal
    either.

Two failure classes, both silent, both caught only by a from-scratch manual
audit. This script makes both classes a HARD pre-commit failure so the table
cannot rot back to that state unseen.

WHAT THIS CHECKS
----------------
  (a) EXISTENCE — every `path` in the table must exist on disk. A dead path
      is the "moved/deleted file" class.
  (b) LIVENESS — every `(path, code)` pair must still be a real ruff
      violation at the repo's OWN configured thresholds, with
      `per-file-ignores` cleared. A pair ruff no longer flags is the
      "vacuous entry" class — the code was fixed (or never really needed
      the ignore) and nobody removed the entry.

THE ONE-SUBPROCESS DESIGN
--------------------------
Codes are NOT hardcoded (`C901`/`PLR0913` today, but a future entry could
name any select-list rule) — the `--select` set is derived from whatever
codes actually appear in the table. One `ruff check` subprocess runs over
`yadgar/` with `per-file-ignores` cleared and the derived `--select`; its
JSON output is the full "what's live" set. Per-entry subprocesses would be
O(entries) ruff invocations for no extra signal.

CEILING — read before trusting a green run
--------------------------------------------
This is a liveness check, not a decomposition mandate. A KEPT entry still
means "this file is allowed to violate this cap forever" — the header
comment in `pyproject.toml` says not to add new entries; this script only
stops old ones from surviving past their sell-by date unnoticed.

Exit codes:
  0  every entry exists and is still a real violation
  1  one or more entries are dead-path or vacuous
"""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT = _REPO_ROOT / "pyproject.toml"


def load_per_file_ignores(pyproject_path: Path) -> dict[str, list[str]]:
    """Return {relative_path: [codes]} from [tool.ruff.lint.per-file-ignores]."""
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    table = data.get("tool", {}).get("ruff", {}).get("lint", {}).get("per-file-ignores", {})
    return {path: list(codes) for path, codes in table.items()}


def check_existence(entries: dict[str, list[str]], repo_root: Path) -> list[str]:
    """(a) Every path must exist on disk."""
    errors = []
    for path in sorted(entries):
        if not (repo_root / path).is_file():
            errors.append(
                f"DEAD-PATH: pyproject.toml per-file-ignores names {path!r}, "
                "which does not exist — ruff silently drops this entry. Remove it."
            )
    return errors


def collect_live_violations(codes: set[str], repo_root: Path) -> set[tuple[str, str]]:
    """Run ruff once with per-file-ignores cleared; return the observed (path, code) set."""
    if not codes:
        return set()
    cmd = [
        "uv",
        "run",
        "ruff",
        "check",
        f"--select={','.join(sorted(codes))}",
        "--config",
        "lint.per-file-ignores={}",
        "--output-format",
        "json",
        "yadgar/",
    ]
    proc = subprocess.run(  # noqa: S603 — fixed argv, no shell, no untrusted input
        cmd, cwd=repo_root, capture_output=True, text=True, check=False
    )
    # ruff exits 1 when violations are found — that's the expected path here,
    # not a failure of this script. A genuinely broken invocation (exit >1,
    # or unparseable stdout) IS a failure of this script.
    if proc.returncode not in (0, 1):
        raise RuntimeError(f"ruff check exited {proc.returncode} unexpectedly:\n{proc.stderr}")
    try:
        violations = json.loads(proc.stdout)
    except json.JSONDecodeError as e:  # pragma: no cover - defensive
        raise RuntimeError(f"could not parse ruff JSON output: {e}\n{proc.stdout[:500]}") from e

    live: set[tuple[str, str]] = set()
    for v in violations:
        filename = Path(v["filename"])
        try:
            rel = filename.relative_to(repo_root).as_posix()
        except ValueError:  # pragma: no cover - defensive
            rel = str(filename)
        live.add((rel, v["code"]))
    return live


def check_liveness(
    entries: dict[str, list[str]], live: set[tuple[str, str]], repo_root: Path
) -> list[str]:
    """(b) Every (path, code) pair must still be a real current violation."""
    errors = []
    for path in sorted(entries):
        if not (repo_root / path).is_file():
            continue  # already reported by check_existence
        for code in entries[path]:
            if (path, code) not in live:
                errors.append(
                    f"VACUOUS: pyproject.toml per-file-ignores allows {code} in {path!r}, "
                    "but ruff no longer flags that violation there at the repo's own "
                    "thresholds — the entry silences nothing today and hides the NEXT "
                    "violation of that code in that file with no signal. Remove it."
                )
    return errors


def check(repo_root: Path) -> list[str]:
    entries = load_per_file_ignores(repo_root / "pyproject.toml")
    errors = check_existence(entries, repo_root)

    codes = {code for codes in entries.values() for code in codes}
    live = collect_live_violations(codes, repo_root)
    errors.extend(check_liveness(entries, live, repo_root))
    return errors


def main(argv: list[str] | None = None) -> int:
    del argv  # no CLI args today — kept for parity with sibling check_*.py scripts
    errors = check(_REPO_ROOT)
    if errors:
        print("ruff per-file-ignores liveness check FAILED:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print("ruff per-file-ignores liveness check OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
