#!/usr/bin/env python3
"""Single source of truth for ci-pr.yml's `tests` matrix groups (Car J1).

WHY THIS EXISTS
----------------
Car J1 replaced four `needs:`-chained test jobs with ONE matrix job whose
`include:` entries are the groups. That collapses four YAML job blocks into
one, which breaks every consumer that used to enumerate the jobs by reading
job headers:

  * ``check-skip-inventory`` used to hardcode ``-ne 5`` for the number of
    ``-rs`` artifacts it expects. A literal that happens to match today stops
    discriminating the moment a group is added or removed — it would download
    fewer artifacts and still pass, which is the "gate that checks nothing"
    shape this repo keeps rediscovering. It now asks THIS script.
  * ``scripts/check_ci_local_parity.py`` used to discover subsystem jobs by
    job-header shape. A matrix job has ONE header for N runtime jobs, so
    header-based discovery would collapse the matrix into a single leg and
    thereby legalise the exact lumped `make ci-local` invocation Car F10
    existed to kill. It now asks THIS script.

GROUPS vs LEGS — the distinction the whole file turns on
---------------------------------------------------------
A GROUP is one matrix runtime job (one container, one pytest process).
A LEG is one local `make ci-local` process.

They are NOT 1:1, because a SHARDED group runs the same selection N times with
different ``--splits/--group`` flags. ``core-1`` and ``core-2`` both select
``yadgar/tests/core/``; locally there is no reason to run that directory twice,
so they collapse to a single ``core`` leg. The mapping is mechanical: strip a
trailing ``-<digits>`` shard suffix, then replace ``-`` with ``_`` so the name
is a legal shell/Make identifier (``CI_LOCAL_DIRS_<leg>`` is exported into the
leg runner's environment, and POSIX env names cannot contain hyphens).

The mapping is checked both ways in :func:`legs`, rather than trusted:
groups with the SAME paths must reduce to the SAME leg, and groups with
DIFFERENT paths must reduce to DIFFERENT legs. Either violation raises. Without
that, ``core-1``/``core-2`` silently mapping to two legs would double local
work, and two genuinely different groups colliding on one leg name would make
one of them locally untested while every count still matched.

Deliberately stdlib-only (no ruamel/pyyaml): a pre-commit ``language: system``
hook runs whatever ``python``/``python3`` resolves to on PATH at commit time,
which is not guaranteed to be this repo's uv-managed ``.venv``. Every other
``check_*.py`` in ``scripts/`` is stdlib-only for the same reason — so this
parses the ``include:`` block textually, with the shape asserted rather than
assumed.

Usage:
  python scripts/ci_group_manifest.py                 # group<TAB>paths, one per line
  python scripts/ci_group_manifest.py --count         # number of groups
  python scripts/ci_group_manifest.py --names         # group names, one per line
  python scripts/ci_group_manifest.py --legs          # leg<TAB>paths, one per line
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci-pr.yml"

# The matrix job whose `include:` entries define the groups.
MATRIX_JOB = "tests"

_JOB_HEADER_RE = re.compile(r"^  ([A-Za-z0-9_-]+):[ \t]*$", re.MULTILINE)
_INCLUDE_RE = re.compile(r"^\s*include:[ \t]*$", re.MULTILINE)
_GROUP_RE = re.compile(r"^\s*- group:[ \t]*(\S+)[ \t]*$")
_PATHS_RE = re.compile(r"^\s*paths:[ \t]*(.+?)[ \t]*$")
_SPLIT_RE = re.compile(r"^\s*split:[ \t]*(.+?)[ \t]*$")
_SHARD_SUFFIX_RE = re.compile(r"-\d+$")


class ManifestError(RuntimeError):
    """The workflow could not be parsed into a group manifest."""


def _matrix_block(workflow_path: Path) -> str:
    """Return the text of the MATRIX_JOB block, sliced between job headers."""
    text = workflow_path.read_text(encoding="utf-8")
    jobs_idx = text.find("\njobs:")
    if jobs_idx == -1:
        raise ManifestError(f"{workflow_path}: no top-level 'jobs:' key found")
    section = text[jobs_idx:]
    headers = [(m.start(), m.group(1)) for m in _JOB_HEADER_RE.finditer(section)]
    for i, (pos, name) in enumerate(headers):
        if name != MATRIX_JOB:
            continue
        end = headers[i + 1][0] if i + 1 < len(headers) else len(section)
        return section[pos:end]
    raise ManifestError(
        f"{workflow_path}: no `{MATRIX_JOB}:` job found. Every consumer of this "
        "manifest (check-skip-inventory's artifact count, check_ci_local_parity) "
        "keys off that job — renaming it silently blinds them, so this fails loudly."
    )


def groups(workflow_path: Path = _WORKFLOW) -> list[dict[str, str]]:
    """Return ``[{'group': ..., 'paths': ..., 'split': ...}, ...]`` in file order.

    ``split`` is the empty string for unsharded groups.
    """
    block = _matrix_block(workflow_path)
    inc = _INCLUDE_RE.search(block)
    if not inc:
        raise ManifestError(f"`{MATRIX_JOB}` job has no `include:` block")

    found: list[dict[str, str]] = []
    for line in block[inc.end() :].splitlines():
        gm = _GROUP_RE.match(line)
        if gm:
            found.append({"group": gm.group(1), "paths": "", "split": ""})
            continue
        if not found:
            continue
        pm = _PATHS_RE.match(line)
        if pm and not found[-1]["paths"]:
            found[-1]["paths"] = pm.group(1)
            continue
        sm = _SPLIT_RE.match(line)
        if sm and not found[-1]["split"]:
            found[-1]["split"] = sm.group(1)

    if not found:
        raise ManifestError(
            f"`{MATRIX_JOB}`'s `include:` block declares no `- group:` entries — "
            "has the matrix shape changed out from under this parser?"
        )
    missing = [g["group"] for g in found if not g["paths"]]
    if missing:
        raise ManifestError(
            f"matrix group(s) {missing!r} declare no `paths:` — a group with no "
            "selection would run nothing and still go green."
        )
    dupes = sorted({g["group"] for g in found if [x["group"] for x in found].count(g["group"]) > 1})
    if dupes:
        raise ManifestError(f"duplicate matrix group name(s) {dupes!r} — names must be unique")
    return found


def leg_name_for_group(group: str) -> str:
    """Map a matrix group name to its local `make ci-local` leg name.

    Strips a trailing ``-<digits>`` shard suffix (``core-1`` -> ``core``) and
    converts ``-`` to ``_`` so the result is a legal POSIX environment-variable
    name (``CI_LOCAL_DIRS_<leg>``).
    """
    return _SHARD_SUFFIX_RE.sub("", group).replace("-", "_")


def legs(workflow_path: Path = _WORKFLOW) -> dict[str, str]:
    """Return ``{leg_name: paths}`` — sharded groups collapsed to one leg.

    Raises if the group->leg mapping is not consistent in BOTH directions; see
    the module docstring for why each direction matters.
    """
    out: dict[str, str] = {}
    origin: dict[str, str] = {}
    for entry in groups(workflow_path):
        leg = leg_name_for_group(entry["group"])
        paths = entry["paths"]
        if leg in out and out[leg] != paths:
            raise ManifestError(
                f"matrix groups {origin[leg]!r} and {entry['group']!r} both map to leg "
                f"{leg!r} but select DIFFERENT paths:\n"
                f"  {origin[leg]}: {out[leg]}\n  {entry['group']}: {paths}\n"
                "One of them would be locally untested while every count still matched. "
                "Rename one group, or give them the same selection."
            )
        out.setdefault(leg, paths)
        origin.setdefault(leg, entry["group"])

    by_paths: dict[str, list[str]] = {}
    for leg, paths in out.items():
        by_paths.setdefault(paths, []).append(leg)
    collisions = {p: sorted(ls) for p, ls in by_paths.items() if len(ls) > 1}
    if collisions:
        raise ManifestError(
            f"distinct legs select IDENTICAL paths: {collisions!r} — `make ci-local` "
            "would run the same selection twice. Shard names must share one leg "
            "(use a trailing -<digits> suffix, e.g. core-1 / core-2)."
        )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Enumerate ci-pr.yml's `tests` matrix groups")
    parser.add_argument("--workflow", default=str(_WORKFLOW), help="Path to ci-pr.yml")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--count", action="store_true", help="print the number of groups")
    mode.add_argument("--names", action="store_true", help="print group names, one per line")
    mode.add_argument("--legs", action="store_true", help="print leg<TAB>paths, one per line")
    args = parser.parse_args(argv)

    path = Path(args.workflow)
    try:
        if args.legs:
            for leg, paths in legs(path).items():
                print(f"{leg}\t{paths}")
            return 0
        entries = groups(path)
    except ManifestError as exc:
        print(f"ci_group_manifest: {exc}", file=sys.stderr)
        return 1

    if args.count:
        print(len(entries))
    elif args.names:
        for entry in entries:
            print(entry["group"])
    else:
        for entry in entries:
            print(f"{entry['group']}\t{entry['paths']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
