"""v5.46.3 TDD — workflows set YADGAR_CI_BRANCH: master (B2 fix).

Verifies .forgejo/workflows/{ci-pr.yaml,ci-release.yaml} set YADGAR_CI_BRANCH: master
at workflow or job level so the daemon can detect branch context in CI.

Updated in v5.58 paydown-A:
- ci.yaml → ci-pr.yaml
- release.yaml → ci-release.yaml
- test_workflow_uses_yadgar_ci_image parametrize updated to new filenames
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_YAML = REPO_ROOT / ".forgejo" / "workflows" / "ci-pr.yaml"
RELEASE_YAML = REPO_ROOT / ".forgejo" / "workflows" / "ci-release.yaml"


class TestCIBranchEnvVar:
    """Both workflow files must set YADGAR_CI_BRANCH: master."""

    def test_ci_yaml_exists(self):
        assert CI_YAML.exists(), f"ci-pr.yaml not found at {CI_YAML}"

    def test_release_yaml_exists(self):
        assert RELEASE_YAML.exists(), f"ci-release.yaml not found at {RELEASE_YAML}"

    def test_ci_yaml_has_yadgar_ci_branch(self):
        content = CI_YAML.read_text()
        assert "YADGAR_CI_BRANCH" in content, "ci-pr.yaml must set YADGAR_CI_BRANCH env var"

    def test_ci_yaml_branch_value_is_master(self):
        content = CI_YAML.read_text()
        assert "YADGAR_CI_BRANCH" in content
        # Find the non-comment line that sets YADGAR_CI_BRANCH and check for 'master'
        for line in content.splitlines():
            stripped = line.strip()
            if "YADGAR_CI_BRANCH" in stripped and not stripped.startswith("#"):
                assert "master" in stripped, (
                    f"YADGAR_CI_BRANCH must be set to 'master' in ci-pr.yaml, got: {stripped!r}"
                )
                break

    def test_release_yaml_has_yadgar_ci_branch(self):
        content = RELEASE_YAML.read_text()
        assert "YADGAR_CI_BRANCH" in content, "ci-release.yaml must set YADGAR_CI_BRANCH env var"

    def test_release_yaml_branch_value_is_master(self):
        content = RELEASE_YAML.read_text()
        assert "YADGAR_CI_BRANCH" in content
        for line in content.splitlines():
            stripped = line.strip()
            if "YADGAR_CI_BRANCH" in stripped and not stripped.startswith("#"):
                assert "master" in stripped, (
                    f"YADGAR_CI_BRANCH must be set to 'master' in ci-release.yaml, got: {stripped!r}"
                )
                break

    @pytest.mark.parametrize("yaml_file", [CI_YAML, RELEASE_YAML])
    def test_workflow_uses_yadgar_ci_image(self, yaml_file):
        """After image swap, workflows must not use python:3.14-slim as container image."""
        content = yaml_file.read_text()
        # The yadgar-ci image ref must be present
        assert "yadgar-ci" in content, (
            f"{yaml_file.name} must reference yadgar-ci image (docker.io/openfantasy/yadgar-ci:...)"
        )
