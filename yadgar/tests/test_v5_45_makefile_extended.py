"""v5.45.0 follow-up TDD — extended Makefile targets (RED first).

Covers:
- pull-images dry-run shows correct pull commands for both images
- enable-units dry-run shows daemon-reload + enable --now yadgar.target
- bootstrap-secrets YADGAR_TEST_DRYRUN=1 writes dummy secrets.env to tmp dir + chmod 600
- restore errors out clearly without YADGAR_RESTORE_DB set
- bootstrap_secrets.sh is idempotent (second run skips prompts)
"""

import os
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MAKEFILE = REPO_ROOT / "Makefile"
BOOTSTRAP_SH = REPO_ROOT / "scripts" / "install" / "bootstrap_secrets.sh"
RESTORE_SH = REPO_ROOT / "scripts" / "install" / "restore.sh"


def _make_dry_run(*targets: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    """Run `make -n <targets>` (dry-run) in repo root."""
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


def _run_script(
    script: Path, args: list[str] | None = None, extra_env: dict | None = None
) -> subprocess.CompletedProcess:
    """Run a bash script directly."""
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    cmd = ["bash", str(script)] + (args or [])
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
    )


class TestPullImages:
    """make pull-images dry-run must show correct pull commands."""

    def test_pull_images_target_exists(self):
        """make -n pull-images must exit 0."""
        result = _make_dry_run("pull-images")
        assert result.returncode == 0, (
            f"make -n pull-images failed\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_pull_images_shows_backend_image(self):
        """make -n pull-images output must reference yadgar-backend image."""
        result = _make_dry_run("pull-images")
        combined = result.stdout + result.stderr
        assert "yadgar-backend" in combined, (
            f"Expected 'yadgar-backend' in pull-images dry-run output\n{combined[:800]}"
        )

    def test_pull_images_shows_core_image(self):
        """make -n pull-images output must reference yadgar core image (openfantasy/yadgar)."""
        result = _make_dry_run("pull-images")
        combined = result.stdout + result.stderr
        # "openfantasy/yadgar" appears in both images — either is fine; just confirm pull appears
        assert "openfantasy/yadgar" in combined, (
            f"Expected 'openfantasy/yadgar' in pull-images dry-run output\n{combined[:800]}"
        )

    def test_pull_images_shows_version_from_server_json(self):
        """pull-images must reference the version from server.json, not 'latest'."""
        result = _make_dry_run("pull-images")
        combined = result.stdout + result.stderr
        # Version from server.json
        import json

        server_json = REPO_ROOT / "server.json"
        if server_json.exists():
            ver = json.loads(server_json.read_text())["version"]
            assert ver in combined, (
                f"Expected version '{ver}' from server.json in pull-images output\n{combined[:800]}"
            )


class TestEnableUnits:
    """make enable-units dry-run must show systemd commands."""

    def test_enable_units_target_exists(self):
        """make -n enable-units must exit 0."""
        result = _make_dry_run("enable-units")
        assert result.returncode == 0, (
            f"make -n enable-units failed\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_enable_units_shows_daemon_reload(self):
        """make -n enable-units output must contain daemon-reload."""
        result = _make_dry_run("enable-units")
        combined = result.stdout + result.stderr
        assert "daemon-reload" in combined, (
            f"Expected 'daemon-reload' in enable-units dry-run output\n{combined[:600]}"
        )

    def test_enable_units_shows_enable_now(self):
        """make -n enable-units output must contain enable --now."""
        result = _make_dry_run("enable-units")
        combined = result.stdout + result.stderr
        assert "enable" in combined and "--now" in combined, (
            f"Expected 'enable --now' in enable-units dry-run output\n{combined[:600]}"
        )

    def test_enable_units_references_yadgar_target(self):
        """make -n enable-units output must reference yadgar.target."""
        result = _make_dry_run("enable-units")
        combined = result.stdout + result.stderr
        assert "yadgar.target" in combined, (
            f"Expected 'yadgar.target' in enable-units dry-run output\n{combined[:600]}"
        )


class TestBootstrapSecrets:
    """bootstrap-secrets with YADGAR_TEST_DRYRUN=1 must write secrets.env to tmp + chmod 600."""

    def test_bootstrap_secrets_target_exists(self):
        """make -n bootstrap-secrets must exit 0."""
        result = _make_dry_run("bootstrap-secrets")
        assert result.returncode == 0, (
            f"make -n bootstrap-secrets failed\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_bootstrap_secrets_script_exists(self):
        """scripts/install/bootstrap_secrets.sh must exist."""
        assert BOOTSTRAP_SH.exists(), f"bootstrap_secrets.sh not found at {BOOTSTRAP_SH}"

    def test_bootstrap_secrets_script_is_executable_via_bash(self):
        """bootstrap_secrets.sh must be valid bash (bash -n passes)."""
        result = subprocess.run(
            ["bash", "-n", str(BOOTSTRAP_SH)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"bash -n check failed on bootstrap_secrets.sh\n{result.stderr}"
        )

    def test_bootstrap_secrets_dryrun_writes_file(self):
        """YADGAR_TEST_DRYRUN=1 must write a dummy secrets.env to YADGAR_TEST_SECRETS_PATH."""
        with tempfile.TemporaryDirectory() as tmpdir:
            secrets_path = Path(tmpdir) / "secrets.env"
            result = _run_script(
                BOOTSTRAP_SH,
                extra_env={
                    "YADGAR_TEST_DRYRUN": "1",
                    "YADGAR_TEST_SECRETS_PATH": str(secrets_path),
                    # Suppress interactive prompts
                    "INSTALL_NONINTERACTIVE": "1",
                },
            )
            assert result.returncode == 0, (
                f"bootstrap_secrets.sh DRYRUN failed\nstdout: {result.stdout}\nstderr: {result.stderr}"
            )
            assert secrets_path.exists(), (
                f"secrets.env not created at {secrets_path}\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}"
            )

    def test_bootstrap_secrets_dryrun_file_mode_600(self):
        """secrets.env written in dryrun must have mode 600."""
        with tempfile.TemporaryDirectory() as tmpdir:
            secrets_path = Path(tmpdir) / "secrets.env"
            _run_script(
                BOOTSTRAP_SH,
                extra_env={
                    "YADGAR_TEST_DRYRUN": "1",
                    "YADGAR_TEST_SECRETS_PATH": str(secrets_path),
                    "INSTALL_NONINTERACTIVE": "1",
                },
            )
            if not secrets_path.exists():
                pytest.skip("secrets.env not created — bootstrap_secrets.sh not implemented yet")
            file_mode = stat.S_IMODE(secrets_path.stat().st_mode)
            assert file_mode == 0o600, f"secrets.env mode is {oct(file_mode)}, expected 0o600"

    def test_bootstrap_secrets_dryrun_file_has_required_keys(self):
        """secrets.env written by dryrun must contain all 6 credential keys."""
        with tempfile.TemporaryDirectory() as tmpdir:
            secrets_path = Path(tmpdir) / "secrets.env"
            _run_script(
                BOOTSTRAP_SH,
                extra_env={
                    "YADGAR_TEST_DRYRUN": "1",
                    "YADGAR_TEST_SECRETS_PATH": str(secrets_path),
                    "INSTALL_NONINTERACTIVE": "1",
                },
            )
            if not secrets_path.exists():
                pytest.skip("secrets.env not created — bootstrap_secrets.sh not implemented yet")
            content = secrets_path.read_text()
            for key in (
                "SURREAL_USER",
                "SURREAL_PASS",
                "YADGAR_RW_USER",
                "YADGAR_RW_PASS",
                "YADGAR_RO_USER",
                "YADGAR_RO_PASS",
            ):
                assert key in content, f"Missing key '{key}' in generated secrets.env"

    def test_bootstrap_secrets_idempotent(self):
        """Second run with existing complete file must skip prompts and exit 0."""
        with tempfile.TemporaryDirectory() as tmpdir:
            secrets_path = Path(tmpdir) / "secrets.env"

            # First run — create
            result1 = _run_script(
                BOOTSTRAP_SH,
                extra_env={
                    "YADGAR_TEST_DRYRUN": "1",
                    "YADGAR_TEST_SECRETS_PATH": str(secrets_path),
                    "INSTALL_NONINTERACTIVE": "1",
                },
            )
            if result1.returncode != 0 or not secrets_path.exists():
                pytest.skip("First run failed — bootstrap_secrets.sh not implemented yet")

            secrets_path.read_text()

            # Second run — should skip prompts, not overwrite
            result2 = _run_script(
                BOOTSTRAP_SH,
                extra_env={
                    "YADGAR_TEST_DRYRUN": "1",
                    "YADGAR_TEST_SECRETS_PATH": str(secrets_path),
                    "INSTALL_NONINTERACTIVE": "1",
                },
            )
            assert result2.returncode == 0, (
                f"Second (idempotent) run failed\nstdout: {result2.stdout}\nstderr: {result2.stderr}"
            )
            # File should indicate it was skipped (not necessarily identical bytes — timestamps differ)
            combined = result2.stdout + result2.stderr
            assert (
                "skip" in combined.lower()
                or "already" in combined.lower()
                or "exist" in combined.lower()
            ), f"Expected idempotency message in second run output\n{combined[:600]}"


class TestRestore:
    """make restore without YADGAR_RESTORE_DB must error clearly."""

    def test_restore_target_exists(self):
        """make -n restore must exit 0."""
        result = _make_dry_run("restore")
        assert result.returncode == 0, (
            f"make -n restore failed\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_restore_script_exists(self):
        """scripts/install/restore.sh must exist."""
        assert RESTORE_SH.exists(), f"restore.sh not found at {RESTORE_SH}"

    def test_restore_script_errors_without_db_env(self):
        """restore.sh without YADGAR_RESTORE_DB must exit non-zero with clear error."""
        env = dict(os.environ)
        env.pop("YADGAR_RESTORE_DB", None)
        env.pop("YADGAR_RESTORE_ARCHIVE", None)
        result = subprocess.run(
            ["bash", str(RESTORE_SH)],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode != 0, (
            f"restore.sh must exit non-zero when YADGAR_RESTORE_DB is unset\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        combined = result.stdout + result.stderr
        assert "YADGAR_RESTORE_DB" in combined, (
            f"Error message must mention YADGAR_RESTORE_DB\n{combined[:600]}"
        )

    def test_restore_script_is_executable_via_bash(self):
        """restore.sh must be valid bash (bash -n passes)."""
        result = subprocess.run(
            ["bash", "-n", str(RESTORE_SH)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"bash -n check failed on restore.sh\n{result.stderr}"


class TestSetupChainExtended:
    """make -n setup must include the new pull-images + bootstrap-secrets + enable-units steps."""

    def test_setup_chain_includes_pull_images(self):
        """make -n setup output must reference pull-images sub-make."""
        result = _make_dry_run("setup", extra_env={"INSTALL_NONINTERACTIVE": "1"})
        combined = result.stdout + result.stderr
        assert "pull-images" in combined, (
            f"Expected 'pull-images' in make -n setup output\n{combined[:1000]}"
        )

    def test_setup_chain_includes_bootstrap_secrets(self):
        """make -n setup output must reference bootstrap-secrets sub-make."""
        result = _make_dry_run("setup", extra_env={"INSTALL_NONINTERACTIVE": "1"})
        combined = result.stdout + result.stderr
        assert "bootstrap-secrets" in combined, (
            f"Expected 'bootstrap-secrets' in make -n setup output\n{combined[:1000]}"
        )

    def test_setup_chain_includes_enable_units(self):
        """make -n setup output must reference enable-units sub-make."""
        result = _make_dry_run("setup", extra_env={"INSTALL_NONINTERACTIVE": "1"})
        combined = result.stdout + result.stderr
        assert "enable-units" in combined, (
            f"Expected 'enable-units' in make -n setup output\n{combined[:1000]}"
        )
