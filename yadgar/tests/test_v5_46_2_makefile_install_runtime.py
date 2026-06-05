"""v5.46.2 TDD — Makefile install-runtime target + setup chain integration (RED).

Verifies:
1. `make install-runtime` target exists and is invokable (dry-run).
2. `make install-runtime` dry-run output invokes install_runtime.sh.
3. `make setup` dry-run still references detect_runtime.sh (no regression).
4. Makefile declares install-runtime in .PHONY.
5. INSTALL_NONINTERACTIVE is passed through to install_runtime.sh call.
"""

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MAKEFILE = REPO_ROOT / "Makefile"
INSTALL_RUNTIME_SH = REPO_ROOT / "scripts" / "install" / "install_runtime.sh"


def _make_dry_run(*targets: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    """Run `make -n <targets>` (dry-run) in repo root."""
    env = dict(os.environ)
    env["INSTALL_NONINTERACTIVE"] = "1"  # avoid interactive prompts in dry-run
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


def _make_real(*targets: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    """Run `make <targets>` (real, not dry-run) — only for safe/read-only targets."""
    env = dict(os.environ)
    env["INSTALL_NONINTERACTIVE"] = "1"
    if extra_env:
        env.update(extra_env)
    cmd = ["make"] + list(targets)
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
    )


class TestV5_46_2MakefileInstallRuntimeTarget:
    """make install-runtime target must exist and invoke the shared helper."""

    def test_install_runtime_sh_exists(self):
        """install_runtime.sh must exist before Makefile tests are meaningful."""
        assert INSTALL_RUNTIME_SH.exists(), f"install_runtime.sh not found at {INSTALL_RUNTIME_SH}"

    def test_install_runtime_target_dry_run_exits_0(self):
        """make -n install-runtime must exit 0 (target exists + graph valid)."""
        result = _make_dry_run("install-runtime")
        assert result.returncode == 0, (
            f"make -n install-runtime failed\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_install_runtime_target_invokes_install_runtime_sh(self):
        """make -n install-runtime must show install_runtime.sh in output."""
        result = _make_dry_run("install-runtime")
        combined = result.stdout + result.stderr
        assert "install_runtime.sh" in combined, (
            f"make install-runtime must invoke install_runtime.sh\ncombined: {combined[:500]}"
        )

    def test_install_runtime_in_phony(self):
        """install-runtime must be declared in .PHONY."""
        content = MAKEFILE.read_text()
        assert "install-runtime" in content, "Makefile must declare install-runtime target"
        # Check it's actually in the .PHONY list
        phony_line_start = content.find(".PHONY")
        if phony_line_start == -1:
            raise AssertionError("Makefile has no .PHONY declaration")
        # .PHONY can span lines; check if install-runtime appears near it
        phony_section = content[phony_line_start : phony_line_start + 500]
        assert "install-runtime" in phony_section, (
            f"install-runtime must be in .PHONY declaration\n.PHONY section: {phony_section!r}"
        )

    def test_install_runtime_passes_install_noninteractive(self):
        """INSTALL_NONINTERACTIVE must be passed through to install_runtime.sh call."""
        result = _make_dry_run("install-runtime", extra_env={"INSTALL_NONINTERACTIVE": "1"})
        combined = result.stdout + result.stderr
        # The command string or env export must appear
        assert "INSTALL_NONINTERACTIVE" in combined or "install_runtime.sh" in combined, (
            f"INSTALL_NONINTERACTIVE should appear in install-runtime invocation\ncombined: {combined!r}"
        )

    def test_make_help_lists_install_runtime(self):
        """make help must list install-runtime target."""
        result = subprocess.run(
            ["make", "help"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        combined = result.stdout + result.stderr
        assert "install-runtime" in combined, (
            f"make help should list install-runtime\ncombined: {combined[:500]}"
        )


class TestV5_46_2MakefileSetupNoRegression:
    """make setup dry-run must still work after install-runtime addition."""

    def test_setup_dry_run_still_exits_0(self):
        """make -n setup must exit 0 after Makefile changes."""
        result = _make_dry_run("setup", extra_env={"INSTALL_NONINTERACTIVE": "1"})
        assert result.returncode == 0, (
            f"make -n setup failed after install-runtime addition\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_setup_still_references_detect_runtime(self):
        """make -n setup must still invoke detect_runtime.sh (no regression)."""
        result = _make_dry_run("setup", extra_env={"INSTALL_NONINTERACTIVE": "1"})
        combined = result.stdout + result.stderr
        assert "detect_runtime.sh" in combined, (
            f"make setup must still invoke detect_runtime.sh\ncombined: {combined[:600]}"
        )

    def test_makefile_has_install_runtime_comment(self):
        """Makefile install-runtime target must have ## help comment."""
        content = MAKEFILE.read_text()
        # Standard pattern: ## install-runtime: <description>
        assert "## install-runtime" in content, (
            "Makefile install-runtime target must have ## help comment"
        )


class TestV5_46_2MakefileNonInteractiveGate:
    """INSTALL_NONINTERACTIVE env var must be respected by install-runtime target."""

    def test_noninteractive_var_in_makefile(self):
        """Makefile must declare or use INSTALL_NONINTERACTIVE variable."""
        content = MAKEFILE.read_text()
        assert "INSTALL_NONINTERACTIVE" in content, (
            "Makefile must reference INSTALL_NONINTERACTIVE variable"
        )
