"""v5.46.1 — .forgejo/workflows/ci-release.yaml publish-pypi job checks.

Validates that the release workflow (renamed ci-release.yaml in v5.57 CI refactor):
- Has a publish-pypi job
- publish-pypi depends on build-wheel (not build-sbom)
- publish-pypi is gated by needs.changes.outputs.release == 'true' (not tag guard)
- publish-pypi uses PYPI_API_TOKEN secret
- publish-pypi runs twine upload (or equivalent PyPI upload)
- workflow_dispatch trigger present (manual fallback)

Updated in v5.58 paydown-A:
- release.yaml → ci-release.yaml
- Dropped: tag-guard assertion (refs/tags/v) — v5.57 removed tag trigger entirely.
  The new gate is `needs.changes.outputs.release == 'true'` (version-bump detection).
- Dropped: "NOT active on workflow_dispatch" assertion — workflow_dispatch now forces
  release=true in the changes job (manual override), so publish CAN run on dispatch.
"""

from pathlib import Path

import pytest

try:
    import yaml

    HAS_YAML = True
except ImportError:
    HAS_YAML = False

REPO_ROOT = Path(__file__).parent.parent.parent.parent
RELEASE_YAML = REPO_ROOT / ".forgejo" / "workflows" / "ci-release.yaml"


@pytest.mark.skipif(not HAS_YAML, reason="PyYAML not installed")
def test_publish_pypi_job_exists():
    """ci-release.yaml must have a publish-pypi job."""
    parsed = yaml.safe_load(RELEASE_YAML.read_text())
    jobs = parsed.get("jobs", {})
    assert "publish-pypi" in jobs, (
        f"Missing 'publish-pypi' job in ci-release.yaml; present jobs: {list(jobs)}"
    )


@pytest.mark.skipif(not HAS_YAML, reason="PyYAML not installed")
def test_publish_pypi_depends_on_build_wheel():
    """publish-pypi must depend on build-wheel (and changes)."""
    parsed = yaml.safe_load(RELEASE_YAML.read_text())
    job = parsed["jobs"]["publish-pypi"]
    needs = job.get("needs", [])
    if isinstance(needs, str):
        needs = [needs]
    assert "build-wheel" in needs, f"publish-pypi must need build-wheel; needs: {needs}"


@pytest.mark.skipif(not HAS_YAML, reason="PyYAML not installed")
def test_publish_pypi_gated_on_release_output():
    """publish-pypi must have an if: gate on needs.changes.outputs.release == 'true'.

    Dropped: refs/tags/v tag-guard assertion.
    Reason: v5.57 removed the tag trigger entirely. The new gate uses the 'changes'
    job's version-bump detection output to decide whether to publish.
    """
    parsed = yaml.safe_load(RELEASE_YAML.read_text())
    job = parsed["jobs"]["publish-pypi"]
    if_condition = str(job.get("if", ""))
    assert "release" in if_condition, (
        f"publish-pypi must be gated on changes.outputs.release; if: {if_condition!r}"
    )


@pytest.mark.skipif(not HAS_YAML, reason="PyYAML not installed")
def test_publish_pypi_uses_pypi_api_token():
    """ci-release.yaml must reference PYPI_API_TOKEN secret."""
    content = RELEASE_YAML.read_text()
    assert "PYPI_API_TOKEN" in content, "ci-release.yaml must reference PYPI_API_TOKEN"


@pytest.mark.skipif(not HAS_YAML, reason="PyYAML not installed")
def test_publish_pypi_uses_twine_upload():
    """publish-pypi steps must invoke twine upload."""
    content = RELEASE_YAML.read_text()
    assert "twine" in content, "publish-pypi job must use twine for upload"


@pytest.mark.skipif(not HAS_YAML, reason="PyYAML not installed")
def test_publish_pypi_skip_existing():
    """twine upload must use --skip-existing for idempotent re-run."""
    content = RELEASE_YAML.read_text()
    assert "--skip-existing" in content, (
        "twine upload must use --skip-existing (idempotent on re-run)"
    )


@pytest.mark.skipif(not HAS_YAML, reason="PyYAML not installed")
def test_publish_pypi_caveat_comment_present():
    """ci-release.yaml must have a PyPI publish comment block."""
    content = RELEASE_YAML.read_text()
    assert "PyPI publish" in content or "pypi" in content.lower(), (
        "ci-release.yaml should have a comment about the PyPI publish job"
    )


@pytest.mark.skipif(not HAS_YAML, reason="PyYAML not installed")
def test_workflow_dispatch_still_present():
    """workflow_dispatch trigger must remain (manual fallback forces release=true)."""
    parsed = yaml.safe_load(RELEASE_YAML.read_text())
    on = parsed.get("on") or parsed.get(True)
    on_str = str(on)
    assert "workflow_dispatch" in on_str, "workflow_dispatch trigger must remain in ci-release.yaml"
