"""Python shim for the yadgar-setup entrypoint.

Resolves the bundled yadgar-setup.sh from the wheel's share-data path and
executes it via subprocess. This file is the target of the [project.scripts]
entry:

    yadgar-setup = "yadgar.scripts.yadgar_setup:main"

Distribution paths supported:
  - pipx:     sys.prefix/share/yadgar/scripts/yadgar-setup.sh
  - brew:     Formula install block copies yadgar-setup.sh to bin/yadgar-setup directly;
              this shim is NOT used in the brew path.
  - nix:      flake.nix installPhase copies yadgar-setup.sh to $out/bin/yadgar-setup directly;
              this shim is NOT used in the nix path.

For repo-checkout users, `make setup` is the canonical path. This shim is for
pipx-only installs where no Makefile is available.
"""

import os
import sys
from pathlib import Path


def _find_setup_sh() -> Path:
    """Locate yadgar-setup.sh from the wheel share-data path."""
    # Primary: sys.prefix/share/yadgar/scripts/yadgar-setup.sh (wheel.shared-data)
    share_path = Path(sys.prefix) / "share" / "yadgar" / "scripts" / "yadgar-setup.sh"
    if share_path.exists():
        return share_path

    # Fallback: repo checkout layout (editable install or development)
    # This file is at yadgar/scripts/yadgar_setup.py; repo root is two levels up.
    repo_root = Path(__file__).parent.parent.parent
    repo_path = repo_root / "scripts" / "install" / "yadgar-setup.sh"
    if repo_path.exists():
        return repo_path

    print(
        "ERROR: yadgar-setup.sh not found.\n"
        f"  Looked in: {share_path}\n"
        f"  Also tried: {repo_root / 'scripts' / 'install' / 'yadgar-setup.sh'}\n"
        "  Is yadgar installed correctly? Try: pipx reinstall yadgar",
        file=sys.stderr,
    )
    sys.exit(1)


def main() -> None:
    """Exec yadgar-setup.sh, forwarding all CLI arguments."""
    setup_sh = _find_setup_sh()

    # Ensure executable bit (wheel.shared-data may not preserve it on some platforms)
    if not os.access(setup_sh, os.X_OK):
        os.chmod(setup_sh, 0o755)

    # exec replaces this process entirely — no wrapping overhead.
    os.execv("/bin/bash", ["/bin/bash", str(setup_sh), *sys.argv[1:]])
