#!/usr/bin/env python3
"""Pre-commit hook: enforce backend_version bump when backend build inputs change.

If any backend build input (entrypoint-backend.sh, Dockerfile.backend, or files
under any backend/ dir at any depth — e.g. yadgar/backend/) is staged in this
commit, server.json must also be staged with a backend_version change vs HEAD.

Exit 0 → OK.  Exit 1 → error (describes offending files).
"""

from __future__ import annotations

import json
import subprocess
import sys
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
# Entry point — called by pre-commit framework (pass_filenames: false)
# ---------------------------------------------------------------------------


def _git(args: list[str]) -> str:
    """Run git and return stdout (empty string on error)."""
    result = subprocess.run(["git"] + args, capture_output=True, text=True)
    if result.returncode != 0:
        return ""
    return result.stdout


def main() -> int:
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
