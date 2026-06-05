"""v5.46.3 TDD — workflow image refs use docker.io/openfantasy/yadgar-ci: prefix.

Verifies that after the image swap, workflows use the yadgar-ci image
instead of python:3.14-slim as the container image for test/build jobs.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_YAML = REPO_ROOT / ".forgejo" / "workflows" / "ci.yaml"
RELEASE_YAML = REPO_ROOT / ".forgejo" / "workflows" / "release.yaml"

# The yadgar-ci image prefix all job containers must use
YADGAR_CI_PREFIX = "docker.io/openfantasy/yadgar-ci:"


class TestYadgarCIImageRef:
    """Workflow job containers must reference yadgar-ci image, not python:3.14-slim."""

    def test_ci_yaml_uses_yadgar_ci_prefix(self):
        content = CI_YAML.read_text()
        assert YADGAR_CI_PREFIX in content, (
            f"ci.yaml must use '{YADGAR_CI_PREFIX}' as container image, not 'python:3.14-slim'"
        )

    def test_release_yaml_uses_yadgar_ci_prefix(self):
        content = RELEASE_YAML.read_text()
        assert YADGAR_CI_PREFIX in content, (
            f"release.yaml must use '{YADGAR_CI_PREFIX}' as container image, not 'python:3.14-slim'"
        )

    @pytest.mark.parametrize(
        "yaml_file,name",
        [
            (CI_YAML, "ci.yaml"),
            (RELEASE_YAML, "release.yaml"),
        ],
    )
    def test_no_python_slim_as_job_container(self, yaml_file, name):
        """python:3.14-slim must not be used as a job container image after swap."""
        content = yaml_file.read_text()
        lines = content.splitlines()
        # Find lines with 'image: python:3.14-slim' (job container refs)
        slim_container_lines = [ln.strip() for ln in lines if "image: python:3.14-slim" in ln]
        assert not slim_container_lines, (
            f"{name} still has python:3.14-slim as job container image: "
            f"{slim_container_lines}. Replace with {YADGAR_CI_PREFIX}5.46.3"
        )

    def test_ci_yaml_yadgar_ci_version_tag(self):
        """yadgar-ci image must include a version tag (not ':latest')."""
        content = CI_YAML.read_text()
        import re

        tags = re.findall(r"docker\.io/openfantasy/yadgar-ci:(\S+)", content)
        assert tags, "ci.yaml must reference docker.io/openfantasy/yadgar-ci:<version>"
        for tag in tags:
            assert tag != "latest", "yadgar-ci image must use a pinned version tag, not ':latest'"
