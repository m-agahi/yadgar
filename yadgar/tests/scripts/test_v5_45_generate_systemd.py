"""v5.45.0 Step 1 TDD — generate_systemd.sh + systemd unit templates (RED)."""

import os
import shutil
import subprocess
from pathlib import Path

from yadgar.tests._paths import REPO_ROOT

GENERATE_SYSTEMD_SH = REPO_ROOT / "scripts" / "install" / "generate_systemd.sh"
SYSTEMD_TEMPLATES_DIR = REPO_ROOT / "scripts" / "install"
BASH = shutil.which("bash") or "/run/current-system/sw/bin/bash"


def _run_generate_systemd(
    target_dir: Path,
    extra_env: dict | None = None,
) -> subprocess.CompletedProcess:
    """Run generate_systemd.sh with OUTPUT_DIR pointing to a temp location."""
    env = dict(os.environ)
    env["YADGAR_SYSTEMD_OUTPUT_DIR"] = str(target_dir)
    env["YADGAR_RUNTIME"] = "podman"
    env["YADGAR_INSTALL_PREFIX"] = "/home/testuser/.yadgar"
    env["YADGAR_SECRETS_ENV_FILE"] = "/home/testuser/.yadgar/secrets.env"
    env["YADGAR_BACKEND_IMAGE"] = "openfantasy/yadgar-backend:5.45.0"
    env["YADGAR_CORE_IMAGE"] = "openfantasy/yadgar:5.45.0"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [BASH, str(GENERATE_SYSTEMD_SH)],
        capture_output=True,
        text=True,
        env=env,
    )


class TestV5_45GenerateSystemd:
    """Tests for scripts/install/generate_systemd.sh."""

    def test_v5_45_generate_systemd_script_exists(self):
        """generate_systemd.sh must exist."""
        assert GENERATE_SYSTEMD_SH.exists(), (
            f"scripts/install/generate_systemd.sh not found at {GENERATE_SYSTEMD_SH}"
        )

    def test_v5_45_systemd_template_files_exist(self):
        """Three .in template files must exist under scripts/install/."""
        for name in ["yadgar.service.in", "yadgar-backend.service.in", "yadgar.target.in"]:
            path = SYSTEMD_TEMPLATES_DIR / name
            assert path.exists(), f"Template not found: {path}"

    def test_v5_45_yadgar_target_template_has_both_services(self):
        """yadgar.target.in must list Wants=yadgar.service yadgar-backend.service."""
        target_template = SYSTEMD_TEMPLATES_DIR / "yadgar.target.in"
        content = target_template.read_text()
        assert "yadgar.service" in content, "yadgar.target.in must mention yadgar.service"
        assert "yadgar-backend.service" in content, (
            "yadgar.target.in must mention yadgar-backend.service"
        )
        assert "Wants" in content, "yadgar.target.in must contain Wants= directive"

    def test_v5_45_generate_systemd_writes_yadgar_target(self, tmp_path):
        """generate_systemd.sh must write yadgar.target to output dir."""
        result = _run_generate_systemd(tmp_path)
        assert result.returncode == 0, (
            f"generate_systemd.sh failed\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        target_file = tmp_path / "yadgar.target"
        assert target_file.exists(), (
            f"yadgar.target not written to {tmp_path}\nfiles in dir: {list(tmp_path.iterdir())}"
        )

    def test_v5_45_generate_systemd_writes_yadgar_service(self, tmp_path):
        """generate_systemd.sh must write yadgar.service."""
        result = _run_generate_systemd(tmp_path)
        assert result.returncode == 0, f"generate_systemd.sh failed\nstderr: {result.stderr}"
        assert (tmp_path / "yadgar.service").exists(), "yadgar.service not written"

    def test_v5_45_generate_systemd_writes_backend_service(self, tmp_path):
        """generate_systemd.sh must write yadgar-backend.service."""
        result = _run_generate_systemd(tmp_path)
        assert result.returncode == 0, f"generate_systemd.sh failed\nstderr: {result.stderr}"
        assert (tmp_path / "yadgar-backend.service").exists(), "yadgar-backend.service not written"

    def test_v5_45_generate_systemd_substitutes_runtime(self, tmp_path):
        """Generated yadgar.service must contain the runtime name (podman/docker)."""
        result = _run_generate_systemd(tmp_path, extra_env={"YADGAR_RUNTIME": "podman"})
        assert result.returncode == 0
        content = (tmp_path / "yadgar.service").read_text()
        assert "podman" in content or "@RUNTIME@" not in content, (
            "YADGAR_RUNTIME placeholder not substituted in yadgar.service"
        )

    def test_v5_45_generate_systemd_nix_symlink_guard(self, tmp_path):
        """generate_systemd.sh must exit non-zero if existing units are nix-managed symlinks."""
        # Create a fake systemd dir with a nix-store symlink
        systemd_dir = tmp_path / "systemd_user"
        systemd_dir.mkdir()
        fake_nix_path = tmp_path / "nix" / "store" / "abc123-yadgar" / "yadgar.service"
        fake_nix_path.parent.mkdir(parents=True)
        fake_nix_path.write_text("[Unit]\nDescription=fake\n")
        (systemd_dir / "yadgar.service").symlink_to(fake_nix_path)

        result = _run_generate_systemd(
            tmp_path,
            extra_env={
                "YADGAR_SYSTEMD_OUTPUT_DIR": str(systemd_dir),
                "YADGAR_TEST_SIMULATE_NIX_SYMLINK": "1",
            },
        )
        assert result.returncode != 0, (
            "generate_systemd.sh should refuse when existing units are nix-managed\n"
            f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
        )
        combined = result.stdout + result.stderr
        assert "nix" in combined.lower() or "managed" in combined.lower(), (
            "Error message should mention nix management"
        )

    def test_v5_45_generated_target_includes_both_services_after_render(self, tmp_path):
        """Rendered yadgar.target must have Wants=yadgar.service yadgar-backend.service."""
        result = _run_generate_systemd(tmp_path)
        assert result.returncode == 0
        content = (tmp_path / "yadgar.target").read_text()
        assert "yadgar.service" in content
        assert "yadgar-backend.service" in content
        assert "Wants" in content
