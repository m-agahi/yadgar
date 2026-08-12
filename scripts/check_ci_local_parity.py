#!/usr/bin/env python3
"""`make ci-local` / `.github/workflows/ci-pr.yml` parity guard (Car F5, extended Car F10).

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
This script is the tripwire: it parses ``ci-pr.yml``, the ``Makefile``, and
(as of Car F10) ``scripts/ci-local-legs.sh`` INDEPENDENTLY and fails loudly
the moment they disagree, instead of trusting any of them to have kept the
others honest.

CAR F10 — WHY DIRS/MARKER PARITY WASN'T ENOUGH
------------------------------------------------
Car F5's original guard compared only the directory union and the marker
expression. It could not see the defect Car F10 fixed: ``make ci-local`` ran
the union as ONE pytest process, while CI runs it as FOUR SEPARATE processes
(one per subsystem job/container). A single-process target and a four-process
target with an IDENTICAL union+marker look identical to a dirs/marker-only
comparison — the lumped invocation accumulated memory no single CI job ever
does and got OOM-killed on a real box. This script now ALSO pins the LEG
STRUCTURE: that there is exactly one leg (one ``run_leg`` call in
scripts/ci-local-legs.sh, fed by one ``CI_LOCAL_DIRS_<leg>`` Makefile
variable) per CI subsystem job — so collapsing the legs back into one lumped
invocation, or a fifth CI job appearing with no matching leg, both fail this
script instead of shipping silently.

WHAT IT COMPARES
-----------------
  * directories — every ``yadgar/tests/<x>/`` path referenced in each
    discovered subsystem job's "Run tests ..." step, unioned, vs. the union
    of the Makefile's ``CI_LOCAL_DIRS_<leg>`` variables.
  * marker expression — the ``-m '...'`` string each subsystem job passes to
    pytest (asserted identical across all of them first; if that assumption
    ever breaks this fails loudly rather than silently picking one), vs. the
    ``CI_LOCAL_MARKER`` make variable.
  * leg structure (Car F10) — that the SET of subsystem jobs discovered in
    ci-pr.yml matches, 1:1, the set of ``CI_LOCAL_DIRS_<leg>`` Makefile
    variables AND the set of ``run_leg <leg> ...`` calls in
    scripts/ci-local-legs.sh, with each leg's dirs matching its job's dirs
    EXACTLY (not just contributing to the same union) and each ``run_leg``
    call wired to the matching ``CI_LOCAL_DIRS_<leg>`` variable.
  * recipe consumption — that the ``ci-local:`` recipe body actually
    references every ``CI_LOCAL_DIRS_<leg>``/``CI_LOCAL_MARKER`` variable and
    delegates to ``scripts/ci-local-legs.sh``, rather than a hardcoded path
    list or a recipe that quietly ignores the declared legs.

SUBSYSTEM JOB DISCOVERY IS SHAPE-BASED, NOT A HARDCODED NAME LIST
-------------------------------------------------------------------
A job counts as a "subsystem leg job" iff its block contains at least one
``yadgar/tests/.../`` path AND a QUOTED ``-m '...'``/``-m "..."`` marker.
This is deliberate (Car F10): a hardcoded ``("test-fast", "test-shared", ...)``
name list, as the original guard used, cannot see a FIFTH subsystem job
someone adds later — it just isn't in the list, so ``make ci-local`` stays
silently blind to it and this script never even looks. Shape-based discovery
picks up a same-shaped fifth job automatically and then requires a matching
leg, so the omission fails loudly instead. ``test-perf`` (``-m perf``,
unquoted) and ``viz-tests``' Layer 2 (``-m integration``, unquoted) are
excluded BY THIS SHAPE, not by name: neither has a quoted marker. Jobs with
no pytest ``-m`` flag at all (check-skip-inventory, invariant-checks,
test-gate, verify-version-bump) are excluded the same way.

NOT compared (out of scope, on purpose — see the Makefile's `ci-local`
comment): ``-n``/``--dist``/``--reruns`` flags (parallelism, not selection);
test-core's ``--splits``/``--group`` sharding (a local run covers the union
of all splits anyway); the separate test-perf, viz-tests, and
invariant-checks jobs (different suites entirely).

Usage:
  python scripts/check_ci_local_parity.py                        # check, exit 0/1
  python scripts/check_ci_local_parity.py --workflow P --makefile P --leg-runner P

Exit codes:
  0  ci-local's dirs, marker, and leg structure match the CI subsystem jobs
  1  divergence found, or a file could not be parsed as expected
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci-pr.yml"
_MAKEFILE = _REPO_ROOT / "Makefile"
_LEG_RUNNER = _REPO_ROOT / "scripts" / "ci-local-legs.sh"

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


def discover_subsystem_jobs(workflow_path: Path) -> dict[str, tuple[set[str], str]]:
    """Return {job_name: (dirs, marker)} for every CI job shaped like a
    "subsystem leg" job — see the module docstring's discovery section.
    """
    text = workflow_path.read_text(encoding="utf-8")
    jobs_idx = text.find("\njobs:")
    if jobs_idx == -1:
        raise ValueError("no top-level 'jobs:' key found")
    blocks = _job_blocks(text[jobs_idx:])

    found: dict[str, tuple[set[str], str]] = {}
    for job_name, block in blocks.items():
        dirs = set(_DIR_RE.findall(block))
        m = _MARKER_RE.search(block)
        if dirs and m:
            found[job_name] = (dirs, m.group(2))
    return found


def leg_name_for_job(job_name: str) -> str:
    """Derive a leg name from a CI job name: strip a leading 'test-' if present.

    This is a NAMING convention only (Makefile/script leg identifiers read
    better as 'fast' than 'test-fast') — it plays no role in job DISCOVERY,
    which is shape-based (see discover_subsystem_jobs). A subsystem job not
    named ``test-<x>`` still gets discovered; its leg name is just the job
    name unchanged.
    """
    return job_name[len("test-") :] if job_name.startswith("test-") else job_name


def workflow_selection(workflow_path: Path) -> tuple[set[str], dict[str, str]]:
    """Return (union of test dirs, {job_name: marker}) for the discovered subsystem jobs."""
    jobs = discover_subsystem_jobs(workflow_path)
    if not jobs:
        raise ValueError(
            "no subsystem-shaped jobs discovered under 'jobs:' (a job needs >=1 "
            "yadgar/tests/.../ path AND a quoted -m '...' marker to count) — has "
            "ci-pr.yml's job shape changed out from under this discovery?"
        )
    dirs: set[str] = set()
    markers: dict[str, str] = {}
    for job_name, (job_dirs, marker) in jobs.items():
        dirs |= job_dirs
        markers[job_name] = marker
    return dirs, markers


def makefile_marker(makefile_path: Path) -> str:
    """Return the ``CI_LOCAL_MARKER := ...`` value declared in the Makefile."""
    text = makefile_path.read_text(encoding="utf-8")
    m = re.search(r"^CI_LOCAL_MARKER\s*:=\s*(.*)$", text, re.MULTILINE)
    if not m:
        raise ValueError(
            "CI_LOCAL_MARKER not found — has the `ci-local` target been removed or renamed?"
        )
    return m.group(1).strip()


def makefile_legs(makefile_path: Path) -> dict[str, set[str]]:
    """Return {leg_name: dirs} for every ``CI_LOCAL_DIRS_<leg> := ...`` line in the Makefile."""
    text = makefile_path.read_text(encoding="utf-8")
    legs: dict[str, set[str]] = {}
    for m in re.finditer(r"^CI_LOCAL_DIRS_([A-Za-z0-9_]+)\s*:=\s*(.*)$", text, re.MULTILINE):
        leg_name, value = m.group(1), m.group(2)
        dirs = set(_DIR_RE.findall(value))
        if not dirs:
            raise ValueError(f"CI_LOCAL_DIRS_{leg_name} declares no yadgar/tests/.../ paths")
        legs[leg_name] = dirs
    if not legs:
        raise ValueError(
            "no CI_LOCAL_DIRS_<leg> variables found — has the `ci-local` target's "
            "leg structure been removed, renamed, or collapsed back into one "
            "CI_LOCAL_DIRS variable?"
        )
    return legs


def ci_local_recipe_text(makefile_path: Path) -> str:
    """Return the `ci-local:` target's recipe body (tab-indented lines following it)."""
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
    return recipe


