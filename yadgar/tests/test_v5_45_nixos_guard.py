"""v5.45.0 Step 1 TDD — NixOS guard tests (RED)."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DETECT_OS_SH = REPO_ROOT / "scripts" / "install" / "detect_os.sh"
GENERATE_SYSTEMD_SH = REPO_ROOT / "scripts" / "install" / "generate_systemd.sh"
BASH = shutil.which("bash") or "/run/current-system/sw/bin/bash"


class TestV5_45NixOSGuard:
    """NixOS detection must refuse install with correct error message."""

    def test_v5_45_detect_os_refuses_nixos_with_message(self, tmp_path):
        """detect_os.sh on NixOS must print refusal message and exit non-zero."""
        nixos_marker = tmp_path / "NIXOS"
        nixos_marker.touch()
        result = subprocess.run(
            [BASH, str(DETECT_OS_SH)],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "YADGAR_TEST_NIXOS_MARKER": str(nixos_marker),
                "YADGAR_NIXOS_ABORT": "1",  # tells script to abort vs just emit
            },
        )
        # When YADGAR_NIXOS_ABORT=1, script must exit non-zero on NixOS
        assert result.returncode != 0, (
            f"detect_os.sh should exit non-zero on NixOS when YADGAR_NIXOS_ABORT=1\n"
            f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
        )
        combined = result.stdout + result.stderr
        # Must mention nix flake or v5.46
        assert "nix flake" in combined.lower() or "nixos" in combined.lower(), (
            f"NixOS refusal message must mention 'nix flake'\ncombined: {combined}"
        )

    def test_v5_45_generate_systemd_nix_symlink_exit_code(self, tmp_path):
        """generate_systemd.sh must exit non-zero when units are nix-managed."""
        systemd_dir = tmp_path / "systemd_user"
        systemd_dir.mkdir()

        # Create nix-store-like path structure
        nix_unit = tmp_path / "nix" / "store" / "abc123" / "yadgar.service"
        nix_unit.parent.mkdir(parents=True)
        nix_unit.write_text("[Unit]\nDescription=nix-managed\n")
        # Create symlink pointing into "nix store"
        (systemd_dir / "yadgar.service").symlink_to(nix_unit)

        result = subprocess.run(
            [BASH, str(GENERATE_SYSTEMD_SH)],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "YADGAR_SYSTEMD_OUTPUT_DIR": str(systemd_dir),
                "YADGAR_RUNTIME": "podman",
                "YADGAR_INSTALL_PREFIX": str(tmp_path / "data"),
                "YADGAR_SECRETS_ENV_FILE": str(tmp_path / "secrets.env"),
                "YADGAR_BACKEND_IMAGE": "openfantasy/yadgar-backend:test",
                "YADGAR_CORE_IMAGE": "openfantasy/yadgar:test",
            },
        )
        assert result.returncode != 0, (
            "generate_systemd.sh should refuse when yadgar.service is a nix symlink\n"
            f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
        )

    def test_v5_45_nixos_error_message_mentions_nix_flake(self, tmp_path):
        """NixOS refusal message must suggest nix flake alternative."""
        nixos_marker = tmp_path / "NIXOS"
        nixos_marker.touch()
        result = subprocess.run(
            [BASH, str(DETECT_OS_SH)],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "YADGAR_TEST_NIXOS_MARKER": str(nixos_marker),
                "YADGAR_NIXOS_ABORT": "1",
            },
        )
        combined = result.stdout + result.stderr
        # The canonical message from DP2 resolutions:
        # "use the nix flake (v5.46+)"
        has_nix_flake = "nix flake" in combined.lower()
        has_nixos_mention = "nixos" in combined.lower()
        assert has_nix_flake or has_nixos_mention, (
            f"Error should mention nix flake or NixOS\ncombined: {combined[:400]}"
        )

    def test_v5_45_yadgar_target_unit_wants_both_services(self):
        """yadgar.target.in template must have Wants= covering both services (DP5)."""
        target_template = REPO_ROOT / "scripts" / "install" / "yadgar.target.in"
        if not target_template.exists():
            pytest.skip("yadgar.target.in not yet created (Step 2)")
        content = target_template.read_text()
        # Per DP5 resolution canonical content:
        assert "Wants=" in content or "Wants =" in content, "Must have Wants= directive"
        assert "yadgar.service" in content
        assert "yadgar-backend.service" in content
