"""v5.45.0 Step 1 TDD — install-subagents + config-sync Makefile targets (RED/GREEN)."""

import subprocess

import pytest

from yadgar.tests._paths import REPO_ROOT

MAKEFILE = REPO_ROOT / "Makefile"


class TestV5_45InstallSubagentsTarget:
    """make install-agents wraps yadgar install-subagents."""

    def test_v5_45_make_install_agents_invokes_yadgar_install_subagents(self):
        """make -n install-agents output must contain 'yadgar install-subagents'."""
        if not MAKEFILE.exists():
            pytest.skip("Makefile not yet created")
        result = subprocess.run(
            ["make", "-n", "install-agents"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0, (
            f"make -n install-agents failed\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "install-subagents" in result.stdout or "install_subagents" in result.stdout, (
            f"make install-agents must invoke yadgar install-subagents\nstdout: {result.stdout}"
        )


class TestV5_45ConfigSyncTarget:
    """make config-sync wraps yadgar config sync."""

    def test_v5_45_make_config_sync_invokes_yadgar_config_sync(self):
        """make -n config-sync output must contain 'yadgar config sync' or 'config'."""
        if not MAKEFILE.exists():
            pytest.skip("Makefile not yet created")
        result = subprocess.run(
            ["make", "-n", "config-sync"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0, (
            f"make -n config-sync failed\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        combined = result.stdout + result.stderr
        assert "config" in combined, (
            f"make config-sync must reference 'config' in commands\nstdout: {result.stdout}"
        )

    def test_v5_45_make_seed_anchors_invokes_yadgar_seed(self):
        """make -n seed-anchors output must contain 'yadgar seed'."""
        if not MAKEFILE.exists():
            pytest.skip("Makefile not yet created")
        result = subprocess.run(
            ["make", "-n", "seed-anchors"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0, (
            f"make -n seed-anchors failed\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "seed" in result.stdout, (
            f"make seed-anchors must reference 'seed' command\nstdout: {result.stdout}"
        )

    def test_v5_45_make_install_rules_invokes_append_script(self):
        """make -n install-rules output must contain 'append_claude_rules.sh'."""
        if not MAKEFILE.exists():
            pytest.skip("Makefile not yet created")
        result = subprocess.run(
            ["make", "-n", "install-rules"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0, (
            f"make -n install-rules failed\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "append_claude_rules" in result.stdout or "install-rules" in result.stdout, (
            f"make install-rules must reference append_claude_rules.sh\nstdout: {result.stdout}"
        )