def leg_runner_legs(leg_runner_path: Path) -> list[str]:
    """Return the (possibly duplicated) list of leg names ``run_leg <name> ...``
    is called with in scripts/ci-local-legs.sh, EXCLUDING the synthetic
    'override' leg (the DIRS= mid-work path — not tied to any CI job, so it
    plays no part in the CI-parity comparison).

    This is the check that actually pins the LEG STRUCTURE (Car F10): if the
    per-job ``run_leg`` calls are ever collapsed back into a single lumped
    invocation — the exact regression this car fixes — the set of names
    returned here shrinks and no longer matches the jobs discovered from
    ci-pr.yml.
    """
    if not leg_runner_path.exists():
        raise ValueError(f"leg runner not found: {leg_runner_path}")
    text = leg_runner_path.read_text(encoding="utf-8")
    names = [m.group(1) for m in re.finditer(r"^\s*run_leg\s+(\S+)", text, re.MULTILINE)]
    return [n for n in names if n != "override"]


def leg_runner_wires_dirs_var(leg_runner_path: Path, leg_name: str) -> bool:
    """Confirm the ``run_leg <leg_name> ...`` call line references
    ``CI_LOCAL_DIRS_<leg_name>`` — catches the sneaky variant where a leg
    name stays correct but is wired to the WRONG per-leg dirs variable
    (e.g. `run_leg fast $CI_LOCAL_DIRS_backend`).
    """
    text = leg_runner_path.read_text(encoding="utf-8")
    for line in text.splitlines():
        if re.match(rf"^\s*run_leg\s+{re.escape(leg_name)}\b", line):
            return f"CI_LOCAL_DIRS_{leg_name}" in line
    return False


