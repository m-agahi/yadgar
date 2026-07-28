#!/usr/bin/env python3
"""Local pre-push mirror of CI's ``verify-version-bump`` job.

CI (.github/workflows/ci-pr.yml, job verify-version-bump) fails a PR when
pyproject.toml's version still matches the latest git tag AND the PR touches
yadgar/**. That only surfaces after push, as a CI failure on an already-open
PR — this script runs the same check as a pre-push hook so it fails fast,
locally, before the ~7min `make e2e` safety net even starts.

Mirrors the CI bash verbatim:
    LATEST_TAG=$(git describe --tags --abbrev=0 2>/dev/null || echo "v0.0.0")
    PYPROJECT_VER=$(grep -E '^version = ' pyproject.toml | head -1 | sed ...)
    if [ "v${PYPROJECT_VER}" = "${LATEST_TAG}" ]; then
      if git diff --name-only origin/master...HEAD | grep -qE '^yadgar/'; then
        exit 1
      fi
    fi

Fail-open: when origin/master is unreachable locally (no network, remote not
fetched, shallow clone without the ref) the diff itself cannot be computed —
this is an environment gap, not evidence of a real version-bump problem, so
the check is skipped with a warning on stderr and exit 0 (mirrors the
fail-open pattern in yadgar/core/runtime_config_client.py: never block on
something you can't verify). CI still enforces this authoritatively on push.

There is no local equivalent of CI's 'no-release' PR-label bypass — use the
standard pre-commit per-hook opt-out instead:
    SKIP=verify-version-bump-local git push

Exit 0 -> OK (or skipped). Exit 1 -> version bump required.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

GitResult = tuple[int, str]
GitRunner = Callable[[list[str]], GitResult]


def _git(args: list[str]) -> GitResult:
    """Run git and return (returncode, stdout)."""
    result = subprocess.run(["git"] + args, capture_output=True, text=True)
    return result.returncode, result.stdout


def get_latest_tag(run_git: GitRunner) -> str:
    """Return the latest reachable tag, or 'v0.0.0' when there is none.

    Mirrors CI: `git describe --tags --abbrev=0 2>/dev/null || echo "v0.0.0"`.
    """
    code, out = run_git(["describe", "--tags", "--abbrev=0"])
    tag = out.strip()
    if code != 0 or not tag:
        return "v0.0.0"
    return tag


def get_yadgar_diff(run_git: GitRunner) -> tuple[bool, list[str]]:
    """Diff against origin/master and return (reachable, yadgar/** files changed).

    reachable=False means the diff itself failed (origin/master ref not
    resolvable locally) — distinct from a reachable-but-empty diff, which is
    a legitimate "nothing changed" result.
    """
    code, out = run_git(["diff", "--name-only", "origin/master...HEAD"])
    if code != 0:
        return False, []
    changed = [f for f in out.splitlines() if f]
    yadgar_files = [f for f in changed if f.startswith("yadgar/")]
    return True, yadgar_files


def read_pyproject_version(pyproject_text: str) -> str | None:
    """Extract the version string from pyproject.toml content."""
    m = re.search(r'^version\s*=\s*"([^"]+)"', pyproject_text, re.MULTILINE)
    return m.group(1) if m else None


def check(pyproject_version: str, latest_tag: str, yadgar_files: list[str]) -> tuple[bool, str]:
    """Pure-function decision logic — same inputs, same verdict as CI.

    Args:
        pyproject_version: version field from pyproject.toml (no leading 'v').
        latest_tag: latest git tag (e.g. 'v5.166.4'), or 'v0.0.0' fallback.
        yadgar_files: files under yadgar/** changed vs origin/master.

    Returns:
        (ok, message) — ok=True means the check passes.
    """
    if f"v{pyproject_version}" == latest_tag and yadgar_files:
        return False, (
            f"pyproject.toml version ({pyproject_version}) matches latest tag "
            f"({latest_tag}). Bump via scripts/bump_version.py --bump patch|minor|major."
        )
    return True, f"pyproject.toml version ({pyproject_version}) vs latest tag ({latest_tag}) — OK"


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    pyproject_path = root / "pyproject.toml"

    version = read_pyproject_version(pyproject_path.read_text())
    if version is None:
        print(
            "check-version-bump: WARNING: could not parse version from pyproject.toml; skipping",
            file=sys.stderr,
        )
        return 0

    reachable, yadgar_files = get_yadgar_diff(_git)
    if not reachable:
        print(
            "check-version-bump: WARNING: origin/master unreachable locally "
            "(no network, remote not fetched, or shallow clone) — skipping local "
            "version-bump check. CI will still enforce this on push.",
            file=sys.stderr,
        )
        return 0

    latest_tag = get_latest_tag(_git)
    ok, message = check(version, latest_tag, yadgar_files)
    if not ok:
        print(f"check-version-bump: ERROR: {message}", file=sys.stderr)
        return 1

    print(f"check-version-bump: OK — {message}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
