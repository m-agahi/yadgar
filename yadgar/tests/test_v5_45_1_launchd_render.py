"""v5.45.1 Step 1 TDD — plist template rendering + XML validity (RED).

Cross-platform: parses rendered plist as XML via stdlib xml.etree.ElementTree.
No macOS-specific tools required.
"""

import os
import shutil
import subprocess
import xml.etree.ElementTree as _stdlib_ET  # ParseError class only
from pathlib import Path

import defusedxml.ElementTree as ET  # safe parsing — defends against XXE

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LAUNCHD_TEMPLATE_DIR = REPO_ROOT / "scripts" / "install" / "launchd"
CORE_PLIST_TEMPLATE = LAUNCHD_TEMPLATE_DIR / "com.openfantasy.yadgar.plist.in"
BACKEND_PLIST_TEMPLATE = LAUNCHD_TEMPLATE_DIR / "com.openfantasy.yadgar-backend.plist.in"
GENERATE_LAUNCHD_SH = REPO_ROOT / "scripts" / "install" / "generate_launchd.sh"
BASH = shutil.which("bash") or "/run/current-system/sw/bin/bash"

# Template variable defaults for test rendering
# Templates use @VAR@ placeholder format (substituted by generate_launchd.sh via sed).
# Log paths use XDG convention: @YADGAR_HOME@/.local/share/yadgar/logs/
# (changed from ~/Library/Logs in v5.47 XDG migration — all launchd plists consistent).
_DEFAULT_ENV = {
    "YADGAR_INSTALL_PREFIX": "/home/testuser/.yadgar",
    "YADGAR_RUNTIME": "podman",
    "YADGAR_SECRETS_ENV_FILE": "/home/testuser/.yadgar/secrets.env",
    "YADGAR_CORE_IMAGE": "openfantasy/yadgar:5.45.1",
    "YADGAR_BACKEND_IMAGE": "openfantasy/yadgar-backend:5.45.1",
    "YADGAR_HOME": "/home/testuser",
}


def _run_generate_launchd(
    output_dir: Path,
    extra_env: dict | None = None,
) -> subprocess.CompletedProcess:
    """Run generate_launchd.sh with OUTPUT_DIR pointing to a temp location."""
    env = dict(os.environ)
    env.update(_DEFAULT_ENV)
    env["YADGAR_LAUNCHD_OUTPUT_DIR"] = str(output_dir)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [BASH, str(GENERATE_LAUNCHD_SH)],
        capture_output=True,
        text=True,
        env=env,
    )


def _render_template(template_path: Path, env: dict | None = None) -> str:
    """Render a .in template via @VAR@ substitutions matching generate_launchd.sh sed pattern."""
    substitutions = {**_DEFAULT_ENV, **(env or {})}
    content = template_path.read_text()
    for key, value in substitutions.items():
        content = content.replace("@" + key + "@", value)
    return content


