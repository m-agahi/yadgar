"""Guard: `.dockerignore` must not exclude any file Dockerfile.ci `COPY`s.

Regression guard for the v5.148.0 → #215 latent bug: task #50 added `uv.lock`
to `.dockerignore` ("unused in the pip install backend build"), but Dockerfile.ci
runs `COPY uv.lock /app/uv.lock` to bake the frozen test+ml deps (ADR-0089 lock
parity). No yadgar-ci rebuild happened between v5.131.0 and the #215 tomlkit lock
change, so the conflict stayed dormant ~5 months, then broke the rebuild at
`COPY uv.lock` ("no items matching glob … filtered out using .dockerignore").

The backend-rebuild-minutes guard is ci-release `backend_changed` (a tomllib
dep-section compare), NOT this ignore file — so un-ignoring uv.lock is safe.

Refs: task #50 (v5.148.0), ADR-0089, Dockerfile.ci.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
DOCKERFILE_CI = REPO_ROOT / "Dockerfile.ci"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"


def _ci_copied_literals() -> list[str]:
    """Literal (non-glob, non `--from=`) source paths COPYd by Dockerfile.ci."""
    text = DOCKERFILE_CI.read_text(encoding="utf-8")
    srcs: list[str] = []
    for line in text.splitlines():
        m = re.match(r"\s*COPY\s+(?!--from=)(\S+)\s+\S+", line)
        if not m:
            continue
        src = m.group(1)
        if any(ch in src for ch in "*?["):  # skip globs — exact match only
            continue
        srcs.append(src)
    return srcs


def _dockerignore_exact_excludes() -> set[str]:
    """Exact-match exclusion entries in .dockerignore (skip comments/blanks/negations/globs)."""
    excludes: set[str] = set()
    for raw in DOCKERIGNORE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        if any(ch in line for ch in "*?["):
            continue
        excludes.add(line.rstrip("/"))
    return excludes


def test_dockerfile_ci_copies_uv_lock() -> None:
    """Precondition: Dockerfile.ci still COPYs uv.lock (else this guard is moot)."""
    assert "uv.lock" in _ci_copied_literals(), (
        "Dockerfile.ci no longer COPYs uv.lock — update/remove this guard."
    )


def test_dockerignore_does_not_exclude_ci_copied_files() -> None:
    """Every literal path Dockerfile.ci COPYs must survive the .dockerignore filter."""
    copied = set(_ci_copied_literals())
    excluded = _dockerignore_exact_excludes()
    clash = copied & excluded
    assert not clash, (
        f".dockerignore excludes files that Dockerfile.ci COPYs: {sorted(clash)}. "
        "The yadgar-ci build will fail at COPY with "
        "'no items matching glob … filtered out using .dockerignore'. "
        "Remove these entries from .dockerignore."
    )