def _check_dirs_and_marker(
    wf_dirs: set[str], mk_dirs: set[str], wf_marker: str, mk_marker: str
) -> list[str]:
    """Union-of-dirs + marker parity — the original Car F5 comparison."""
    errors: list[str] = []
    missing_in_makefile = sorted(wf_dirs - mk_dirs)
    extra_in_makefile = sorted(mk_dirs - wf_dirs)
    if missing_in_makefile:
        errors.append(
            f"ci-pr.yml's subsystem jobs test {missing_in_makefile} but the Makefile's "
            "CI_LOCAL_DIRS_<leg> variables do not — `make ci-local` is BLIND to these "
            "directories. Add them to the right leg."
        )
    if extra_in_makefile:
        errors.append(
            f"the Makefile's CI_LOCAL_DIRS_<leg> variables test {extra_in_makefile} but no "
            "CI subsystem job does — remove them, or confirm a CI job now covers them and "
            "this script's discovery needs updating."
        )
    if wf_marker != mk_marker:
        errors.append(
            f"marker mismatch: ci-pr.yml's subsystem jobs use {wf_marker!r}, "
            f"Makefile's CI_LOCAL_MARKER is {mk_marker!r}."
        )
    return errors


def _check_leg_boundaries(
    expected_legs: dict[str, set[str]], mk_legs: dict[str, set[str]]
) -> list[str]:
    """Leg structure (Car F10), Makefile side: one CI_LOCAL_DIRS_<leg> per CI
    subsystem job, each matching that job's dirs EXACTLY (not just
    contributing to the same union — see _check_dirs_and_marker for that)."""
    errors: list[str] = []
    missing_legs = sorted(set(expected_legs) - set(mk_legs))
    extra_legs = sorted(set(mk_legs) - set(expected_legs))
    if missing_legs:
        errors.append(
            f"ci-pr.yml has subsystem job(s) {missing_legs!r} (by leg name) with no matching "
            "CI_LOCAL_DIRS_<leg> in the Makefile — `make ci-local`'s leg structure does not "
            "cover every CI subsystem job. Add the missing leg."
        )
    if extra_legs:
        errors.append(
            f"the Makefile declares CI_LOCAL_DIRS_<leg> for leg(s) {extra_legs!r} with no "
            "matching CI subsystem job — remove them, or confirm ci-pr.yml still has a "
            "matching job and this script's discovery needs updating."
        )
    for leg in sorted(set(expected_legs) & set(mk_legs)):
        if expected_legs[leg] != mk_legs[leg]:
            errors.append(
                f"leg {leg!r}: ci-pr.yml's job tests {sorted(expected_legs[leg])} but the "
                f"Makefile's CI_LOCAL_DIRS_{leg} tests {sorted(mk_legs[leg])} — the LEG "
                "BOUNDARY has drifted even though the total union may still match."
            )
    return errors


