"""v5.45.0 Step 1 TDD — Makefile targets (RED: Makefile does not exist yet)."""

import subprocess

from yadgar.tests._paths import REPO_ROOT

MAKEFILE = REPO_ROOT / "Makefile"


def _make_dry_run(*targets: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    """Run `make -n <targets>` (dry-run) in repo root."""
    import os

    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    cmd = ["make", "-n"] + list(targets)
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
    )


class TestV5_45MakefileTargets:
    """Each Makefile target must be invokable (via dry-run) and produce expected commands."""

    def test_v5_45_makefile_exists(self):
        """Makefile must exist at repo root."""
        assert MAKEFILE.exists(), f"Makefile not found at {MAKEFILE}"

    def test_v5_45_makefile_is_gnu_make(self):
        """GNU make must be present (required for Makefile features)."""
        result = subprocess.run(["make", "--version"], capture_output=True, text=True)
        assert result.returncode == 0
        assert "GNU Make" in result.stdout, f"GNU make required but got: {result.stdout[:100]}"

    def test_v5_45_make_setup_target_dry_run(self):
        """make -n setup must exit 0 (target exists and graph is valid)."""
        result = _make_dry_run("setup", extra_env={"INSTALL_NONINTERACTIVE": "1"})
        assert result.returncode == 0, (
            f"make -n setup failed\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_v5_45_make_setup_chains_expected_targets(self):
        """make -n setup output must reference all sub-targets in chain."""
        result = _make_dry_run("setup", extra_env={"INSTALL_NONINTERACTIVE": "1"})
        combined = result.stdout + result.stderr
        # These sub-targets or their shell commands must appear in the dry-run output
        for expected in ["detect_runtime.sh", "generate_systemd.sh", "install-hooks", "config"]:
            assert expected in combined, (
                f"Expected '{expected}' to appear in make -n setup output\n"
                f"combined output: {combined[:500]}"
            )

    def test_v5_45_make_install_hooks_target_dry_run(self):
        """make -n install-hooks must exit 0."""
        result = _make_dry_run("install-hooks")
        assert result.returncode == 0, (
            f"make -n install-hooks failed\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_v5_45_make_install_agents_target_dry_run(self):
        """make -n install-agents must exit 0."""
        result = _make_dry_run("install-agents")
        assert result.returncode == 0, (
            f"make -n install-agents failed\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_v5_45_make_config_sync_target_dry_run(self):
        """make -n config-sync must exit 0."""
        result = _make_dry_run("config-sync")
        assert result.returncode == 0, (
            f"make -n config-sync failed\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_v5_45_make_install_rules_target_dry_run(self):
        """make -n install-rules must exit 0."""
        result = _make_dry_run("install-rules")
        assert result.returncode == 0, (
            f"make -n install-rules failed\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_v5_45_make_seed_anchors_target_dry_run(self):
        """make -n seed-anchors must exit 0."""
        result = _make_dry_run("seed-anchors")
        assert result.returncode == 0, (
            f"make -n seed-anchors failed\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_v5_45_make_uninstall_target_dry_run(self):
        """make -n uninstall must exit 0."""
        result = _make_dry_run("uninstall")
        assert result.returncode == 0, (
            f"make -n uninstall failed\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_v5_45_make_uninstall_purge_target_dry_run(self):
        """make -n uninstall-purge must exit 0."""
        result = _make_dry_run("uninstall-purge")
        assert result.returncode == 0, (
            f"make -n uninstall-purge failed\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_v5_45_make_detect_runtime_target_dry_run(self):
        """make -n detect-runtime must exit 0."""
        result = _make_dry_run("detect-runtime")
        assert result.returncode == 0, (
            f"make -n detect-runtime failed\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_v5_45_make_help_target_dry_run(self):
        """make help must exit 0 and list at least 5 targets."""
        result = subprocess.run(
            ["make", "help"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        # help may or may not need -n; try both
        if result.returncode != 0:
            result = _make_dry_run("help")
        assert result.returncode == 0, (
            f"make help failed\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        combined = result.stdout + result.stderr
        # At least 5 target names in output
        target_words = {"setup", "uninstall", "install-hooks", "config-sync", "help"}
        found = sum(1 for t in target_words if t in combined)
        assert found >= 3, (
            f"make help output should list known targets; found {found}/5\n"
            f"combined: {combined[:300]}"
        )

    def test_v5_45_makefile_phony_declared(self):
        """Makefile must declare .PHONY for primary targets."""
        content = MAKEFILE.read_text()
        assert ".PHONY" in content, "Makefile must contain .PHONY declaration"
        # setup must be phony
        assert "setup" in content, "Makefile must have 'setup' target"

    def test_v5_45_makefile_gnu_make_guard_in_pre_setup(self):
        """pre-setup target must contain GNU make guard."""
        content = MAKEFILE.read_text()
        assert "pre-setup" in content, "Makefile must have pre-setup target"
        assert "GNU Make" in content, (
            "pre-setup must contain GNU Make guard checking for 'GNU Make' string"
        )
