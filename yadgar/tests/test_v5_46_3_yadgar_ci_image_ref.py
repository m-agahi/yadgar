"""v5.46.3 TDD — workflow image refs use docker.io/openfantasy/yadgar-ci: prefix.

Verifies that after the image swap, workflows use the yadgar-ci image
instead of python:3.14-slim as the container image for test/build jobs.

Updated in v5.58 paydown-A:
- ci.yaml → ci-pr.yaml
- release.yaml → ci-release.yaml
- test_no_python_slim_as_job_container: validate.yaml uses python:3.14-slim
  intentionally (lightweight validate job, no test deps needed) — excluded from
  the slim-image check. Only ci-pr.yaml and ci-release.yaml are checked.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_YAML = REPO_ROOT / ".forgejo" / "workflows" / "ci-pr.yaml"
RELEASE_YAML = REPO_ROOT / ".forgejo" / "workflows" / "ci-release.yaml"

# The yadgar-ci image prefix all job containers must use
YADGAR_CI_PREFIX = "docker.io/openfantasy/yadgar-ci:"


class TestYadgarCIImageRef:
    """Workflow job containers must reference yadgar-ci image, not python:3.14-slim."""

    def test_ci_yaml_uses_yadgar_ci_prefix(self):
        content = CI_YAML.read_text()
        assert YADGAR_CI_PREFIX in content, (
            f"ci-pr.yaml must use '{YADGAR_CI_PREFIX}' as container image, not 'python:3.14-slim'"
        )

    def test_release_yaml_uses_yadgar_ci_prefix(self):
        content = RELEASE_YAML.read_text()
        assert YADGAR_CI_PREFIX in content, (
            f"ci-release.yaml must use '{YADGAR_CI_PREFIX}' as container image, not 'python:3.14-slim'"
        )

    def test_no_python_slim_as_test_job_container(self):
        """python:3.14-slim must not be used as the test/build job container image.

        ci-pr.yaml: the 'test' and 'viz-tests' jobs must use yadgar-ci image.
        The 'verify-version-bump' job is exempt — it uses python:3.14-slim
        intentionally (lightweight version check, no test deps needed).

        ci-release.yaml: all jobs use yadgar-ci or docker:cli (build-images).
        Neither should use python:3.14-slim.

        Note: validate.yaml also uses python:3.14-slim intentionally — excluded.
        """
        # ci-pr.yaml: test and viz-tests jobs must use yadgar-ci
        content_pr = CI_YAML.read_text()
        assert "docker.io/openfantasy/yadgar-ci:" in content_pr, (
            "ci-pr.yaml must reference yadgar-ci image for test/viz-tests jobs"
        )

        # ci-release.yaml: must not use python:3.14-slim (uses yadgar-ci + docker:cli)
        content_release = RELEASE_YAML.read_text()
        lines = content_release.splitlines()
        slim_lines = [ln.strip() for ln in lines if "image: python:3.14-slim" in ln]
        assert not slim_lines, (
            f"ci-release.yaml still has python:3.14-slim as job container: "
            f"{slim_lines}. Replace with {YADGAR_CI_PREFIX}<version>"
        )

    def test_ci_yaml_yadgar_ci_version_tag(self):
        """yadgar-ci image must include a version tag (not ':latest')."""
        content = CI_YAML.read_text()
        import re

        tags = re.findall(r"docker\.io/openfantasy/yadgar-ci:(\S+)", content)
        assert tags, "ci-pr.yaml must reference docker.io/openfantasy/yadgar-ci:<version>"
        for tag in tags:
            assert tag != "latest", "yadgar-ci image must use a pinned version tag, not ':latest'"