def _check_recipe_consumption(recipe: str, mk_legs: dict[str, set[str]]) -> list[str]:
    """Declaring CI_LOCAL_DIRS_<leg>/CI_LOCAL_MARKER correctly is not enough —
    the `ci-local:` recipe must actually hand them to the leg runner."""
    errors: list[str] = []
    if "ci-local-legs.sh" not in recipe:
        errors.append(
            "the `ci-local:` recipe no longer delegates to scripts/ci-local-legs.sh — "
            "cannot verify the legs run as separate processes."
        )
    if "$(CI_LOCAL_MARKER)" not in recipe:
        errors.append(
            "the `ci-local:` recipe does not reference $(CI_LOCAL_MARKER) — "
            "CI_LOCAL_MARKER can be perfectly correct while the recipe passes something else."
        )
    for leg in sorted(mk_legs):
        ref = f"$(CI_LOCAL_DIRS_{leg})"
        if ref not in recipe:
            errors.append(
                f"the `ci-local:` recipe does not reference {ref} — CI_LOCAL_DIRS_{leg} can "
                "be perfectly correct while the recipe never hands it to the leg runner."
            )
    return errors


def _check_script_legs(
    expected_legs: dict[str, set[str]], script_legs: list[str], leg_runner_path: Path
) -> list[str]:
    """Leg structure (Car F10), leg-runner side: one `run_leg` call per CI
    subsystem job, each wired to the matching CI_LOCAL_DIRS_<leg> variable.
    This is the check that actually catches "legs collapsed back into one
    lumped invocation" — the regression this car fixed."""
    errors: list[str] = []
    if len(script_legs) <= 1 and len(expected_legs) > 1:
        errors.append(
            f"scripts/ci-local-legs.sh calls `run_leg` {len(script_legs)} time(s) "
            f"({script_legs!r}) but ci-pr.yml has {len(expected_legs)} subsystem jobs — the "
            "legs have been collapsed back into one lumped invocation, the exact regression "
            "Car F10 fixed. Restore one `run_leg` call per CI subsystem job."
        )

    missing_in_script = sorted(set(expected_legs) - set(script_legs))
    extra_in_script = sorted(set(script_legs) - set(expected_legs))
    duplicate_in_script = sorted({n for n in script_legs if script_legs.count(n) > 1})
    if missing_in_script:
        errors.append(
            f"scripts/ci-local-legs.sh has no `run_leg` call for leg(s) {missing_in_script!r} "
            "— `make ci-local` never runs that CI subsystem job's tests as its own process."
        )
    if extra_in_script:
        errors.append(
            f"scripts/ci-local-legs.sh calls `run_leg` for leg(s) {extra_in_script!r} with no "
            "matching CI subsystem job — remove the call, or confirm ci-pr.yml still has a "
            "matching job and this script's discovery needs updating."
        )
    if duplicate_in_script:
        errors.append(
            f"scripts/ci-local-legs.sh calls `run_leg` more than once for leg(s) "
            f"{duplicate_in_script!r} — ambiguous, pick one."
        )
    for leg in sorted(set(expected_legs) & set(script_legs)):
        if not leg_runner_wires_dirs_var(leg_runner_path, leg):
            errors.append(
                f"scripts/ci-local-legs.sh's `run_leg {leg}` call does not reference "
                f"CI_LOCAL_DIRS_{leg} — it may be wired to the wrong leg's directories."
            )
    return errors


