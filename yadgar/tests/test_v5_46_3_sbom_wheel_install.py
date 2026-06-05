"""v5.46.3 TDD — release.yaml build-sbom job installs from local wheel (SBOM fix).

Verifies the build-sbom job in release.yaml uses local wheel pattern:
  pip install "dist/yadgar-${version}-py3-none-any.whl[sbom]"
instead of PyPI roundtrip:
  pip install "yadgar[sbom]==<version>"
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RELEASE_YAML = REPO_ROOT / ".forgejo" / "workflows" / "release.yaml"


class TestSBOMWheelInstall:
    """build-sbom job must install from local wheel, not PyPI."""

    def test_release_yaml_exists(self):
        assert RELEASE_YAML.exists(), f"release.yaml not found at {RELEASE_YAML}"

    def test_sbom_installs_from_local_wheel(self):
        content = RELEASE_YAML.read_text()
        # Must have local wheel pattern
        assert "dist/yadgar-" in content and ".whl[sbom]" in content, (
            "release.yaml build-sbom must install from local wheel: "
            "'dist/yadgar-${version}-py3-none-any.whl[sbom]'. "
            "Current content does not match this pattern."
        )

    def test_sbom_does_not_use_pypi_roundtrip(self):
        content = RELEASE_YAML.read_text()
        # Old pattern: pip install "yadgar[sbom]==${{ steps.get-version.outputs.version }}"
        # This should not be present (replaced by local wheel)
        assert "yadgar[sbom]==${{" not in content and "yadgar[sbom]==${{" not in content, (
            "release.yaml build-sbom must not use PyPI roundtrip install "
            "'yadgar[sbom]==${{ steps.get-version.outputs.version }}'"
        )

    def test_sbom_does_not_use_fallback_editable_install(self):
        content = RELEASE_YAML.read_text()
        # Old fallback: || pip install -e ".[sbom]"
        # Should not be present — local wheel covers both cases
        assert '|| pip install -e ".[sbom]"' not in content, (
            "release.yaml build-sbom must not have editable install fallback "
            "'|| pip install -e \".[sbom]\"'"
        )

    def test_sbom_wheel_pattern_correct_format(self):
        """Wheel filename pattern must match: yadgar-<version>-py3-none-any.whl."""
        content = RELEASE_YAML.read_text()
        # Check for the correct wheel name pattern with version variable
        assert "py3-none-any.whl[sbom]" in content, (
            "release.yaml build-sbom wheel pattern must end with 'py3-none-any.whl[sbom]'"
        )
