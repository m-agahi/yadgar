#!/usr/bin/env python3
"""Backend-version bump enforcer: pre-commit hook AND CI gate (#83).

Pre-commit mode (default, no args):
    Checks staged files vs index. Run by pre-commit framework.

CI mode (--ci --base <ref>):
    Checks files changed between the merge-base of <ref> and HEAD vs the
    backend_version in server.json at base..HEAD.  Use with fetch-depth: 0 so
    the merge-base is reachable.  Fails a PR when yadgar/backend/** or other
    backend build inputs changed without a backend_version bump.

    The version comparison is merge-base vs HEAD (not <ref> tip vs HEAD) so a
    branch that merely lags behind master's backend_version is not a false pass.

Exit 0 → OK.  Exit 1 → error (describes offending files).
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

# ---------------------------------------------------------------------------
# Backend build inputs — paths whose staging triggers the version check.
# ---------------------------------------------------------------------------
BACKEND_BUILD_INPUTS: tuple[str, ...] = (
    "entrypoint-backend.sh",
    "Dockerfile.backend",
)
BACKEND_BUILD_DIRS: tuple[str, ...] = ("backend",)


def _backend_version_from_json(text: str) -> str | None:
    """Return backend_version from server.json text, or None if absent/unparseable."""
    try:
        data = json.loads(text)
        return data.get("backend_version")
    except json.JSONDecodeError:
        return None
    except AttributeError:
        return None


def _is_backend_build_input(path: str) -> bool:
    """Return True if *path* is a backend build input."""
    p = Path(path)
    if p.name in BACKEND_BUILD_INPUTS:
        return True
    # Match a "backend" dir at ANY depth: top-level backend/ and the v5.60
    # yadgar/backend/ subpackage (cache, ml_client, embed_service, metrics).
    for d in BACKEND_BUILD_DIRS:
        if d in p.parts:
            return True
    return False


def check(
    staged_files: list[str],
    server_json_head: str | None,
    server_json_staged: str | None,
) -> tuple[bool, str]:
    """Pure-function decision logic for the hook.

    Args:
        staged_files: Files staged in this commit (from git diff --cached --name-only).
        server_json_head: Content of server.json at HEAD (None if not yet in tree).
        server_json_staged: Content of server.json in the index (None if not staged).

    Returns:
        (ok, message) — ok=True means the hook passes.
    """
    # Determine which backend build inputs are staged.
    staged_backend = [f for f in staged_files if _is_backend_build_input(f)]
    if not staged_backend:
        return True, "no backend build inputs staged"

    # server.json must be staged.
    if server_json_staged is None:
        return False, (
            f"Backend build inputs staged ({', '.join(staged_backend)}) but server.json "
            "is not staged. Bump backend_version in server.json before committing."
        )

    # backend_version must have changed relative to HEAD.
    head_ver = _backend_version_from_json(server_json_head) if server_json_head else None
    staged_ver = _backend_version_from_json(server_json_staged)

    if staged_ver is None:
        return False, (
            "server.json is staged but backend_version is missing or unparseable. "
            "Add a backend_version field and bump it."
        )

    if head_ver == staged_ver:
        return False, (
            f"Backend build inputs staged ({', '.join(staged_backend)}) but "
            f"backend_version in server.json is unchanged ({staged_ver!r}). "
            "Bump backend_version (e.g. 5.0.2 → 5.0.3) before committing."
        )

    return True, f"backend_version bumped {head_ver!r} → {staged_ver!r}"


# ---------------------------------------------------------------------------
# CI mode helpers
# ---------------------------------------------------------------------------

GitRunner = Callable[[list[str]], str]


def collect_ci_inputs(
    base_ref: str,
    run_git: GitRunner,
) -> tuple[list[str], str | None, str | None]:
    """Gather inputs for check() from a CI diff (PR branch vs merge-base).

    Uses the merge-base of <base_ref> and HEAD so the comparison is stable even
    when the base branch has advanced beyond the branch point.

    Args:
        base_ref: The base branch ref (e.g. "origin/master").
        run_git:  Callable(args) → stdout str (injectable for testing).

    Returns:
        (changed_files, server_json_base, server_json_head)
        where server_json_base is the content at the merge-base commit and
        server_json_head is the content at HEAD.  Either may be None if the
        file doesn't exist at that ref.
    """
    # Find the merge-base so we compare against the branch point, not the tip.
    merge_base = run_git(["merge-base", base_ref, "HEAD"]).strip()
    if not merge_base:
        # Fallback: treat base_ref itself as the base (shallow histories,
        # first commit, etc.).
        merge_base = base_ref

    changed_raw = run_git(["diff", "--name-only", merge_base, "HEAD"])
    changed_files = [f for f in changed_raw.splitlines() if f]

    base_server = run_git(["show", f"{merge_base}:server.json"]) or None
    head_server = run_git(["show", "HEAD:server.json"]) or None

    return changed_files, base_server, head_server


# ---------------------------------------------------------------------------
# Entry point — called by pre-commit framework (pass_filenames: false)
#                OR directly as `python scripts/check_backend_bump.py --ci --base <ref>`
# ---------------------------------------------------------------------------


def _git(args: list[str]) -> str:
    """Run git and return stdout (empty string on error)."""
    result = subprocess.run(["git"] + args, capture_output=True, text=True)
    if result.returncode != 0:
        return ""
    return result.stdout


def main() -> int:
    args = sys.argv[1:]

    # ── CI mode ──────────────────────────────────────────────────────────────
    if "--ci" in args:
        try:
            base_idx = args.index("--base")
            base_ref = args[base_idx + 1]
        except ValueError, IndexError:
            print(
                "check-backend-bump: ERROR: --ci requires --base <ref>",
                file=sys.stderr,
            )
            return 1

        changed_files, server_json_base, server_json_head = collect_ci_inputs(base_ref, _git)
        ok, message = check(changed_files, server_json_base, server_json_head)
        if not ok:
            print(f"check-backend-bump [CI]: ERROR: {message}", file=sys.stderr)
            return 1
        print(f"check-backend-bump [CI]: OK — {message}")
        return 0

    # ── Pre-commit mode (default) ─────────────────────────────────────────────
    staged_files_raw = _git(["diff", "--cached", "--name-only"])
    staged_files = [f for f in staged_files_raw.splitlines() if f]

    # Read server.json from the index (staged version).
    server_json_staged_raw = _git(["show", ":server.json"])
    server_json_staged = server_json_staged_raw if server_json_staged_raw else None

    # Read server.json at HEAD (prior to this commit).
    server_json_head_raw = _git(["show", "HEAD:server.json"])
    server_json_head = server_json_head_raw if server_json_head_raw else None

    ok, message = check(staged_files, server_json_head, server_json_staged)
    if not ok:
        print(f"check-backend-bump: ERROR: {message}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
