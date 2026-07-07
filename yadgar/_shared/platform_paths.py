"""Cross-platform path resolution for Claude Code config directories.

Supports Linux, macOS, and Windows.
Used by install-subagents and config-sync commands (v5.44.0).

No hardcoded /home/<user> paths — always derives from pathlib.Path.home().
"""

from __future__ import annotations

import os
import platform
import shutil
from pathlib import Path

from yadgar._shared.observability.observe import observe


@observe(tier="stage")
def get_claude_config_dir() -> Path:
    """Return the Claude Code user config directory for the current OS.

    - Linux / other POSIX: ~/.claude/
    - macOS:               ~/Library/Application Support/Claude/
    - Windows:             %APPDATA%\\Claude\\
    """
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Claude"
    if system == "Windows":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "Claude"
        return Path.home() / "AppData" / "Roaming" / "Claude"
    # Linux and other POSIX
    return Path.home() / ".claude"


def get_claude_agents_dir() -> Path:
    """Return the Claude Code agents directory (~/.claude/agents/ or OS equivalent)."""
    return get_claude_config_dir() / "agents"


def get_claude_settings_path() -> Path:
    """Return the Claude Code settings.json path."""
    return get_claude_config_dir() / "settings.json"


@observe(tier="hot")
def is_nix_managed() -> bool:
    """True if the system is NixOS or nix-managed — install scripts should be skipped.

    Detects:
    - /etc/NIXOS file exists
    - nixos-version command is available on PATH
    """
    if Path("/etc/NIXOS").exists():
        return True
    if shutil.which("nixos-version") is not None:
        return True
    return False
