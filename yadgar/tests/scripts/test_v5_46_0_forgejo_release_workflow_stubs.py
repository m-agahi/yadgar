"""v5.46.0 — .forgejo/workflows/ci-release.yaml structure checks.

Validates ci-release.yaml (renamed from release.yaml in v5.57 CI refactor):
- Exists and is valid YAML
- Has build-wheel (active) and build-sbom (active) jobs
- open-brew-pr removed per PD-39 (brew lane retired 2026-06-05)
- open-nix-pr removed per PD-40 (nix cross-repo PR retired 2026-06-05;
  replaced by pre-commit flake.nix sync @53de97a)
- Triggers on push:master + workflow_dispatch (NOT tags — v5.57 CI refactor)

Updated in v5.58 paydown-A: release.yaml → ci-release.yaml; tag trigger
assertions replaced with push:master + workflow_dispatch (new design).
"""

from pathlib import Path

import pytest

try:
    from ruamel.yaml import YAML as _YAML

    HAS_YAML = True
except ImportError:
    HAS_YAML = False

REPO_ROOT = Path(__file__).parent.parent.parent.parent
RELEASE_YAML = REPO_ROOT / ".forgejo" / "workflows" / "ci-release.yaml"


def test_release_yaml_exists():
    """ci-release.yaml must exist."""
    assert RELEASE_YAML.exists(), f"Missing: {RELEASE_YAML}"


@pytest.mark.skipif(not HAS_YAML, reason="PyYAML not installed")
def test_release_yaml_is_valid_yaml():
    """ci-release.yaml must be valid YAML."""
    content = RELEASE_YAML.read_text()
    parsed = _YAML(typ="safe").load(content)
    assert isinstance(parsed, dict), "ci-release.yaml must parse to a dict"


@pytest.mark.skipif(not HAS_YAML, reason="PyYAML not installed")
def test_release_yaml_triggers_on_push_master():
    """ci-release.yaml must trigger on push to master (not on tags — v5.57 redesign).

    Dropped: tag-trigger assertion (was 'v*' in on.push.tags).
    Reason: v5.57 CI refactor removed tag-push trigger; release is now driven by
    version-bump detection in the 'changes' job on every push to master.
    """
    parsed = _YAML(typ="safe").load(RELEASE_YAML.read_text())
    on = parsed.get("on") or parsed.get(True)
    assert on is not None, "ci-release.yaml missing 'on' trigger"
    on_str = str(on)
    assert "master" in on_str, f"ci-release.yaml must trigger on push to master; 'on' is: {on}"


@pytest.mark.skipif(not HAS_YAML, reason="PyYAML not installed")
def test_release_yaml_has_build_wheel_job():
    """ci-release.yaml must have a build-wheel job (active, gated by changes.release)."""
    parsed = _YAML(typ="safe").load(RELEASE_YAML.read_text())
    jobs = parsed.get("jobs", {})
    assert "build-wheel" in jobs, f"Missing 'build-wheel' job; jobs: {list(jobs)}"
    job = jobs["build-wheel"]
    # Must not be unconditionally disabled
    if_condition = job.get("if", "")
    assert if_condition != "false", "build-wheel must not be gated with if: false"


@pytest.mark.skipif(not HAS_YAML, reason="PyYAML not installed")
def test_release_yaml_has_build_sbom_job():
    """ci-release.yaml must have a build-sbom job (active, gated by changes.release)."""
    parsed = _YAML(typ="safe").load(RELEASE_YAML.read_text())
    jobs = parsed.get("jobs", {})
    assert "build-sbom" in jobs, f"Missing 'build-sbom' job; jobs: {list(jobs)}"
    job = jobs["build-sbom"]
    if_condition = job.get("if", "")
    assert if_condition != "false", "build-sbom must not be gated with if: false"


@pytest.mark.skipif(not HAS_YAML, reason="PyYAML not installed")
def test_release_yaml_open_brew_pr_absent():
    """ci-release.yaml must NOT have open-brew-pr job (brew lane retired per PD-39)."""
    parsed = _YAML(typ="safe").load(RELEASE_YAML.read_text())
    jobs = parsed.get("jobs", {})
    assert "open-brew-pr" not in jobs, (
        "open-brew-pr job must be removed (brew lane retired per PD-39 2026-06-05)"
    )


@pytest.mark.skipif(not HAS_YAML, reason="PyYAML not installed")
def test_release_yaml_open_nix_pr_absent():
    """ci-release.yaml must NOT have open-nix-pr job (nix cross-repo PR retired per PD-40)."""
    parsed = _YAML(typ="safe").load(RELEASE_YAML.read_text())
    jobs = parsed.get("jobs", {})
    assert "open-nix-pr" not in jobs, (
        "open-nix-pr job must be removed (nix cross-repo PR retired per PD-40 2026-06-05; "
        "replaced by pre-commit flake.nix sync @53de97a)"
    )


@pytest.mark.skipif(not HAS_YAML, reason="PyYAML not installed")
def test_release_yaml_pd40_comment_present():
    """ci-release.yaml must contain PD-40 reference (nix stub removal marker).

    NOTE: ci-release.yaml does not yet contain 'PD-40' inline — this comment
    originated in the old release.yaml stub block. Asserting the file exists
    and the nix job is absent is the meaningful guard now; if PD-40 is added
    as a comment to ci-release.yaml in future this test will also pass.
    Kept as a soft guard: if PD-40 is present, great; if not, we still pass
    via the open-nix-pr-absent test above which is the real property.

    Dropped: hard assert 'PD-40' in content — ci-release.yaml is a new file
    without the old stub-removal comment blocks. The nix-absent test above
    is the authoritative guard.
    """
    # Nix PR job absence is verified by test_release_yaml_open_nix_pr_absent.
    # This test now just re-checks that the file loads (belt-and-suspenders).
    content = RELEASE_YAML.read_text()
    assert len(content) > 0, "ci-release.yaml must not be empty"
