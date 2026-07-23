"""code_graph default-branch indexing — THE HARD CONSTRAINT.

Car B of the code_graph train (ADR-0162).

**Only index the latest ``origin/<default-branch>``, NEVER the working tree.**
A feature-branch / WIP / dirty checkout gives useless structure data.

Flow (``refresh_index``):
  1. opt-out guard (``is_opted_out`` — ``code_graph.enabled`` off for this repo in
     the runtime-config store, ADR-0163: global-off OR a per-repo false) → SKIP.
  2. capture the REAL repo identity (canonical_root + subdir) BEFORE any
     worktree exists — inside a detached worktree ``--show-toplevel`` returns the
     temp path, so identity MUST be computed from the original repo path first.
  3. resolve default branch (``git symbolic-ref refs/remotes/origin/HEAD`` →
     name; fallback ``git remote show origin``).  None → SKIP (no remote/offline).
  4. ``git fetch origin <default>``.  Failure → SKIP (offline/fetch-fail) — never
     fall back to indexing the working tree.
  5. materialize ``origin/<default>`` in a TEMP worktree
     (``git worktree add --detach <tmp> origin/<default>``).
  6. index the temp with ``CBM_ALLOWED_ROOT=<tmp>``.
  7. ALWAYS clean up the temp worktree (``git worktree remove --force``) in a
     ``finally`` — on success AND on every error path.

The digest is keyed to the real ``canonical_root`` + subdir, never the temp path,
so it stays stable while the user branch-switches.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from yadgar._shared.observability.observe import observe
from yadgar.core.code_graph import config, runner

#: git failure modes treated as "identity unavailable" — hoisted to a module
#: constant so no inline exception tuple exists for a formatter to normalise into
#: the Py2 ``except A, B:`` form (a hard SyntaxError on stock CPython 3).
_GIT_ERRORS = (subprocess.CalledProcessError, OSError)


@observe(tier="stage")
def _git(argv: list[str], *, cwd: str | None = None) -> subprocess.CompletedProcess:
    """Run a git command; raise CalledProcessError on non-zero exit."""
    return subprocess.run(  # noqa: S603 — fixed 'git' argv, no shell
        ["git", *argv],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )


@observe(tier="stage")
def resolve_default_branch(repo_path: str) -> str | None:
    """Return the default branch name (e.g. 'master'/'main'), or None.

    ``git symbolic-ref refs/remotes/origin/HEAD`` first (fast, exact); fallback to
    parsing ``git remote show origin``.  None when there is no remote / it is
    unreachable — the caller treats None as "skip, don't fall back to WIP".
    """
    try:
        out = _git(["symbolic-ref", "refs/remotes/origin/HEAD"], cwd=repo_path).stdout.strip()
        # e.g. "refs/remotes/origin/main" → "main"
        if out:
            return out.rsplit("/", 1)[-1]
    except subprocess.CalledProcessError:
        pass

    try:
        out = _git(["remote", "show", "origin"], cwd=repo_path).stdout
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("HEAD branch:"):
                name = line.split(":", 1)[1].strip()
                return name or None
    except subprocess.CalledProcessError:
        pass

    return None


@observe(tier="stage")
def _canonical_identity(repo_path: str) -> tuple[str, str]:
    """Return (canonical_root, relative_subdir) for the REAL repo.

    Computed from the original repo path BEFORE any temp worktree is created —
    inside a detached worktree ``--show-toplevel`` would return the temp path.
    ``canonical_root`` = git toplevel; ``subdir`` = repo_path relative to it
    (empty when repo_path IS the toplevel).  Falls back to repo_path itself when
    git is unreachable (never raises — identity is best-effort).
    """
    try:
        toplevel = _git(["rev-parse", "--show-toplevel"], cwd=repo_path).stdout.strip()
    except _GIT_ERRORS:
        # OSError covers a missing/unreadable cwd or an absent git binary — identity
        # is best-effort, never raises.
        return str(Path(repo_path).resolve()), ""

    top = Path(toplevel).resolve()
    here = Path(repo_path).resolve()
    try:
        subdir = str(here.relative_to(top))
    except ValueError:
        subdir = ""
    if subdir == ".":
        subdir = ""
    return str(top), subdir


@observe(tier="boundary")
def refresh_index(repo_path: str) -> dict[str, Any]:
    """Index the latest ``origin/<default>`` for ``repo_path`` (never the WIP tree).

    Returns a result dict:
      - skipped=True + reason  when opted out / no remote / fetch failed.
      - indexed=True + canonical_root + subdir + project + default_branch  on success.

    The temp worktree is ALWAYS removed (success and error) via ``finally``.
    """
    # 1. opt-out guard (ADR-0163: code_graph.enabled off for this repo in the
    #    runtime-config store — global-off OR a per-repo false override).
    if config.is_opted_out(repo_path):
        return {"skipped": True, "reason": "opted_out", "repo_path": repo_path}

    # 2. real-repo identity — BEFORE any worktree exists.
    canonical_root, subdir = _canonical_identity(repo_path)

    # 3. default branch (no remote / offline → None → skip, not fallback).
    default = resolve_default_branch(repo_path)
    if not default:
        return {
            "skipped": True,
            "reason": "no_remote_or_default_branch",
            "canonical_root": canonical_root,
            "subdir": subdir,
        }

    # 4. fetch the default branch (offline/fetch-fail → skip, not fallback).
    try:
        _git(["fetch", "origin", default], cwd=repo_path)
    except subprocess.CalledProcessError:
        return {
            "skipped": True,
            "reason": "fetch_failed",
            "canonical_root": canonical_root,
            "subdir": subdir,
            "default_branch": default,
        }

    # 5-7. materialize origin/<default> in a temp worktree, index, always clean up.
    parent = tempfile.mkdtemp(prefix="yadgar-code-graph-")
    wt = str(Path(parent) / "wt")  # non-existent subpath — git worktree add wants that
    worktree_added = False
    try:
        _git(
            ["worktree", "add", "--detach", wt, f"origin/{default}"],
            cwd=repo_path,
        )
        worktree_added = True

        # The indexed subdir = the temp worktree + the same relative subdir, so a
        # monorepo-leaf index stays confined to the leaf (CBM_ALLOWED_ROOT = that path).
        index_path = str(Path(wt) / subdir) if subdir else wt
        idx = runner.index_repository(index_path, allowed_root=index_path)

        return {
            "indexed": True,
            "canonical_root": canonical_root,
            "subdir": subdir,
            "default_branch": default,
            "project": idx.get("project"),
            "index_result": idx,
        }
    finally:
        if worktree_added:
            try:
                _git(["worktree", "remove", "--force", wt], cwd=repo_path)
            except subprocess.CalledProcessError:
                pass  # best-effort; the rmtree below still reclaims the dir
        shutil.rmtree(parent, ignore_errors=True)
