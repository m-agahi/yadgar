"""v5.46.0 — pyproject.toml distribution metadata validation.

Verifies:
- License classifier matches Apache-2.0 (not MIT)
- [dist] extra has cyclonedx-bom pin
- [sbom] extra has cyclonedx-bom pin
- Missing OS/Topic/Environment classifiers are now present
- [project.scripts] has yadgar-setup entry

GREEN after Step 0 pyproject edits (most tests pass immediately after license fix).
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"


def _read_pyproject():
    return PYPROJECT.read_text()


def test_license_classifier_is_apache():
    """Classifier must be Apache Software License, not MIT."""
    content = _read_pyproject()
    assert "License :: OSI Approved :: Apache Software License" in content, (
        "Missing Apache license classifier"
    )
    assert "License :: OSI Approved :: MIT License" not in content, (
        "Stale MIT license classifier still present"
    )


def test_license_spdx_is_apache():
    """license = 'Apache-2.0' SPDX field must be present."""
    content = _read_pyproject()
    assert 'license = "Apache-2.0"' in content


def test_classifier_linux():
    """Operating System :: POSIX :: Linux classifier must be present."""
    content = _read_pyproject()
    assert "Operating System :: POSIX :: Linux" in content


def test_classifier_macos():
    """Operating System :: MacOS classifier must be present."""
    content = _read_pyproject()
    assert "Operating System :: MacOS" in content


def test_classifier_console():
    """Environment :: Console classifier must be present."""
    content = _read_pyproject()
    assert "Environment :: Console" in content


def test_classifier_filesystems():
    """Topic :: System :: Filesystems classifier must be present."""
    content = _read_pyproject()
    assert "Topic :: System :: Filesystems" in content


def test_dist_extra_has_cyclonedx_pin():
    """[dist] extra must have cyclonedx-bom pinned to an exact version."""
    content = _read_pyproject()
    # Find dist extra section
    assert "dist = [" in content or "dist=[" in content, "Missing [dist] optional-dependency group"
    # Check cyclonedx-bom with version pin
    assert re.search(r"cyclonedx-bom==\d+\.\d+\.\d+", content), (
        "cyclonedx-bom must be pinned to exact version in [dist] extra"
    )


def test_sbom_extra_has_cyclonedx_pin():
    """[sbom] extra must have cyclonedx-bom pinned."""
    content = _read_pyproject()
    assert "sbom = [" in content or "sbom=[" in content, "Missing [sbom] optional-dependency group"
    assert re.search(r"cyclonedx-bom==\d+\.\d+\.\d+", content), (
        "cyclonedx-bom must be pinned in [sbom] extra"
    )


def test_project_scripts_has_yadgar_setup():
    """[project.scripts] must have yadgar-setup entry."""
    content = _read_pyproject()
    assert "yadgar-setup" in content, "yadgar-setup missing from [project.scripts]"
    assert "yadgar.core.scripts.yadgar_setup:main" in content, (
        "yadgar-setup must point to yadgar.core.scripts.yadgar_setup:main"
    )


def test_wheel_shared_data_ships_setup_sh():
    """pyproject.toml must ship scripts/install/yadgar-setup.sh via wheel.shared-data."""
    content = _read_pyproject()
    assert "yadgar-setup.sh" in content, (
        "scripts/install/yadgar-setup.sh must be in wheel.shared-data"
    )
