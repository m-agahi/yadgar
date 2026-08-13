#!/usr/bin/env python3
"""`make ci-local` / `.github/workflows/ci-pr.yml` parity guard (Car F5, extended Car F10, reworked Car J1).

WHY THIS EXISTS
----------------
PR #40 shipped on a green ``make e2e`` — a target that runs
``yadgar/tests/e2e/ -m e2e``, a directory and marker completely disjoint from
what CI actually gates a PR on. CI then found 49 failures ``make e2e`` never
touched. ``make ci-local`` (Makefile) exists to give a real local
reproduction of CI's selection: the union of the test groups in ``ci-pr.yml``.

A ``make ci-local`` that is a hand-copied SNAPSHOT of those groups' paths and
marker would itself silently rot the next time someone edits the workflow
without updating the Makefile — the exact bug class this whole car exists to
close. This script is the tripwire: it parses ``ci-pr.yml``, the ``Makefile``,
and ``scripts/ci-local-legs.sh`` INDEPENDENTLY and fails loudly the moment
they disagree, instead of trusting any of them to have kept the others honest.

CAR F10 — WHY DIRS/MARKER PARITY WASN'T ENOUGH
------------------------------------------------
Car F5's original guard compared only the directory union and the marker
expression. It could not see the defect Car F10 fixed: ``make ci-local`` ran
the union as ONE pytest process, while CI runs it as SEVERAL SEPARATE processes
(one per group/container). A single-process target and a many-process target
with an IDENTICAL union+marker look identical to a dirs/marker-only
comparison — the lumped invocation accumulated memory no single CI job ever
does and got OOM-killed on a real box. This script therefore ALSO pins the LEG
STRUCTURE: one ``run_leg`` call in scripts/ci-local-legs.sh, fed by one
``CI_LOCAL_DIRS_<leg>`` Makefile variable, per CI leg.

CAR J1 — TWO CHANGES, BOTH FORCED BY THE MATRIX
-------------------------------------------------
Car J1 replaced four ``needs:``-chained test jobs with ONE ``tests`` matrix
job whose ``include:`` entries are the groups. That breaks the original design
in two independent ways, and BOTH had to change or this guard would have kept
passing while checking nothing:

1. **Discovery is no longer job-header shape.** A matrix job has ONE ``  tests:``
   header for N runtime jobs. Header-shape discovery would therefore collapse
   the whole matrix into a single leg — and then demand a single lumped
   ``make ci-local`` leg covering every directory, LEGALISING precisely the
   regression Car F10 existed to kill. Discovery now delegates to
   :mod:`ci_group_manifest`, which enumerates the matrix ``include:`` entries.

2. **The comparison unit is no longer a set of directory prefixes.** With
   groups selecting file globs or sharded selections, two groups can reduce to
   the same directory prefix while selecting completely different tests (e.g.
   ``yadgar/tests/core/test_wiki_*.py`` and ``yadgar/tests/core/test_dlq_*.py``
   both reduce to ``yadgar/tests/core/``). A dirs-only comparison would call
   those identical and pass while the local leg ran the wrong files. The
   load-bearing comparison is now the VERBATIM pytest path-argument tokens per
   leg, order-insensitive but otherwise byte-for-byte. The directory union is
   still compared, but only as a secondary, friendlier error message.

A SHARDED group maps N runtime jobs onto ONE local leg (``core-1``/``core-2``
both select ``yadgar/tests/core/``; running that directory twice locally would
be pure waste). :func:`ci_group_manifest.legs` owns that mapping and validates
it in both directions — see its docstring.

WHAT IT COMPARES
-----------------
  * selection tokens — each CI leg's exact pytest path arguments vs. the
    matching ``CI_LOCAL_DIRS_<leg>`` Makefile variable's tokens.
  * directories — the union of every leg's ``yadgar/tests/<x>/`` paths, both
    sides (secondary; a friendlier message for the common "a whole directory is
    missing" case).
  * marker expression — the quoted ``-m '...'`` string the matrix job passes to
    pytest, vs. the ``CI_LOCAL_MARKER`` make variable.
  * leg structure — the set of CI legs matches, 1:1, the set of
    ``CI_LOCAL_DIRS_<leg>`` Makefile variables AND the set of ``run_leg <leg>
    ...`` calls in scripts/ci-local-legs.sh, with each ``run_leg`` call wired to
    the matching ``CI_LOCAL_DIRS_<leg>`` variable.
  * recipe consumption — that the ``ci-local:`` recipe body actually references
    every ``CI_LOCAL_DIRS_<leg>``/``CI_LOCAL_MARKER`` variable and delegates to
    ``scripts/ci-local-legs.sh``, rather than a hardcoded path list or a recipe
    that quietly ignores the declared legs.
  * stray test jobs — any job OUTSIDE the matrix that is shaped like a
    subsystem leg (a ``yadgar/tests/.../`` path plus a QUOTED ``-m`` marker) is
    a test job nothing here accounts for, and fails. This retains the property
    Car F10's shape-based discovery gave: a fifth test job added later cannot
    leave ``make ci-local`` silently blind to it. ``test-perf`` (``-m perf``,
    unquoted) and ``viz-tests``' Layer 2 (``-m integration``, unquoted) are
    excluded BY THIS SHAPE, not by name.

NOT compared (out of scope, on purpose — see the Makefile's `ci-local`
comment): ``-n``/``--dist``/``--reruns`` flags (parallelism, not selection);
the ``--splits``/``--group`` sharding (a local run covers the union of all
splits anyway); the separate test-perf, viz-tests, and invariant-checks jobs
(different suites entirely).

Usage:
  python scripts/check_ci_local_parity.py                        # check, exit 0/1
  python scripts/check_ci_local_parity.py --workflow P --makefile P --leg-runner P

Exit codes:
  0  ci-local's selection, marker, and leg structure match the CI test groups
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

# ci_group_manifest lives next to this file. Running as `python
# scripts/check_ci_local_parity.py` puts scripts/ on sys.path[0]; importing this
# module from elsewhere (a test) does not, so add it explicitly. Both modules
# are stdlib-only — a pre-commit `language: system` hook runs whatever
# `python`/`python3` is on PATH at commit time, not necessarily this repo's
# uv-managed .venv (see ADR-0218's `lint-imports` PATH note).
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from ci_group_manifest import MATRIX_JOB, ManifestError  # noqa: E402
from ci_group_manifest import legs as manifest_legs  # noqa: E402

# Job blocks are top-level `  <name>:` keys under `jobs:` (2-space indent,
# GitHub Actions requires this shape) — text-sliced between consecutive headers
# rather than fully parsed.
_JOB_HEADER_RE = re.compile(r"^  ([A-Za-z0-9_-]+):[ \t]*$", re.MULTILINE)
_DIR_RE = re.compile(r"yadgar/tests/[\w\-]+/")
_MARKER_RE = re.compile(r"-m\s+(['\"])(.*?)\1")

# Bound to a NAME rather than written inline as `except (A, B):`. `ruff format`
# 0.16.x rewrites the parenthesised tuple to PEP 758's bare `except A, B:` on
# py3.14, and a repo guard (test_v5_46_16_except_tuple_sweep) fails the build on
# that shape. A named tuple is immune to the rewrite.
_PARSE_ERRORS = (ManifestError, ValueError)


def _job_blocks(jobs_section: str) -> dict[str, str]:
    """Slice *jobs_section* (text starting at 'jobs:') into {job_name: block_text}."""
    headers = [(m.start(), m.group(1)) for m in _JOB_HEADER_RE.finditer(jobs_section)]
    blocks: dict[str, str] = {}
    for i, (pos, name) in enumerate(headers):
        end = headers[i + 1][0] if i + 1 < len(headers) else len(jobs_section)
        blocks[name] = jobs_section[pos:end]
    return blocks


def _jobs_text(workflow_path: Path) -> dict[str, str]:
    text = workflow_path.read_text(encoding="utf-8")
    jobs_idx = text.find("\njobs:")
    if jobs_idx == -1:
        raise ValueError("no top-level 'jobs:' key found")
    return _job_blocks(text[jobs_idx:])


def tokens(value: str) -> tuple[str, ...]:
    """Split a pytest path-argument string into comparable, order-insensitive tokens."""
    return tuple(sorted(value.split()))


def workflow_legs(workflow_path: Path = _WORKFLOW) -> dict[str, tuple[str, ...]]:
    """Return ``{leg_name: selection_tokens}`` for the CI test matrix."""
    return {leg: tokens(paths) for leg, paths in manifest_legs(workflow_path).items()}


def workflow_marker(workflow_path: Path = _WORKFLOW) -> str:
    """Return the quoted ``-m '...'`` marker the matrix job passes to pytest.

    Asserted identical across every quoted marker in the matrix block, so that
    the collect-only step and the run step cannot drift apart silently.
    """
    blocks = _jobs_text(workflow_path)
    block = blocks.get(MATRIX_JOB)
    if block is None:
        raise ValueError(f"no `{MATRIX_JOB}:` job found in {workflow_path}")
    found = {m.group(2) for m in _MARKER_RE.finditer(block)}
    if not found:
        raise ValueError(
            f"`{MATRIX_JOB}` declares no quoted `-m '...'` marker — a group with no "
            "marker filter would run integration/e2e/perf tests the other jobs own."
        )
    if len(found) != 1:
        raise ValueError(
            f"`{MATRIX_JOB}` uses more than one quoted marker expression ({sorted(found)!r}) — "
            "`make ci-local` shares ONE -m expression across legs, so this must be single-valued."
        )
    return found.pop()


def stray_test_jobs(workflow_path: Path = _WORKFLOW) -> list[str]:
    """Jobs OUTSIDE the matrix that are shaped like a subsystem leg job.

    Shape: at least one ``yadgar/tests/.../`` path AND a QUOTED ``-m`` marker.
    Such a job runs gated unit tests that no ci-local leg covers.
    """
    stray: list[str] = []
    for name, block in _jobs_text(workflow_path).items():
        if name == MATRIX_JOB:
            continue
        if _DIR_RE.search(block) and _MARKER_RE.search(block):
            stray.append(name)
    return sorted(stray)


def makefile_marker(makefile_path: Path) -> str:
    """Return the ``CI_LOCAL_MARKER := ...`` value declared in the Makefile."""
    text = makefile_path.read_text(encoding="utf-8")
    m = re.search(r"^CI_LOCAL_MARKER\s*:=\s*(.*)$", text, re.MULTILINE)
    if not m:
        raise ValueError(
            "CI_LOCAL_MARKER not found — has the `ci-local` target been removed or renamed?"
        )
    return m.group(1).strip()


def makefile_legs(makefile_path: Path) -> dict[str, tuple[str, ...]]:
    """Return {leg_name: selection_tokens} for every ``CI_LOCAL_DIRS_<leg>`` line."""
    text = makefile_path.read_text(encoding="utf-8")
    found: dict[str, tuple[str, ...]] = {}
    for m in re.finditer(r"^CI_LOCAL_DIRS_([A-Za-z0-9_]+)\s*:=\s*(.*)$", text, re.MULTILINE):
        leg_name, value = m.group(1), m.group(2)
        toks = tokens(value)
        if not toks:
            raise ValueError(f"CI_LOCAL_DIRS_{leg_name} declares no paths")
        found[leg_name] = toks
    if not found:
        raise ValueError(
            "no CI_LOCAL_DIRS_<leg> variables found — has the `ci-local` target's "
            "leg structure been removed, renamed, or collapsed back into one "
            "CI_LOCAL_DIRS variable?"
        )
    return found


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
    'override' leg (the DIRS= mid-work path — not tied to any CI group, so it
    plays no part in the CI-parity comparison).

    This is the check that actually pins the LEG STRUCTURE (Car F10): if the
    per-group ``run_leg`` calls are ever collapsed back into a single lumped
    invocation — the exact regression this car fixed — the set of names
    returned here shrinks and no longer matches the legs derived from
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


def _dirs(selection: tuple[str, ...]) -> set[str]:
    """The ``yadgar/tests/<x>/`` directory prefixes a selection touches."""
    out: set[str] = set()
    for token in selection:
        out |= set(_DIR_RE.findall(token))
    return out


def _check_union_and_marker(
    wf_legs: dict[str, tuple[str, ...]],
    mk_legs: dict[str, tuple[str, ...]],
    wf_marker: str,
    mk_marker: str,
) -> list[str]:
    """Directory union + marker parity — the original Car F5 comparison.

    SECONDARY as of Car J1: the union cannot distinguish two selections inside
    the same directory, so :func:`_check_leg_selections` is the load-bearing
    check. This one survives because "a whole directory is missing" is the
    common case and deserves the clearer message.
    """
    errors: list[str] = []
    wf_dirs: set[str] = set()
    for sel in wf_legs.values():
        wf_dirs |= _dirs(sel)
    mk_dirs: set[str] = set()
    for sel in mk_legs.values():
        mk_dirs |= _dirs(sel)

    missing_in_makefile = sorted(wf_dirs - mk_dirs)
    extra_in_makefile = sorted(mk_dirs - wf_dirs)
    if missing_in_makefile:
        errors.append(
            f"ci-pr.yml's test groups test {missing_in_makefile} but the Makefile's "
            "CI_LOCAL_DIRS_<leg> variables do not — `make ci-local` is BLIND to these "
            "directories. Add them to the right leg."
        )
    if extra_in_makefile:
        errors.append(
            f"the Makefile's CI_LOCAL_DIRS_<leg> variables test {extra_in_makefile} but no "
            "CI test group does — remove them, or confirm a CI group now covers them and "
            "this script's discovery needs updating."
        )
    if wf_marker != mk_marker:
        errors.append(
            f"marker mismatch: ci-pr.yml's `{MATRIX_JOB}` matrix uses {wf_marker!r}, "
            f"Makefile's CI_LOCAL_MARKER is {mk_marker!r}."
        )
    return errors


def _check_leg_selections(
    wf_legs: dict[str, tuple[str, ...]], mk_legs: dict[str, tuple[str, ...]]
) -> list[str]:
    """Leg structure + VERBATIM selection, Makefile side (Car J1's load-bearing check).

    One CI_LOCAL_DIRS_<leg> per CI leg, each matching that leg's pytest path
    arguments EXACTLY — not merely reducing to the same directory prefixes,
    which is what a dirs-only comparison could not tell apart.
    """
    errors: list[str] = []
    missing_legs = sorted(set(wf_legs) - set(mk_legs))
    extra_legs = sorted(set(mk_legs) - set(wf_legs))
    if missing_legs:
        errors.append(
            f"ci-pr.yml has test leg(s) {missing_legs!r} with no matching "
            "CI_LOCAL_DIRS_<leg> in the Makefile — `make ci-local`'s leg structure does not "
            "cover every CI test group. Add the missing leg."
        )
    if extra_legs:
        errors.append(
            f"the Makefile declares CI_LOCAL_DIRS_<leg> for leg(s) {extra_legs!r} with no "
            "matching CI test group — remove them, or confirm ci-pr.yml still has a "
            "matching group and this script's discovery needs updating."
        )
    for leg in sorted(set(wf_legs) & set(mk_legs)):
        if wf_legs[leg] != mk_legs[leg]:
            errors.append(
                f"leg {leg!r}: ci-pr.yml's group selects {list(wf_legs[leg])} but the "
                f"Makefile's CI_LOCAL_DIRS_{leg} selects {list(mk_legs[leg])} — the LEG "
                "SELECTION has drifted even though the directory union may still match."
            )
    return errors


def _check_recipe_consumption(recipe: str, mk_legs: dict[str, tuple[str, ...]]) -> list[str]:
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
    wf_legs: dict[str, tuple[str, ...]], script_legs: list[str], leg_runner_path: Path
) -> list[str]:
    """Leg structure, leg-runner side: one `run_leg` call per CI leg, each wired
    to the matching CI_LOCAL_DIRS_<leg> variable. This is the check that
    actually catches "legs collapsed back into one lumped invocation" — the
    regression Car F10 fixed."""
    errors: list[str] = []
    if len(script_legs) <= 1 and len(wf_legs) > 1:
        errors.append(
            f"scripts/ci-local-legs.sh calls `run_leg` {len(script_legs)} time(s) "
            f"({script_legs!r}) but ci-pr.yml has {len(wf_legs)} test legs — the "
            "legs have been collapsed back into one lumped invocation, the exact regression "
            "Car F10 fixed. Restore one `run_leg` call per CI leg."
        )

    missing_in_script = sorted(set(wf_legs) - set(script_legs))
    extra_in_script = sorted(set(script_legs) - set(wf_legs))
    duplicate_in_script = sorted({n for n in script_legs if script_legs.count(n) > 1})
    if missing_in_script:
        errors.append(
            f"scripts/ci-local-legs.sh has no `run_leg` call for leg(s) {missing_in_script!r} "
            "— `make ci-local` never runs that CI group's tests as its own process."
        )
    if extra_in_script:
        errors.append(
            f"scripts/ci-local-legs.sh calls `run_leg` for leg(s) {extra_in_script!r} with no "
            "matching CI test group — remove the call, or confirm ci-pr.yml still has a "
            "matching group and this script's discovery needs updating."
        )
    if duplicate_in_script:
        errors.append(
            f"scripts/ci-local-legs.sh calls `run_leg` more than once for leg(s) "
            f"{duplicate_in_script!r} — ambiguous, pick one."
        )
    for leg in sorted(set(wf_legs) & set(script_legs)):
        if not leg_runner_wires_dirs_var(leg_runner_path, leg):
            errors.append(
                f"scripts/ci-local-legs.sh's `run_leg {leg}` call does not reference "
                f"CI_LOCAL_DIRS_{leg} — it may be wired to the wrong leg's directories."
            )
    return errors


def check(workflow_path: Path, makefile_path: Path, leg_runner_path: Path) -> list[str]:
    """Return a list of violation strings (empty = clean)."""
    try:
        wf_legs = workflow_legs(workflow_path)
        wf_marker = workflow_marker(workflow_path)
        stray = stray_test_jobs(workflow_path)
    except _PARSE_ERRORS as exc:
        return [f"WORKFLOW PARSE ERROR ({workflow_path}): {exc}"]

    errors: list[str] = []
    if stray:
        errors.append(
            f"job(s) {stray!r} are shaped like subsystem test jobs (a yadgar/tests/.../ path "
            f"plus a quoted -m marker) but live OUTSIDE the `{MATRIX_JOB}` matrix, so no "
            "`make ci-local` leg covers them. Fold them into the matrix, or — if they are "
            "genuinely a different suite — remove the quoted marker/path shape that makes "
            "them look like one."
        )

    try:
        mk_marker = makefile_marker(makefile_path)
        mk_legs = makefile_legs(makefile_path)
    except ValueError as exc:
        return [*errors, f"MAKEFILE PARSE ERROR ({makefile_path}): {exc}"]

    errors += _check_union_and_marker(wf_legs, mk_legs, wf_marker, mk_marker)
    errors += _check_leg_selections(wf_legs, mk_legs)

    try:
        recipe = ci_local_recipe_text(makefile_path)
    except ValueError as exc:
        return [*errors, f"MAKEFILE PARSE ERROR ({makefile_path}): {exc}"]
    errors += _check_recipe_consumption(recipe, mk_legs)

    try:
        script_legs = leg_runner_legs(leg_runner_path)
    except ValueError as exc:
        return [*errors, f"LEG RUNNER PARSE ERROR ({leg_runner_path}): {exc}"]
    errors += _check_script_legs(wf_legs, script_legs, leg_runner_path)

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fail if `make ci-local`'s test selection or LEG STRUCTURE diverges "
            "from ci-pr.yml's test groups"
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