class TestV5_45_1LaunchdTemplates:
    """Plist template files must exist and be valid XML."""

    def test_v5_45_1_launchd_template_dir_exists(self):
        """scripts/install/launchd/ directory must exist."""
        assert LAUNCHD_TEMPLATE_DIR.exists(), (
            f"scripts/install/launchd/ directory not found at {LAUNCHD_TEMPLATE_DIR}"
        )

    def test_v5_45_1_core_plist_template_exists(self):
        """com.openfantasy.yadgar.plist.in must exist."""
        assert CORE_PLIST_TEMPLATE.exists(), (
            f"Core plist template not found at {CORE_PLIST_TEMPLATE}"
        )

    def test_v5_45_1_backend_plist_template_exists(self):
        """com.openfantasy.yadgar-backend.plist.in must exist."""
        assert BACKEND_PLIST_TEMPLATE.exists(), (
            f"Backend plist template not found at {BACKEND_PLIST_TEMPLATE}"
        )

    def test_v5_45_1_core_plist_template_is_valid_xml(self):
        """Core plist template with vars substituted must parse as valid XML."""
        rendered = _render_template(CORE_PLIST_TEMPLATE)
        try:
            ET.fromstring(rendered)
        except _stdlib_ET.ParseError as exc:
            raise AssertionError(
                f"Core plist template is not valid XML after substitution: {exc}\n"
                f"Rendered:\n{rendered[:500]}"
            ) from exc

    def test_v5_45_1_backend_plist_template_is_valid_xml(self):
        """Backend plist template with vars substituted must parse as valid XML."""
        rendered = _render_template(BACKEND_PLIST_TEMPLATE)
        try:
            ET.fromstring(rendered)
        except _stdlib_ET.ParseError as exc:
            raise AssertionError(
                f"Backend plist template is not valid XML after substitution: {exc}\n"
                f"Rendered:\n{rendered[:500]}"
            ) from exc

    def test_v5_45_1_core_plist_label_is_correct(self):
        """Core plist must set Label to com.openfantasy.yadgar."""
        rendered = _render_template(CORE_PLIST_TEMPLATE)
        assert "com.openfantasy.yadgar" in rendered, (
            "Core plist Label key must contain 'com.openfantasy.yadgar'"
        )

    def test_v5_45_1_backend_plist_label_is_correct(self):
        """Backend plist must set Label to com.openfantasy.yadgar-backend."""
        rendered = _render_template(BACKEND_PLIST_TEMPLATE)
        assert "com.openfantasy.yadgar-backend" in rendered, (
            "Backend plist Label key must contain 'com.openfantasy.yadgar-backend'"
        )

    def test_v5_45_1_core_plist_has_run_at_load(self):
        """Core plist must set RunAtLoad=true."""
        rendered = _render_template(CORE_PLIST_TEMPLATE)
        assert "RunAtLoad" in rendered, "Core plist must contain RunAtLoad key"
        assert "<true/>" in rendered, "RunAtLoad must be set to true"

    def test_v5_45_1_core_plist_has_keep_alive(self):
        """Core plist must set KeepAlive=true."""
        rendered = _render_template(CORE_PLIST_TEMPLATE)
        assert "KeepAlive" in rendered, "Core plist must contain KeepAlive key"

    def test_v5_45_1_core_plist_has_program_arguments(self):
        """Core plist must contain ProgramArguments array."""
        rendered = _render_template(CORE_PLIST_TEMPLATE)
        assert "ProgramArguments" in rendered, "Core plist must have ProgramArguments"

    def test_v5_45_1_backend_plist_has_program_arguments(self):
        """Backend plist must contain ProgramArguments array."""
        rendered = _render_template(BACKEND_PLIST_TEMPLATE)
        assert "ProgramArguments" in rendered, "Backend plist must have ProgramArguments"

    def test_v5_45_1_core_plist_stdout_path_in_xdg_logs(self):
        """Core plist StandardOutPath must be under ~/.local/share/yadgar/logs/.

        v5.47 XDG migration: all launchd plists use @YADGAR_HOME@/.local/share/yadgar/logs/
        consistently. ~/Library/Logs convention was superseded.
        """
        rendered = _render_template(CORE_PLIST_TEMPLATE)
        assert "StandardOutPath" in rendered, "Core plist must set StandardOutPath"
        assert ".local/share/yadgar/logs" in rendered, (
            "StandardOutPath must be under ~/.local/share/yadgar/logs/ (XDG convention, v5.47+)"
        )

    def test_v5_45_1_core_plist_stderr_path_in_xdg_logs(self):
        """Core plist StandardErrorPath must be under ~/.local/share/yadgar/logs/.

        v5.47 XDG migration: all launchd plists use @YADGAR_HOME@/.local/share/yadgar/logs/
        consistently. ~/Library/Logs convention was superseded.
        """
        rendered = _render_template(CORE_PLIST_TEMPLATE)
        assert "StandardErrorPath" in rendered, "Core plist must set StandardErrorPath"
        assert ".local/share/yadgar/logs" in rendered, (
            "StandardErrorPath must be under ~/.local/share/yadgar/logs/ (XDG convention, v5.47+)"
        )

    def test_v5_45_1_core_plist_has_working_directory(self):
        """Core plist must set WorkingDirectory to YADGAR_INSTALL_PREFIX."""
        rendered = _render_template(CORE_PLIST_TEMPLATE)
        assert "WorkingDirectory" in rendered, "Core plist must have WorkingDirectory"
        assert "/home/testuser/.yadgar" in rendered, (
            "WorkingDirectory must be substituted with YADGAR_INSTALL_PREFIX"
        )

    def test_v5_45_1_core_plist_has_environment_variables(self):
        """Core plist must set EnvironmentVariables including PATH."""
        rendered = _render_template(CORE_PLIST_TEMPLATE)
        assert "EnvironmentVariables" in rendered, (
            "Core plist must contain EnvironmentVariables dict"
        )
        assert "PATH" in rendered, "EnvironmentVariables must include PATH"

    def test_v5_45_1_core_plist_path_includes_homebrew(self):
        """Core plist PATH must include /opt/homebrew/bin for Apple Silicon podman."""
        rendered = _render_template(CORE_PLIST_TEMPLATE)
        assert "/opt/homebrew/bin" in rendered, (
            "PATH in EnvironmentVariables must include /opt/homebrew/bin"
        )

    def test_v5_45_1_core_plist_process_type_background(self):
        """Core plist must set ProcessType=Background."""
        rendered = _render_template(CORE_PLIST_TEMPLATE)
        assert "ProcessType" in rendered, "Core plist must set ProcessType"
        assert "Background" in rendered, "ProcessType must be Background"

    def test_v5_45_1_core_plist_runtime_substituted(self):
        """Core plist must substitute YADGAR_RUNTIME (podman/docker) in ProgramArguments."""
        rendered = _render_template(CORE_PLIST_TEMPLATE)
        assert "podman" in rendered, "YADGAR_RUNTIME=podman must appear in rendered core plist"
        assert "${YADGAR_RUNTIME}" not in rendered, (
            "Unsubstituted ${YADGAR_RUNTIME} found in rendered core plist"
        )

    def test_v5_45_1_backend_plist_runtime_substituted(self):
        """Backend plist must substitute YADGAR_RUNTIME in ProgramArguments."""
        rendered = _render_template(BACKEND_PLIST_TEMPLATE)
        assert "podman" in rendered, "YADGAR_RUNTIME=podman must appear in rendered backend plist"

    def test_v5_45_1_core_plist_image_substituted(self):
        """Core plist must have YADGAR_CORE_IMAGE substituted."""
        rendered = _render_template(CORE_PLIST_TEMPLATE)
        assert "openfantasy/yadgar:5.45.1" in rendered, (
            "YADGAR_CORE_IMAGE not substituted in core plist"
        )

    def test_v5_45_1_backend_plist_backend_image_substituted(self):
        """Backend plist must have YADGAR_BACKEND_IMAGE substituted."""
        rendered = _render_template(BACKEND_PLIST_TEMPLATE)
        assert "openfantasy/yadgar-backend:5.45.1" in rendered, (
            "YADGAR_BACKEND_IMAGE not substituted in backend plist"
        )


