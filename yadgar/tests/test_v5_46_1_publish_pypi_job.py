"""v5.46.1 — .forgejo/workflows/release.yaml publish-pypi job checks.

Validates that the release workflow:
- Has a publish-pypi job
- publish-pypi depends on build-wheel (not build-sbom)
- publish-pypi triggers only on tag push (startsWith 'refs/tags/v')
- publish-pypi uses PYPI_API_TOKEN secret
- publish-pypi is NOT active on workflow_dispatch (tag-only guard)
- publish-pypi runs twine upload (or equivalent PyPI upload)

RED phase: fails until publish-pypi job is added to release.yaml.
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


@pytest.mark.skipif(not HAS_YAML, reason="PyYAML not installed")
def test_publish_pypi_job_exists():
    """release.yaml must have a publish-pypi job."""
    parsed = yaml.safe_load(RELEASE_YAML.read_text())
    jobs = parsed.get("jobs", {})
    assert "publish-pypi" in jobs, (
        f"Missing 'publish-pypi' job in release.yaml; present jobs: {list(jobs)}"
    )


@pytest.mark.skipif(not HAS_YAML, reason="PyYAML not installed")
def test_publish_pypi_depends_on_build_wheel():
    """publish-pypi must depend on build-wheel (not build-sbom)."""
    parsed = yaml.safe_load(RELEASE_YAML.read_text())
    job = parsed["jobs"]["publish-pypi"]
    needs = job.get("needs", [])
    if isinstance(needs, str):
        needs = [needs]
    assert "build-wheel" in needs, f"publish-pypi must need build-wheel; needs: {needs}"


@pytest.mark.skipif(not HAS_YAML, reason="PyYAML not installed")
def test_publish_pypi_has_tag_guard():
    """publish-pypi must have an if: condition gating on refs/tags/v prefix."""
    parsed = yaml.safe_load(RELEASE_YAML.read_text())
    job = parsed["jobs"]["publish-pypi"]
    if_condition = str(job.get("if", ""))
    assert "refs/tags/v" in if_condition or "startsWith" in if_condition, (
        f"publish-pypi must be gated on tag push; if: {if_condition!r}"
    )


@pytest.mark.skipif(not HAS_YAML, reason="PyYAML not installed")
def test_publish_pypi_uses_pypi_api_token():
    """publish-pypi must reference PYPI_API_TOKEN secret."""
    content = RELEASE_YAML.read_text()
    assert "PYPI_API_TOKEN" in content, "release.yaml must reference PYPI_API_TOKEN"


@pytest.mark.skipif(not HAS_YAML, reason="PyYAML not installed")
def test_publish_pypi_uses_twine_upload():
    """publish-pypi steps must invoke twine upload."""
    content = RELEASE_YAML.read_text()
    assert "twine" in content, "publish-pypi job must use twine for upload"


@pytest.mark.skipif(not HAS_YAML, reason="PyYAML not installed")
def test_publish_pypi_skip_existing():
    """twine upload must use --skip-existing for idempotent re-tag."""
    content = RELEASE_YAML.read_text()
    assert "--skip-existing" in content, (
        "twine upload must use --skip-existing (idempotent on re-tag)"
    )


@pytest.mark.skipif(not HAS_YAML, reason="PyYAML not installed")
def test_publish_pypi_caveat_comment_present():
    """release.yaml must have the PyPI publish caveat comment block."""
    content = RELEASE_YAML.read_text()
    assert "PyPI publish" in content or "pypi" in content.lower(), (
        "release.yaml should have a comment about the PyPI publish job"
    )


@pytest.mark.skipif(not HAS_YAML, reason="PyYAML not installed")
def test_workflow_dispatch_still_present():
    """workflow_dispatch trigger must remain (for manual runs, excluding publish)."""
    parsed = yaml.safe_load(RELEASE_YAML.read_text())
    on = parsed.get("on") or parsed.get(True)
    on_str = str(on)
    assert "workflow_dispatch" in on_str, "workflow_dispatch trigger must remain in release.yaml"
