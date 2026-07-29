"""v5.46.3 TDD — ci-release.yaml build-sbom job installs from local wheel (SBOM fix).

Verifies the build-sbom job in ci-release.yaml uses local wheel pattern:
  pip install "dist/yadgar-${version}-py3-none-any.whl[sbom]"
instead of PyPI roundtrip:
  pip install "yadgar[sbom]==<version>"

Updated in v5.58 paydown-A: release.yaml → ci-release.yaml.

Updated 2026-07-29 (Car H2 / D4): parametrized over BOTH CI mirrors. Both
`.github/workflows/` and `.forgejo/workflows/` are canonical and must stay in
sync, so a `.forgejo`-only assertion let the GitHub mirror drift unchecked.
General structural parity is guarded by `test_ci_mirror_parity.py`; this file
pins the build-sbom install shape in each mirror.
"""

import pytest

from yadgar.tests._paths import REPO_ROOT

MIRRORS = [
    pytest.param(REPO_ROOT / ".github" / "workflows" / "ci-release.yml", id="github"),
    pytest.param(REPO_ROOT / ".forgejo" / "workflows" / "ci-release.yaml", id="forgejo"),
]


@pytest.mark.parametrize("RELEASE_YAML", MIRRORS)
class TestSBOMWheelInstall:
    """build-sbom job must install from local wheel, not PyPI — in both mirrors."""

    def test_release_yaml_exists(self, RELEASE_YAML):
        assert RELEASE_YAML.exists(), f"ci-release workflow not found at {RELEASE_YAML}"

    def test_sbom_installs_from_local_wheel(self, RELEASE_YAML):
        content = RELEASE_YAML.read_text()
        # Must have local wheel pattern
        assert "dist/yadgar-" in content and ".whl[sbom]" in content, (
            "ci-release.yaml build-sbom must install from local wheel: "
            "'dist/yadgar-${version}-py3-none-any.whl[sbom]'. "
            "Current content does not match this pattern."
        )

    def test_sbom_does_not_use_pypi_roundtrip(self, RELEASE_YAML):
        content = RELEASE_YAML.read_text()
        # Old pattern: pip install "yadgar[sbom]==${{ steps.get-version.outputs.version }}"
        # This should not be present (replaced by local wheel)
        assert "yadgar[sbom]==${{" not in content and "yadgar[sbom]==${{" not in content, (
            "ci-release.yaml build-sbom must not use PyPI roundtrip install "
            "'yadgar[sbom]==${{ steps.get-version.outputs.version }}'"
        )

    def test_sbom_does_not_use_fallback_editable_install(self, RELEASE_YAML):
        content = RELEASE_YAML.read_text()
        # Old fallback: || pip install -e ".[sbom]"
        # Should not be present — local wheel covers both cases
        assert '|| pip install -e ".[sbom]"' not in content, (
            "ci-release.yaml build-sbom must not have editable install fallback "
            "'|| pip install -e \".[sbom]\"'"
        )

    def test_sbom_wheel_pattern_correct_format(self, RELEASE_YAML):
        """Wheel filename pattern must match: yadgar-<version>-py3-none-any.whl."""
        content = RELEASE_YAML.read_text()
        # Check for the correct wheel name pattern with version variable
        assert "py3-none-any.whl[sbom]" in content, (
            "ci-release.yaml build-sbom wheel pattern must end with 'py3-none-any.whl[sbom]'"
        )
