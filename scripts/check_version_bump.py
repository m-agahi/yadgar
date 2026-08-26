#!/usr/bin/env python3
"""Core-version bump enforcer: pre-push hook AND CI gate.

The question this gate must answer is: **does THIS branch bump
``pyproject.toml``'s version relative to the commit it forked from?**

It used to answer a different question — "does ``pyproject.toml``'s version
differ from the latest git tag?" — and that question is unanswerable once
master moves. Measured on the identity/bug-bag trains (task 382):

    ref                                  pyproject version
    ------------------------------------ -----------------
    merge-base with origin/master        5.190.2
    the train's own bump commit          5.190.3
    after a master merge landed          5.190.2   <- bump silently reverted

The latest tag was ``v5.190.1``. Because ``5.190.2 != v5.190.1`` the gate
stayed GREEN across a branch whose own version bump had been clobbered by a
master merge — ~100 files of core change riding a release number master had
already consumed. A tag cannot see that: it is a fixed point in the past,
not the branch's fork point.

So the comparison is now **merge-base vs HEAD**, the same shape
``scripts/check_backend_bump.py`` has used since PR #175 (ADR-0097), and the
merge-base resolution itself is that script's ``resolve_merge_base`` — one
implementation, shared, rather than two that can drift.

FAIL LOUD, NEVER DEGRADE
------------------------
When the merge-base cannot be determined (shallow clone, unfetched remote,
detached CI checkout) this gate exits 1. The previous fail-open behaviour was
defensible while the comparison was tag-based — the tag is always local — but
a gate that silently falls back to the broken comparison whenever it cannot
run the correct one is the same defect wearing a fallback. The error message
names the remedy (``git fetch origin master``) and the documented escape.

Modes:
    (default)          pre-push hook — base ref ``origin/master``.
    --ci --base <ref>  CI gate — same logic, explicit base ref. Requires
                       ``fetch-depth: 0`` so the merge-base is reachable.

Both modes feed the same pure ``check()`` — same inputs, same verdict, which
is the ADR-0097 local/CI parity contract this gate family is held to.

There is no local equivalent of CI's 'no-release' PR-label bypass — use the
standard pre-commit per-hook opt-out instead:
    SKIP=verify-version-bump-local git push

Exit 0 -> OK. Exit 1 -> version bump required (or merge-base unresolvable).
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

# Sibling-script import (precedent: scripts/check_skip_markers.py imports from
# scripts/check_skip_inventory.py). Reused rather than reimplemented so the two
# version gates cannot drift on what "the branch point" means.
from check_backend_bump import GitRunner, resolve_merge_base  # noqa: E402

DEFAULT_BASE_REF = "origin/master"


def _git(args: list[str]) -> str:
    """Run git and return stdout (empty string on error)."""
    result = subprocess.run(["git"] + args, capture_output=True, text=True)
    if result.returncode != 0:
        return ""
    return result.stdout


def read_pyproject_version(pyproject_text: str) -> str | None:
    """Extract the version string from pyproject.toml content."""
    m = re.search(r'^version\s*=\s*"([^"]+)"', pyproject_text, re.MULTILINE)
    return m.group(1) if m else None


def get_base_version(merge_base: str, run_git: GitRunner) -> str | None:
    """Return pyproject.toml's version at *merge_base*, or None.

    None means either the file did not exist at the branch point or its
    version field was unparseable there — both of which make "unchanged since
    the branch point" unprovable, so the caller treats it as a pass.
    """
    text = run_git(["show", f"{merge_base}:pyproject.toml"])
    if not text:
        return None
    return read_pyproject_version(text)


def get_yadgar_diff(merge_base: str, run_git: GitRunner) -> list[str]:
    """Return the ``yadgar/**`` files changed between *merge_base* and HEAD."""
    out = run_git(["diff", "--name-only", merge_base, "HEAD"])
    return [f for f in out.splitlines() if f.startswith("yadgar/")]


def check(
    pyproject_version: str,
    base_version: str | None,
    yadgar_files: list[str],
) -> tuple[bool, str]:
    """Pure-function decision logic — same inputs, same verdict as CI.

    Args:
        pyproject_version: version field from pyproject.toml at HEAD.
        base_version: version field at the merge-base with the default branch,
            or None when pyproject.toml was absent/unparseable there.
        yadgar_files: files under yadgar/** changed vs the merge-base.

    Returns:
        (ok, message) — ok=True means the check passes.
    """
    if not yadgar_files:
        return True, (
            f"no yadgar/** changes vs the merge-base — version ({pyproject_version}) needs no bump"
        )
    if base_version is None:
        return True, (
            f"pyproject.toml absent or unparseable at the merge-base — "
            f"version ({pyproject_version}) accepted"
        )
    if pyproject_version == base_version:
        return False, (
            f"pyproject.toml version ({pyproject_version}) is UNCHANGED since the "
            f"merge-base with the default branch, but {len(yadgar_files)} "
            f"yadgar/** file(s) changed on this branch. Bump via "
            f"scripts/bump_version.py --bump patch|minor|major. "
            f"(A branch whose bump was reverted by a master merge lands here too — "
            f"that is the case this gate was rebuilt to catch.)"
        )
    return True, (
        f"pyproject.toml version bumped {base_version} -> {pyproject_version} "
        f"vs the merge-base — OK"
    )


def run(base_ref: str, label: str) -> int:
    """Shared entry path for both modes. Returns a process exit code."""
    root = Path(__file__).resolve().parent.parent
    pyproject_path = root / "pyproject.toml"

    version = read_pyproject_version(pyproject_path.read_text())
    if version is None:
        print(
            f"{label}: WARNING: could not parse version from pyproject.toml; skipping",
            file=sys.stderr,
        )
        return 0

    merge_base = resolve_merge_base(base_ref, _git)
    if merge_base is None:
        print(
            f"{label}: ERROR: cannot resolve the merge-base of {base_ref} and HEAD, "
            f"so 'did this branch bump the version' is unanswerable. This gate "
            f"deliberately does NOT fall back to a latest-tag comparison — that "
            f"comparison is the defect it replaced (task 382). Fix the checkout: "
            f"`git fetch origin master` locally, or `fetch-depth: 0` in CI. "
            f"Deliberate no-bump push: `SKIP=verify-version-bump-local git push`.",
            file=sys.stderr,
        )
        return 1

    base_version = get_base_version(merge_base, _git)
    yadgar_files = get_yadgar_diff(merge_base, _git)

    ok, message = check(version, base_version, yadgar_files)
    if not ok:
        print(f"{label}: ERROR: {message}", file=sys.stderr)
        return 1

    print(f"{label}: OK — {message}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if "--ci" in args:
        try:
            base_ref = args[args.index("--base") + 1]
        # Parenthesised tuple required — CI compiles on <py3.14 where the bare
        # `except X, Y:` form is a SyntaxError. fmt:skip keeps ruff (py314
        # target, PEP 758) from stripping the parens back to the bare form.
        except (ValueError, IndexError):  # fmt: skip
            print("check-version-bump: ERROR: --ci requires --base <ref>", file=sys.stderr)
            return 1
        return run(base_ref, "check-version-bump [CI]")

    return run(DEFAULT_BASE_REF, "check-version-bump")


if __name__ == "__main__":
    sys.exit(main())
