#!/usr/bin/env python3
"""I-check: every major subsystem package has a README.md (existence only).

T2 Car D doc scaffold (layer-boundary train, user-agreed 2026-07-09):
README.md per SUBSYSTEM (layer roots + major packages), AGENTS.md per LAYER
root, leaf docs stay in docstrings (they feed the repo-wiki generator).

This lint checks EXISTENCE only — content quality is a review concern, not a
hook concern. Add a directory here when a new subsystem package is created.

Usage: python scripts/check_subsystem_readmes.py
Exit 1 when any required README.md / AGENTS.md is missing.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Layer roots: README.md (subsystem map) + AGENTS.md (placement laws).
_LAYER_ROOTS = [
    "yadgar/_shared",
    "yadgar/backend",
    "yadgar/core",
]

# Major subsystem packages: README.md required.
_SUBSYSTEMS = [
    "yadgar/_shared/storage",
    "yadgar/_shared/retrieval",
    "yadgar/_shared/config",
    "yadgar/_shared/observability",
    "yadgar/_shared/security",
    "yadgar/_shared/wiki",
    "yadgar/backend/embed_service",
    "yadgar/backend/consolidation",
    "yadgar/core/server",
    "yadgar/core/viz",
    "yadgar/core/daemon",
    "yadgar/core/cli",
    "yadgar/core/install",
    "yadgar/core/seed",
    "yadgar/core/hooks",
]


def main() -> int:
    missing: list[str] = []
    for root in _LAYER_ROOTS:
        for doc in ("README.md", "AGENTS.md"):
            if not (_REPO_ROOT / root / doc).is_file():
                missing.append(f"{root}/{doc}")
    for pkg in _SUBSYSTEMS:
        if not (_REPO_ROOT / pkg).is_dir():
            missing.append(f"{pkg}/ (directory gone — update this list)")
        elif not (_REPO_ROOT / pkg / "README.md").is_file():
            missing.append(f"{pkg}/README.md")

    if missing:
        print("Subsystem-README lint FAILED — missing docs:")
        for m in missing:
            print(f"  - {m}")
        print(
            "\nEvery major subsystem package carries a README.md (what it is, how it"
            "\nconnects, seams); layer roots also carry AGENTS.md (placement laws)."
            "\nSee the T2 layer-boundary plan, Car D."
        )
        return 1
    print(f"Subsystem-README lint OK ({len(_LAYER_ROOTS)} layers, {len(_SUBSYSTEMS)} subsystems)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