class TestV5_45_1GenerateLaunchdScript:
    """generate_launchd.sh renders templates and writes plist files."""

    def test_v5_45_1_generate_launchd_script_exists(self):
        """generate_launchd.sh must exist."""
        assert GENERATE_LAUNCHD_SH.exists(), (
            f"generate_launchd.sh not found at {GENERATE_LAUNCHD_SH}"
        )

    def test_v5_45_1_generate_launchd_writes_core_plist(self, tmp_path):
        """generate_launchd.sh must write com.openfantasy.yadgar.plist."""
        result = _run_generate_launchd(tmp_path)
        assert result.returncode == 0, (
            f"generate_launchd.sh failed\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        plist_path = tmp_path / "com.openfantasy.yadgar.plist"
        assert plist_path.exists(), (
            f"com.openfantasy.yadgar.plist not written to {tmp_path}\n"
            f"files: {list(tmp_path.iterdir())}\nstdout: {result.stdout}"
        )

    def test_v5_45_1_generate_launchd_writes_backend_plist(self, tmp_path):
        """generate_launchd.sh must write com.openfantasy.yadgar-backend.plist."""
        result = _run_generate_launchd(tmp_path)
        assert result.returncode == 0, (
            f"generate_launchd.sh failed\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        plist_path = tmp_path / "com.openfantasy.yadgar-backend.plist"
        assert plist_path.exists(), (
            f"com.openfantasy.yadgar-backend.plist not written\nstdout: {result.stdout}"
        )

    def test_v5_45_1_generated_core_plist_is_valid_xml(self, tmp_path):
        """Rendered com.openfantasy.yadgar.plist must parse as valid XML."""
        result = _run_generate_launchd(tmp_path)
        assert result.returncode == 0, f"generate_launchd.sh failed: {result.stderr}"
        plist_path = tmp_path / "com.openfantasy.yadgar.plist"
        content = plist_path.read_text()
        try:
            ET.fromstring(content)
        except _stdlib_ET.ParseError as exc:
            raise AssertionError(
                f"Generated core plist is not valid XML: {exc}\nContent:\n{content[:600]}"
            ) from exc

    def test_v5_45_1_generated_backend_plist_is_valid_xml(self, tmp_path):
        """Rendered com.openfantasy.yadgar-backend.plist must parse as valid XML."""
        result = _run_generate_launchd(tmp_path)
        assert result.returncode == 0, f"generate_launchd.sh failed: {result.stderr}"
        plist_path = tmp_path / "com.openfantasy.yadgar-backend.plist"
        content = plist_path.read_text()
        try:
            ET.fromstring(content)
        except _stdlib_ET.ParseError as exc:
            raise AssertionError(
                f"Generated backend plist is not valid XML: {exc}\nContent:\n{content[:600]}"
            ) from exc

    def test_v5_45_1_generate_launchd_substitutes_runtime(self, tmp_path):
        """Generated plist must contain the runtime name (podman)."""
        result = _run_generate_launchd(tmp_path, extra_env={"YADGAR_RUNTIME": "podman"})
        assert result.returncode == 0
        content = (tmp_path / "com.openfantasy.yadgar.plist").read_text()
        assert "podman" in content, "YADGAR_RUNTIME not substituted in core plist"

    def test_v5_45_1_generate_launchd_substitutes_data_dir(self, tmp_path):
        """Generated plist must contain the data dir path."""
        result = _run_generate_launchd(
            tmp_path, extra_env={"YADGAR_INSTALL_PREFIX": "/home/testuser/.yadgar"}
        )
        assert result.returncode == 0
        content = (tmp_path / "com.openfantasy.yadgar.plist").read_text()
        assert "/home/testuser/.yadgar" in content, (
            "YADGAR_INSTALL_PREFIX not substituted in core plist"
        )
