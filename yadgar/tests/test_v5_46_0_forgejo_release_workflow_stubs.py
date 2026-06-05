"""v5.46.0 — .forgejo/workflows/release.yaml structure checks.

Validates release.yaml:
- Exists and is valid YAML
- Has build-wheel (active) and build-sbom (active) jobs
- Has open-brew-pr and open-nix-pr jobs with if: false gate (v5.46.1 stub contract)
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
def test_release_yaml_open_brew_pr_is_stub():
    """release.yaml must have open-brew-pr job with if: false (v5.46.1 stub)."""
    parsed = yaml.safe_load(RELEASE_YAML.read_text())
    jobs = parsed.get("jobs", {})
    assert "open-brew-pr" in jobs, f"Missing 'open-brew-pr' job; jobs: {list(jobs)}"
    job = jobs["open-brew-pr"]
    if_condition = job.get("if", "")
    assert str(if_condition).lower() == "false", (
        f"open-brew-pr must have 'if: false' (v5.46.1 stub contract); got: {if_condition!r}"
    )


@pytest.mark.skipif(not HAS_YAML, reason="PyYAML not installed")
def test_release_yaml_open_nix_pr_is_stub():
    """release.yaml must have open-nix-pr job with if: false (v5.46.1 stub)."""
    parsed = yaml.safe_load(RELEASE_YAML.read_text())
    jobs = parsed.get("jobs", {})
    assert "open-nix-pr" in jobs, f"Missing 'open-nix-pr' job; jobs: {list(jobs)}"
    job = jobs["open-nix-pr"]
    if_condition = job.get("if", "")
    assert str(if_condition).lower() == "false", (
        f"open-nix-pr must have 'if: false' (v5.46.1 stub contract); got: {if_condition!r}"
    )


@pytest.mark.skipif(not HAS_YAML, reason="PyYAML not installed")
def test_release_yaml_stubs_have_v5461_comment():
    """Stub jobs must contain '# v5.46.1 fills this stub' comment."""
    content = RELEASE_YAML.read_text()
    assert "v5.46.1" in content, "release.yaml stub jobs must mention v5.46.1 (coordination marker)"
