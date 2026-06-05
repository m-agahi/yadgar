"""v5.46.3 TDD — Dockerfile.ci installs make (B6 fix).

Text-based assertion on Dockerfile.ci file content.
Cannot actually run docker build in test context.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE_CI = REPO_ROOT / "Dockerfile.ci"


class TestDockerfileCiHasMake:
    """Dockerfile.ci must install make via apt-get."""

    def test_dockerfile_ci_exists(self):
        assert DOCKERFILE_CI.exists(), f"Dockerfile.ci not found at {DOCKERFILE_CI}"

    def test_dockerfile_ci_installs_make(self):
        content = DOCKERFILE_CI.read_text()
        assert "make" in content, "Dockerfile.ci must install 'make' via apt-get"

    def test_dockerfile_ci_apt_get_make(self):
        content = DOCKERFILE_CI.read_text()
        # Must be in an apt-get install line, not just a comment
        lines = content.splitlines()
        apt_lines = [ln for ln in lines if "apt-get" in ln and "make" in ln]
        assert apt_lines, (
            "Dockerfile.ci must have an apt-get install line that includes 'make'. "
            f"Found no such line in:\n{content}"
        )

    def test_dockerfile_ci_base_python_3_14(self):
        content = DOCKERFILE_CI.read_text()
        assert "python:3.14" in content, "Dockerfile.ci must use python:3.14 base image"

    def test_dockerfile_ci_has_git(self):
        content = DOCKERFILE_CI.read_text()
        lines = content.splitlines()
        apt_lines = [ln for ln in lines if "apt-get" in ln and "git" in ln]
        assert apt_lines, "Dockerfile.ci must install 'git' via apt-get"

    def test_dockerfile_ci_has_curl(self):
        content = DOCKERFILE_CI.read_text()
        lines = content.splitlines()
        apt_lines = [ln for ln in lines if "apt-get" in ln and "curl" in ln]
        assert apt_lines, "Dockerfile.ci must install 'curl' via apt-get"

    def test_dockerfile_ci_has_oci_label(self):
        content = DOCKERFILE_CI.read_text()
        assert "org.opencontainers.image.source" in content, (
            "Dockerfile.ci must include OCI image.source LABEL"
        )
