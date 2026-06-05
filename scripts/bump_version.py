#!/usr/bin/env python3
"""Bump the version in pyproject.toml.

Usage:
    python3 scripts/bump_version.py --new 5.46.1
    python3 scripts/bump_version.py --bump patch
    python3 scripts/bump_version.py --bump minor
    python3 scripts/bump_version.py --bump major
    python3 scripts/bump_version.py --dry-run --new 5.46.1
    python3 scripts/bump_version.py --current-version

Pre-commit hooks (sync_version + check_versions) cascade the bump to
server.json, flake.nix, and uv.lock on the next commit — no manual
editing of those files required.

The script refuses to proceed if pyproject.toml has unstaged edits
(pass --force to override the dirty guard in unusual recovery scenarios).
"""

import argparse
import re
import sys
from pathlib import Path


def _find_root(project_root: Path | None) -> Path:
    if project_root is not None:
        return project_root.resolve()
    # Walk up from this script to find root (pyproject.toml must be there)
    here = Path(__file__).resolve().parent
    for candidate in [here.parent, here]:
        if (candidate / "pyproject.toml").exists():
            return candidate
    return here.parent


def _read_version(pyproject: Path) -> str:
    text = pyproject.read_text()
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not m:
        print(f"ERROR: could not find version field in {pyproject}", file=sys.stderr)
        sys.exit(1)
    return m.group(1)


def _bump(version: str, part: str) -> str:
    try:
        major, minor, patch = (int(x) for x in version.split("."))
    except ValueError:
        print(
            f"ERROR: version '{version}' is not in MAJOR.MINOR.PATCH format",
            file=sys.stderr,
        )
        sys.exit(1)
    if part == "patch":
        patch += 1
    elif part == "minor":
        minor += 1
        patch = 0
    elif part == "major":
        major += 1
        minor = 0
        patch = 0
    else:
        print(f"ERROR: unknown bump part '{part}'; use patch|minor|major", file=sys.stderr)
        sys.exit(1)
    return f"{major}.{minor}.{patch}"


def _write_version(pyproject: Path, old: str, new: str) -> None:
    text = pyproject.read_text()
    new_text, n = re.subn(
        r'^(version\s*=\s*")[^"]+(")',
        rf"\g<1>{new}\g<2>",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if n == 0:
        print(f"ERROR: substitution failed in {pyproject}", file=sys.stderr)
        sys.exit(1)
    pyproject.write_text(new_text)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bump version in pyproject.toml.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--new", metavar="VERSION", help="Set explicit new version")
    action.add_argument(
        "--bump",
        choices=["patch", "minor", "major"],
        help="Increment patch, minor, or major component",
    )
    action.add_argument(
        "--current-version",
        action="store_true",
        help="Print current version and exit",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned change without writing",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        metavar="DIR",
        help="Override project root directory (default: auto-detect from script location)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Skip dirty-tree guard",
    )

    args = parser.parse_args()

    root = _find_root(args.project_root)
    pyproject = root / "pyproject.toml"

    if not pyproject.exists():
        print(f"ERROR: {pyproject} not found", file=sys.stderr)
        sys.exit(1)

    current = _read_version(pyproject)

    if args.current_version:
        print(current)
        sys.exit(0)

    if args.new is None and args.bump is None:
        parser.print_help()
        sys.exit(0)

    if args.new:
        new_version = args.new
    else:
        new_version = _bump(current, args.bump)

    if args.dry_run:
        print(f"DRY-RUN: {current} → {new_version}  ({pyproject})")
        sys.exit(0)

    _write_version(pyproject, current, new_version)
    print(f"Bumped: {current} → {new_version}  ({pyproject})")
    print("Next: git add pyproject.toml && git commit")
    print("Pre-commit hooks will auto-sync flake.nix, server.json, uv.lock.")


if __name__ == "__main__":
    main()