def check(workflow_path: Path, makefile_path: Path, leg_runner_path: Path) -> list[str]:
    """Return a list of violation strings (empty = clean).

    Orchestrates the individual comparisons (each its own function, kept
    small — see I13's complexity cap) in the same order the module docstring
    describes: dirs+marker union, then the Car F10 leg structure on the
    Makefile side, then on the recipe side, then on the leg-runner side.
    """
    try:
        wf_dirs, wf_markers = workflow_selection(workflow_path)
    except ValueError as exc:
        return [f"WORKFLOW PARSE ERROR ({workflow_path}): {exc}"]

    distinct_markers = set(wf_markers.values())
    if len(distinct_markers) != 1:
        return [
            "the discovered CI subsystem jobs no longer share ONE marker expression "
            f"({wf_markers!r}) — this script's single-marker assumption is invalid. "
            "Update check_ci_local_parity.py to compare per-job (and reconsider "
            "whether `make ci-local` can still share one -m expression across legs)."
        ]
    wf_marker = distinct_markers.pop()

    try:
        mk_marker = makefile_marker(makefile_path)
        mk_legs = makefile_legs(makefile_path)
    except ValueError as exc:
        return [f"MAKEFILE PARSE ERROR ({makefile_path}): {exc}"]

    mk_dirs: set[str] = set()
    for dirs in mk_legs.values():
        mk_dirs |= dirs

    errors = _check_dirs_and_marker(wf_dirs, mk_dirs, wf_marker, mk_marker)

    expected_legs = {
        leg_name_for_job(job): dirs
        for job, (dirs, _marker) in discover_subsystem_jobs(workflow_path).items()
    }
    errors += _check_leg_boundaries(expected_legs, mk_legs)

    try:
        recipe = ci_local_recipe_text(makefile_path)
    except ValueError as exc:
        return [*errors, f"MAKEFILE PARSE ERROR ({makefile_path}): {exc}"]
    errors += _check_recipe_consumption(recipe, mk_legs)

    try:
        script_legs = leg_runner_legs(leg_runner_path)
    except ValueError as exc:
        return [*errors, f"LEG RUNNER PARSE ERROR ({leg_runner_path}): {exc}"]
    errors += _check_script_legs(expected_legs, script_legs, leg_runner_path)

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fail if `make ci-local`'s test selection or LEG STRUCTURE diverges "
            "from ci-pr.yml's subsystem jobs"
        )
    )
    parser.add_argument("--workflow", default=str(_WORKFLOW), help="Path to ci-pr.yml")
    parser.add_argument("--makefile", default=str(_MAKEFILE), help="Path to the Makefile")
    parser.add_argument(
        "--leg-runner", default=str(_LEG_RUNNER), help="Path to scripts/ci-local-legs.sh"
    )
    args = parser.parse_args(argv)

    errors = check(Path(args.workflow), Path(args.makefile), Path(args.leg_runner))
    if errors:
        print("ci-local / ci-pr.yml parity check FAILED:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print("ci-local / ci-pr.yml parity check OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
