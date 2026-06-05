"""v5.46.0 — .forgejo/workflows/release.yaml structure checks.

Validates release.yaml:
- Exists and is valid YAML
- Has build-wheel (active) and build-sbom (active) jobs
- open-brew-pr removed per PD-39 (brew lane retired 2026-06-05)
- open-nix-pr removed per PD-40 (nix cross-repo PR retired 2026-06-05;
  replaced by pre-commit flake.nix sync @53de97a)
- Triggers on tags: ["v*"]

RED phase: fails until .forgejo/workflows/release.yaml is created.
"""

from pathlib import Path

import pytest

try:
    import yaml

    HAS_YAML = True
except ImportError:
    HAS_YAML = False

REPO_ROOT = Path(__file__).parent.parent.parent
RELEASE_YAML = REPO_ROOT / ".forgejo" / "workflows" / "release.yaml"


def test_release_yaml_exists():
    """release.yaml must exist."""
    assert RELEASE_YAML.exists(), f"Missing: {RELEASE_YAML}"


@pytest.mark.skipif(not HAS_YAML, reason="PyYAML not installed")
def test_release_yaml_is_valid_yaml():
    """release.yaml must be valid YAML."""
    content = RELEASE_YAML.read_text()
    parsed = yaml.safe_load(content)
    assert isinstance(parsed, dict), "release.yaml must parse to a dict"


@pytest.mark.skipif(not HAS_YAML, reason="PyYAML not installed")
def test_release_yaml_triggers_on_version_tags():
    """release.yaml must trigger on v* tags."""
    parsed = yaml.safe_load(RELEASE_YAML.read_text())
    on = parsed.get("on") or parsed.get(True)  # YAML `on` is a bool alias
    assert on is not None, "release.yaml missing 'on' trigger"
    # Can be string or dict
    on_str = str(on)
    assert "v*" in on_str or "tags" in on_str, (
        f"release.yaml must trigger on v* tags; 'on' is: {on}"
    )


@pytest.mark.skipif(not HAS_YAML, reason="PyYAML not installed")
def test_release_yaml_has_build_wheel_job():
    """release.yaml must have a build-wheel job (active, no if:false)."""
    parsed = yaml.safe_load(RELEASE_YAML.read_text())
    jobs = parsed.get("jobs", {})
    assert "build-wheel" in jobs, f"Missing 'build-wheel' job; jobs: {list(jobs)}"
    job = jobs["build-wheel"]
    # Must not be if: false
    if_condition = job.get("if", "")
    assert if_condition != "false", "build-wheel must not be gated with if: false"


@pytest.mark.skipif(not HAS_YAML, reason="PyYAML not installed")
def test_release_yaml_has_build_sbom_job():
    """release.yaml must have a build-sbom job (active, no if:false)."""
    parsed = yaml.safe_load(RELEASE_YAML.read_text())
    jobs = parsed.get("jobs", {})
    assert "build-sbom" in jobs, f"Missing 'build-sbom' job; jobs: {list(jobs)}"
    job = jobs["build-sbom"]
    if_condition = job.get("if", "")
    assert if_condition != "false", "build-sbom must not be gated with if: false"


@pytest.mark.skipif(not HAS_YAML, reason="PyYAML not installed")
def test_release_yaml_open_brew_pr_absent():
    """release.yaml must NOT have open-brew-pr job (brew lane retired per PD-39)."""
    parsed = yaml.safe_load(RELEASE_YAML.read_text())
    jobs = parsed.get("jobs", {})
    assert "open-brew-pr" not in jobs, (
        "open-brew-pr job must be removed (brew lane retired per PD-39 2026-06-05)"
    )


@pytest.mark.skipif(not HAS_YAML, reason="PyYAML not installed")
def test_release_yaml_open_nix_pr_absent():
    """release.yaml must NOT have open-nix-pr job (nix cross-repo PR retired per PD-40 2026-06-05)."""
    parsed = yaml.safe_load(RELEASE_YAML.read_text())
    jobs = parsed.get("jobs", {})
    assert "open-nix-pr" not in jobs, (
        "open-nix-pr job must be removed (nix cross-repo PR retired per PD-40 2026-06-05; "
        "replaced by pre-commit flake.nix sync @53de97a)"
    )


@pytest.mark.skipif(not HAS_YAML, reason="PyYAML not installed")
def test_release_yaml_pd40_comment_present():
    """release.yaml must contain PD-40 reference (nix stub removal marker)."""
    content = RELEASE_YAML.read_text()
    assert "PD-40" in content, (
        "release.yaml must reference PD-40 (nix stub removal decision marker)"
    )
