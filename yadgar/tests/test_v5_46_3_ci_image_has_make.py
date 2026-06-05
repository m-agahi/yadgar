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
        # 'make' appears as a continuation-line arg in the RUN block.
        # Verify: apt-get install block exists AND make is listed in it.
        assert "apt-get install" in content, "Dockerfile.ci must have apt-get install block"
        # 'make' must appear as a non-comment line in the file
        non_comment_lines = [ln for ln in content.splitlines() if not ln.strip().startswith("#")]
        non_comment = "\n".join(non_comment_lines)
        assert "make" in non_comment, (
            "Dockerfile.ci must install 'make' (as apt-get continuation arg). "
            "Found in comments only or not at all."
        )

    def test_dockerfile_ci_base_python_3_14(self):
        content = DOCKERFILE_CI.read_text()
        assert "python:3.14" in content, "Dockerfile.ci must use python:3.14 base image"

    def test_dockerfile_ci_has_git(self):
        content = DOCKERFILE_CI.read_text()
        non_comment = "\n".join(ln for ln in content.splitlines() if not ln.strip().startswith("#"))
        assert "apt-get install" in non_comment, "Dockerfile.ci must have apt-get install"
        assert "git" in non_comment, (
            "Dockerfile.ci must install 'git' (as apt-get continuation arg)"
        )

    def test_dockerfile_ci_has_curl(self):
        content = DOCKERFILE_CI.read_text()
        non_comment = "\n".join(ln for ln in content.splitlines() if not ln.strip().startswith("#"))
        assert "apt-get install" in non_comment, "Dockerfile.ci must have apt-get install"
        assert "curl" in non_comment, (
            "Dockerfile.ci must install 'curl' (as apt-get continuation arg)"
        )

    def test_dockerfile_ci_has_oci_label(self):
        content = DOCKERFILE_CI.read_text()
        assert "org.opencontainers.image.source" in content, (
            "Dockerfile.ci must include OCI image.source LABEL"
        )
