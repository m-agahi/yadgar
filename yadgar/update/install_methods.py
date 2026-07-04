"""v5.48.0 — Install-method detection and upgrade-command generation.

Detection order (matches detect_install_method.sh):
  1. which yadgar → real path
  2. Match against: /nix/store/* → nix-flake
                    */Cellar/yadgar/* → brew
                    */.local/pipx/venvs/yadgar/* → pipx
  3. If file content starts with "docker run" → container
  4. If .git dir ancestor exists → source
  5. Fallback → unknown

Returns:
  detect_install_method() → str: one of pipx/brew/nix-flake/container/source/unknown/not_installed
  upgrade_command(method) → str: appropriate upgrade incantation
  can_self_install(method) → bool: True only for pipx and source
"""

from __future__ import annotations

import os
import subprocess

from yadgar.observability.observe import observe


@observe(tier="stage")
def detect_install_method() -> str:
    """Detect how yadgar is installed on this system.

    Uses subprocess to call 'which yadgar', then inspects the resolved path.
    Returns one of: pipx / brew / nix-flake / container / source / unknown / not_installed.
    """
    try:
        raw = subprocess.check_output(
            ["which", "yadgar"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):  # fmt: skip
        return "not_installed"

    if not raw:
        return "not_installed"

    # Resolve symlinks to find the real path
    real = os.path.realpath(raw)

    # nix-flake: resolves into /nix/store/
    if "/nix/store/" in real:
        return "nix-flake"

    # brew: resolves into Cellar
    if "/Cellar/yadgar/" in real:
        return "brew"

    # pipx: resolves into .local/pipx/venvs/yadgar/
    if "/.local/pipx/venvs/yadgar/" in real:
        return "pipx"

    # container: shim whose first line invokes docker run
    if _is_docker_shim(raw):
        return "container"

    # source: any ancestor directory contains a .git directory
    if _has_git_ancestor(real):
        return "source"

    return "unknown"


@observe(tier="stage")
def _is_docker_shim(path: str) -> bool:
    """Return True if 'path' is a shell script whose first content line calls docker run."""
    try:
        with open(path) as fh:
            content = fh.read(256)
        return "docker run" in content
    except OSError:
        return False


@observe(tier="stage")
def _has_git_ancestor(path: str) -> bool:
    """Return True if any ancestor directory of 'path' contains a .git entry."""
    current = os.path.dirname(os.path.abspath(path))
    while current and current != os.path.dirname(current):
        if os.path.exists(os.path.join(current, ".git")):
            return True
        current = os.path.dirname(current)
    return False


@observe(tier="stage")
def upgrade_command(method: str) -> str:
    """Return the upgrade command string for the given install method.

    The returned string is informational — the user runs it manually.
    v5.48 ships CHECK-ONLY; --install deferred to v5.49 (graceful-restart primitive needed).
    """
    if method == "pipx":
        return "pipx upgrade yadgar"
    if method == "brew":
        return "brew upgrade yadgar"
    if method == "nix-flake":
        return (
            "nix profile upgrade '.*yadgar.*'  "
            "# or: nix flake update && nix profile install .#yadgar  "
            "# See https://pypi.org/project/yadgar/ for details"
        )
    if method == "container":
        return (
            "docker pull docker.io/openfantasy/yadgar:latest && "
            "systemctl --user restart yadgar  "
            "# macOS: launchctl kickstart -k gui/$UID/com.openfantasy.yadgar"
        )
    if method == "source":
        return "cd $(git rev-parse --show-toplevel) && git pull && pip install -e ."
    # unknown / not_installed / fallback
    return (
        "Cannot determine install method. "
        "See https://pypi.org/project/yadgar/ for manual upgrade steps."
    )


@observe(tier="stage")
def can_self_install(method: str) -> bool:
    """Return True if the daemon process can perform the upgrade itself.

    v5.48 CHECK-ONLY: can_self_install drives whether action=install is permitted via
    the API, but since --install is DROPPED in v5.48, this function is used only
    to surface the 400 guard correctly (action=install is always rejected).

    Conservative policy:
      pipx  → True  (writable ~/.local/pipx/venvs/)
      source → True  (editable install in user-owned directory)
      brew / nix-flake / container → False (read-only or requires restart from outside)
    """
    if method in ("pipx", "source"):
        return True
    return False
