#!/usr/bin/env python3
"""`make ci-local` / `.github/workflows/ci-pr.yml` parity guard (Car F5, PR #40 postmortem).

WHY THIS EXISTS
----------------
PR #40 shipped on a green ``make e2e`` — a target that runs
``yadgar/tests/e2e/ -m e2e``, a directory and marker completely disjoint from
what CI actually gates a PR on. CI then found 49 failures ``make e2e`` never
touched. ``make ci-local`` (Makefile) exists to give a real local
reproduction of CI's selection: the union of the four subsystem jobs
(test-fast, test-shared, test-backend, test-core) in ``ci-pr.yml``.

A ``make ci-local`` that is a hand-copied SNAPSHOT of those jobs' dirs and
marker would itself silently rot the next time someone edits a job without
updating the Makefile — the exact bug class this whole car exists to close.
This script is the tripwire: it parses ``ci-pr.yml`` and the ``Makefile``
INDEPENDENTLY and fails loudly the moment they disagree, instead of trusting
either file to have kept the other honest.

WHAT IT COMPARES
-----------------
  * directories — every ``yadgar/tests/<x>/`` path referenced in each of the
    4 subsystem jobs' "Run tests ..." step, unioned, vs. the ``CI_LOCAL_DIRS``
    make variable.
  * marker expression — the ``-m '...'`` string each of those 4 jobs passes
    to pytest (asserted identical across all 4 first; if that assumption
    ever breaks this fails loudly rather than silently picking one), vs. the
    ``CI_LOCAL_MARKER`` make variable.
  * recipe consumption — that the ``ci-local:`` recipe body actually
    references ``CI_LOCAL_TEST_DIRS``/``CI_LOCAL_DIRS`` and
    ``CI_LOCAL_MARKER`` rather than a hardcoded path list or marker string.
    The two checks above only validate the variable DECLARATIONS; without
    this one, a correct declaration could sit next to a recipe that quietly
    ignores it.

NOT compared (out of scope, on purpose — see the Makefile's `ci-local`
comment): ``-n``/``--dist``/``--reruns`` flags (parallelism, not selection);
test-core's ``--splits``/``--group`` sharding (a local run covers the union
of all splits anyway); the separate test-perf, viz-tests, and
invariant-checks jobs (different suites entirely).

Usage:
  python scripts/check_ci_local_parity.py                        # check, exit 0/1
  python scripts/check_ci_local_parity.py --workflow P --makefile P

Exit codes:
  0  ci-local's dirs + marker match the union of the 4 CI subsystem jobs
  1  divergence found, or either file could not be parsed as expected
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci-pr.yml"
_MAKEFILE = _REPO_ROOT / "Makefile"

# The 4 subsystem jobs whose union `make ci-local` must reproduce. test-perf,
# viz-tests, and invariant-checks are deliberately excluded — see docstring.
_SUBSYSTEM_JOBS = ("test-fast", "test-shared", "test-backend", "test-core")

# Deliberately stdlib-only (no ruamel/pyyaml): a pre-commit `language: system`
# hook runs whatever `python`/`python3` resolves to on PATH at commit time,
# which is not guaranteed to be this repo's uv-managed .venv where third-party
# deps live (see ADR-0218's `lint-imports` PATH note). Every other check_*.py
# in scripts/ is stdlib-only for the same reason. Job blocks are top-level
# `  <name>:` keys under `jobs:` (2-space indent, GitHub Actions requires this
# shape) — text-sliced between consecutive headers rather than fully parsed.
_JOB_HEADER_RE = re.compile(r"^  ([A-Za-z0-9_-]+):[ \t]*$", re.MULTILINE)
_DIR_RE = re.compile(r"yadgar/tests/[\w\-]+/")
_MARKER_RE = re.compile(r"-m\s+(['\"])(.*?)\1")


def _job_blocks(jobs_section: str) -> dict[str, str]:
    """Slice *jobs_section* (text starting at 'jobs:') into {job_name: block_text}."""
    headers = [(m.start(), m.group(1)) for m in _JOB_HEADER_RE.finditer(jobs_section)]
    blocks: dict[str, str] = {}
    for i, (pos, name) in enumerate(headers):
        end = headers[i + 1][0] if i + 1 < len(headers) else len(jobs_section)
        blocks[name] = jobs_section[pos:end]
    return blocks


def workflow_selection(workflow_path: Path) -> tuple[set[str], dict[str, str]]:
    """Return (union of test dirs, {job_name: marker}) for the 4 subsystem jobs."""
    text = workflow_path.read_text(encoding="utf-8")
    jobs_idx = text.find("\njobs:")
    if jobs_idx == -1:
        raise ValueError("no top-level 'jobs:' key found")
    blocks = _job_blocks(text[jobs_idx:])

    dirs: set[str] = set()
    markers: dict[str, str] = {}
    for job_name in _SUBSYSTEM_JOBS:
        block = blocks.get(job_name)
        if block is None:
            raise ValueError(f"workflow job {job_name!r} not found")
        found_dirs = set(_DIR_RE.findall(block))
        if not found_dirs:
            raise ValueError(f"no yadgar/tests/.../ paths found in {job_name}'s block")
        dirs |= found_dirs
        m = _MARKER_RE.search(block)
        if not m:
            raise ValueError(f"no -m '...' marker found in {job_name}'s block")
        markers[job_name] = m.group(2)
    return dirs, markers


def makefile_selection(makefile_path: Path) -> tuple[set[str], str]:
    """Return (CI_LOCAL_DIRS set, CI_LOCAL_MARKER string) declared in the Makefile."""
    text = makefile_path.read_text(encoding="utf-8")

    dirs_start = text.find("CI_LOCAL_DIRS")
    if dirs_start == -1:
        raise ValueError(
            "CI_LOCAL_DIRS not found — has the `ci-local` target been removed or renamed?"
        )
    marker_start = text.find("CI_LOCAL_MARKER", dirs_start)
    if marker_start == -1:
        raise ValueError(
            "CI_LOCAL_MARKER not found — has the `ci-local` target been removed or renamed?"
        )

    dirs_block = text[dirs_start:marker_start]
    dirs = set(_DIR_RE.findall(dirs_block))
    if not dirs:
        raise ValueError("CI_LOCAL_DIRS block contains no yadgar/tests/.../ paths")

    marker_line_end = text.find("\n", marker_start)
    marker_line = text[marker_start : marker_line_end if marker_line_end != -1 else None]
    if ":=" not in marker_line:
        raise ValueError("CI_LOCAL_MARKER line has no ':=' assignment")
    marker = marker_line.split(":=", 1)[1].strip()

    return dirs, marker


# Names the `ci-local:` recipe body may reference for the directory list. Two
# names because the recipe reads the origin-guarded CI_LOCAL_TEST_DIRS (see
# the Makefile's DIRS-collision comment), not CI_LOCAL_DIRS directly — but
# either counts as "consumes the declared selection" for this check.
_DIRS_VAR_REFS = ("$(CI_LOCAL_TEST_DIRS)", "$(CI_LOCAL_DIRS)")
_MARKER_VAR_REF = "$(CI_LOCAL_MARKER)"


def makefile_recipe_consumes_vars(makefile_path: Path) -> tuple[bool, bool]:
    """Return (invocation_uses_a_dirs_var, invocation_uses_marker_var) for `ci-local:`.

    Guards against the declared CI_LOCAL_DIRS/CI_LOCAL_MARKER staying correct
    while the recipe itself is edited to hardcode something else (e.g. `pytest
    yadgar/tests/_shared/`) — the parity check above only validates the
    variable DECLARATIONS, not that the recipe actually consumes them.

    Scoped to the substring starting at the first ``pytest`` in the recipe,
    NOT the whole recipe body: the empty-DIRS guard line (``test -n
    "$(strip $(CI_LOCAL_TEST_DIRS))" || ...``) always references
    CI_LOCAL_TEST_DIRS regardless of what the actual pytest invocation does,
    so scanning the whole recipe would report "consumed" even when the pytest
    command line itself was edited to hardcode a path — the exact hole this
    function exists to close, caught only by narrowing to the invocation.
    """
    text = makefile_path.read_text(encoding="utf-8")
    m = re.search(r"^ci-local:[ \t]*$", text, re.MULTILINE)
    if not m:
        raise ValueError("no `ci-local:` target found")
    recipe_lines: list[str] = []
    for line in text[m.end() :].splitlines():
        if line.startswith("\t"):
            recipe_lines.append(line)
        elif line.strip() == "":
            continue
        else:
            break
    recipe = "\n".join(recipe_lines)
    if not recipe:
        raise ValueError("`ci-local:` target has an empty recipe")
    pytest_idx = recipe.find("pytest")
    if pytest_idx == -1:
        raise ValueError("`ci-local:` recipe contains no 'pytest' invocation")
    invocation = recipe[pytest_idx:]
    uses_dirs = any(ref in invocation for ref in _DIRS_VAR_REFS)
    uses_marker = _MARKER_VAR_REF in invocation
    return uses_dirs, uses_marker


def check(workflow_path: Path, makefile_path: Path) -> list[str]:
    """Return a list of violation strings (empty = clean)."""
    try:
        wf_dirs, wf_markers = workflow_selection(workflow_path)
    except ValueError as exc:
        return [f"WORKFLOW PARSE ERROR ({workflow_path}): {exc}"]
    try:
        mk_dirs, mk_marker = makefile_selection(makefile_path)
    except ValueError as exc:
        return [f"MAKEFILE PARSE ERROR ({makefile_path}): {exc}"]

    errors: list[str] = []

    distinct_markers = set(wf_markers.values())
    if len(distinct_markers) != 1:
        return [
            "the 4 CI subsystem jobs no longer share ONE marker expression "
            f"({wf_markers!r}) — this script's single-marker assumption is invalid. "
            "Update check_ci_local_parity.py to compare per-job (and reconsider "
            "whether `make ci-local` can still be one pytest invocation)."
        ]
    wf_marker = distinct_markers.pop()

    missing_in_makefile = sorted(wf_dirs - mk_dirs)
    extra_in_makefile = sorted(mk_dirs - wf_dirs)
    if missing_in_makefile:
        errors.append(
            f"ci-pr.yml's subsystem jobs test {missing_in_makefile} but Makefile's "
            "CI_LOCAL_DIRS does not — `make ci-local` is BLIND to these directories. "
            "Add them to CI_LOCAL_DIRS."
        )
    if extra_in_makefile:
        errors.append(
            f"Makefile's CI_LOCAL_DIRS tests {extra_in_makefile} but no CI subsystem "
            "job does — remove them, or confirm a CI job now covers them and this "
            "script's job list needs updating."
        )
    if wf_marker != mk_marker:
        errors.append(
            f"marker mismatch: ci-pr.yml's subsystem jobs use {wf_marker!r}, "
            f"Makefile's CI_LOCAL_MARKER is {mk_marker!r}."
        )

    try:
        uses_dirs, uses_marker = makefile_recipe_consumes_vars(makefile_path)
    except ValueError as exc:
        return [*errors, f"MAKEFILE PARSE ERROR ({makefile_path}): {exc}"]
    if not uses_dirs:
        errors.append(
            "the `ci-local:` recipe does not reference "
            f"{' or '.join(_DIRS_VAR_REFS)} — CI_LOCAL_DIRS can be perfectly correct "
            "while the recipe itself runs something else entirely. Have the recipe "
            "consume the declared variable instead of a hardcoded path list."
        )
    if not uses_marker:
        errors.append(
            f"the `ci-local:` recipe does not reference {_MARKER_VAR_REF} — "
            "CI_LOCAL_MARKER can be perfectly correct while the recipe passes a "
            "different -m expression. Have the recipe consume the declared variable."
        )
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail if `make ci-local`'s test selection diverges from ci-pr.yml's subsystem jobs"
    )
    parser.add_argument("--workflow", default=str(_WORKFLOW), help="Path to ci-pr.yml")
    parser.add_argument("--makefile", default=str(_MAKEFILE), help="Path to the Makefile")
    args = parser.parse_args(argv)

    errors = check(Path(args.workflow), Path(args.makefile))
    if errors:
        print("ci-local / ci-pr.yml parity check FAILED:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print("ci-local / ci-pr.yml parity check OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
