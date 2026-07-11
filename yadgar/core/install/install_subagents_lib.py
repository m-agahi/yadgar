"""Shared install_subagents implementation (v5.44.0 X4).

Both the CLI subcommand (yadgar/cli/install_subagents.py) and
the MCP tool (if added to server/tools/) call install_subagents_impl() here.

OS detection via yadgar.core.install.platform_paths — no hardcoded paths.

Idempotent: running twice is a no-op unless --force is set.
Nix carve-out: if is_nix_managed() returns True, skip with status=nix_managed.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from yadgar._shared.observability.observe import observe

logger = logging.getLogger(__name__)


def _get_bundled_agents_dir() -> Path:
    """Return the path to bundled agent templates inside the yadgar package."""
    return Path(__file__).parents[1] / "install_assets" / "agents"


@observe(tier="boundary")
def install_subagents_impl(
    home_dir: Path,
    dry_run: bool = False,
    force: bool = False,
    check: bool = False,
) -> dict:
    """Copy bundled agent .md templates to ~/.claude/agents/.

    Parameters
    ----------
    home_dir:
        User's home directory. Pass Path.home() for real usage or tmp_path in tests.
    dry_run:
        Compute what would be installed; print preview; don't write any files.
    force:
        Overwrite existing agent files even if they already exist.
    check:
        List files that would be installed without writing. Returns result dict
        with status="check" and "would_install" list.

    Returns
    -------
    dict with keys:
        status: "installed" | "no_changes" | "dry_run" | "check" | "nix_managed" | "error"
        installed: list of file names actually written (status=installed)
        would_install: list of file names that would be written (status=check/dry_run)
        agents_dir: str path to target agents directory
    """
    from yadgar.core.install.platform_paths import is_nix_managed  # noqa: PLC0415

    # Nix carve-out: nix repo owns ~/.claude/agents/ on NixOS
    if is_nix_managed():
        logger.info("install_subagents: NixOS detected — skipping (nix repo manages agent files)")
        return {
            "status": "nix_managed",
            "message": (
                "NixOS detected. Agent templates are managed by the nix repo. "
                "Run a nix rebuild to update agent definitions."
            ),
        }

    bundled_dir = _get_bundled_agents_dir()
    if not bundled_dir.exists():
        return {
            "status": "error",
            "reason": f"Bundled agents directory not found: {bundled_dir}",
        }

    # Compute target directory using home_dir (portable, no hardcoded paths)
    agents_dir = home_dir / ".claude" / "agents"

    # Collect all bundled .md files
    bundled_files = sorted(bundled_dir.glob("*.md"))
    if not bundled_files:
        return {
            "status": "error",
            "reason": f"No .md files found in bundled agents directory: {bundled_dir}",
        }

    # Determine which files would be installed (new or force-overwrite)
    would_install = []
    for src in bundled_files:
        dst = agents_dir / src.name
        if not dst.exists() or force:
            would_install.append(src.name)

    # --check mode: return list without writing
    if check:
        return {
            "status": "check",
            "would_install": would_install,
            "agents_dir": str(agents_dir),
        }

    # --dry-run mode: print preview, return info
    if dry_run:
        print(f"[dry-run] Would install to: {agents_dir}")
        for name in would_install:
            print(f"  + {name}")
        if not would_install:
            print("  (no changes)")
        return {
            "status": "dry_run",
            "would_install": would_install,
            "agents_dir": str(agents_dir),
        }

    # Nothing to install and not force
    if not would_install:
        return {
            "status": "no_changes",
            "installed": [],
            "agents_dir": str(agents_dir),
        }

    # Create directory
    agents_dir.mkdir(parents=True, exist_ok=True)

    # Copy files
    installed = []
    for src in bundled_files:
        name = src.name
        if name not in would_install:
            continue
        dst = agents_dir / name
        shutil.copy2(src, dst)
        installed.append(name)
        logger.info("install_subagents: installed %s → %s", name, dst)

    return {
        "status": "installed",
        "installed": installed,
        "agents_dir": str(agents_dir),
    }
