"""v5.46.0 — nix flake.nix structural checks.

Checks flake.nix structure without requiring nix in PATH.
nix flake check --no-build skipped if nix unavailable.

RED phase: fails until flake.nix is created.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent.parent
FLAKE_NIX = REPO_ROOT / "flake.nix"
NIX = shutil.which("nix")


def test_flake_nix_exists():
    """flake.nix must exist at repo root."""
    assert FLAKE_NIX.exists(), f"Missing: {FLAKE_NIX}"


def test_flake_nix_has_inputs():
    """flake.nix must have an inputs block."""
    assert FLAKE_NIX.exists()
    content = FLAKE_NIX.read_text()
    assert "inputs" in content


def test_flake_nix_has_nixpkgs_input():
    """flake.nix must reference nixpkgs."""
    assert FLAKE_NIX.exists()
    content = FLAKE_NIX.read_text()
    assert "nixpkgs" in content


def test_flake_nix_has_outputs():
    """flake.nix must have an outputs function."""
    assert FLAKE_NIX.exists()
    content = FLAKE_NIX.read_text()
    assert "outputs" in content


def test_flake_nix_has_packages():
    """flake.nix outputs must include packages."""
    assert FLAKE_NIX.exists()
    content = FLAKE_NIX.read_text()
    assert "packages" in content


def test_flake_nix_has_nixos_modules():
    """flake.nix outputs must include nixosModules."""
    assert FLAKE_NIX.exists()
    content = FLAKE_NIX.read_text()
    assert "nixosModules" in content


def test_flake_nix_installs_yadgar_setup():
    """flake.nix installPhase must copy yadgar-setup.sh to $out/bin."""
    assert FLAKE_NIX.exists()
    content = FLAKE_NIX.read_text()
    assert "yadgar-setup" in content


def test_flake_nix_no_auto_setup():
    """flake.nix must NOT auto-invoke yadgar-setup (no activation hook calling it)."""
    assert FLAKE_NIX.exists()
    content = FLAKE_NIX.read_text()
    # The flake must not have an activation block that runs yadgar-setup automatically
    # (user runs it manually after install)
    assert (
        "activation" not in content or "yadgar-setup" not in content.split("activation")[1][:200]
    ), "flake.nix must not auto-invoke yadgar-setup in activation"


def test_flake_nix_uses_nixos_unstable_channel():
    """flake.nix must use nixos-unstable (Python 3.14 only in unstable)."""
    assert FLAKE_NIX.exists()
    content = FLAKE_NIX.read_text()
    assert "nixos-unstable" in content or "nixpkgs-unstable" in content, (
        "flake.nix must use nixos-unstable channel (Python 3.14 requires it)"
    )


@pytest.mark.skipif(not NIX, reason="nix not in PATH")
def test_flake_nix_check_no_build():
    """nix flake check --no-build must exit 0."""
    result = subprocess.run(
        ["nix", "flake", "check", "--no-build"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, f"nix flake check failed:\n{result.stdout}\n{result.stderr}"
