"""v5.45.0 Step 1 TDD — uninstall.sh + data preservation (RED)."""

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
UNINSTALL_SH = REPO_ROOT / "scripts" / "install" / "uninstall.sh"


def _run_uninstall(yadgar_dir: Path, purge: bool = False) -> subprocess.CompletedProcess:
    """Run uninstall.sh with YADGAR_DIR pointing to tmp location."""
    cmd = ["bash", str(UNINSTALL_SH)]
    if purge:
        cmd.append("--purge")
    env = {
        **os.environ,
        "YADGAR_DIR": str(yadgar_dir),
        "YADGAR_TEST_MODE": "1",  # skip systemctl calls
        "YADGAR_SYSTEMD_OUTPUT_DIR": str(yadgar_dir / "systemd_user"),  # point to temp
    }
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


class TestV5_45Uninstall:
    """uninstall.sh preserves ~/.yadgar by default; removes on --purge."""

    def test_v5_45_uninstall_script_exists(self):
        """uninstall.sh must exist."""
        assert UNINSTALL_SH.exists(), f"scripts/install/uninstall.sh not found at {UNINSTALL_SH}"

    def test_v5_45_uninstall_preserves_data_dir(self, tmp_path):
        """uninstall.sh without --purge must preserve the YADGAR_DIR data."""
        yadgar_dir = tmp_path / ".yadgar"
        yadgar_dir.mkdir()
        (yadgar_dir / "memories.db").write_text("precious data")
        systemd_dir = yadgar_dir / "systemd_user"
        systemd_dir.mkdir()

        result = _run_uninstall(yadgar_dir, purge=False)
        assert result.returncode == 0, (
            f"uninstall.sh failed\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert yadgar_dir.exists(), (
            f"YADGAR_DIR was removed by uninstall without --purge!\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert (yadgar_dir / "memories.db").exists(), "memories.db must be preserved"

    def test_v5_45_uninstall_purge_removes_data_dir(self, tmp_path):
        """uninstall.sh --purge must remove YADGAR_DIR."""
        yadgar_dir = tmp_path / ".yadgar"
        yadgar_dir.mkdir()
        (yadgar_dir / "memories.db").write_text("data to remove")
        systemd_dir = yadgar_dir / "systemd_user"
        systemd_dir.mkdir()

        result = _run_uninstall(yadgar_dir, purge=True)
        assert result.returncode == 0, (
            f"uninstall.sh --purge failed\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert not yadgar_dir.exists(), (
            f"YADGAR_DIR should be removed after --purge\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_v5_45_uninstall_removes_unit_files(self, tmp_path):
        """uninstall.sh must remove systemd unit files from output dir."""
        yadgar_dir = tmp_path / ".yadgar"
        yadgar_dir.mkdir()
        systemd_dir = yadgar_dir / "systemd_user"
        systemd_dir.mkdir()
        # Create fake unit files
        (systemd_dir / "yadgar.service").write_text("[Unit]\n")
        (systemd_dir / "yadgar-backend.service").write_text("[Unit]\n")
        (systemd_dir / "yadgar.target").write_text("[Unit]\n")

        result = _run_uninstall(yadgar_dir, purge=False)
        assert result.returncode == 0, (
            f"uninstall.sh failed\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        # Unit files should be removed
        for unit in ["yadgar.service", "yadgar-backend.service", "yadgar.target"]:
            assert not (systemd_dir / unit).exists(), f"{unit} was not removed by uninstall"
