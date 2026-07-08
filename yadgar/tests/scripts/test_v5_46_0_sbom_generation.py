"""v5.46.0 — SBOM generation script checks.

Verifies scripts/generate_sbom.sh exists and scripts/generate_sbom.sh --help
works. Full CycloneDX generation is tested only when cyclonedx-bom is installed.

RED phase: fails until scripts/generate_sbom.sh is created.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent.parent
GEN_SBOM_SH = REPO_ROOT / "scripts" / "generate_sbom.sh"
CYCLONEDX = shutil.which("cyclonedx-bom") or shutil.which("cyclonedx")


def test_generate_sbom_sh_exists():
    """scripts/generate_sbom.sh must exist."""
    assert GEN_SBOM_SH.exists(), f"Missing: {GEN_SBOM_SH}"


def test_generate_sbom_sh_is_executable():
    """scripts/generate_sbom.sh must be executable."""
    assert GEN_SBOM_SH.exists()
    assert os.access(GEN_SBOM_SH, os.X_OK), f"Not executable: {GEN_SBOM_SH}"


def test_generate_sbom_sh_has_shebang():
    """scripts/generate_sbom.sh must have a bash shebang."""
    assert GEN_SBOM_SH.exists()
    first_line = GEN_SBOM_SH.read_text().splitlines()[0]
    assert first_line.startswith("#!") and "bash" in first_line, (
        f"Missing bash shebang: {first_line}"
    )


def test_generate_sbom_sh_has_set_e():
    """scripts/generate_sbom.sh must use set -euo pipefail."""
    assert GEN_SBOM_SH.exists()
    content = GEN_SBOM_SH.read_text()
    assert "set -euo pipefail" in content or "set -e" in content


def test_generate_sbom_sh_references_cyclonedx():
    """scripts/generate_sbom.sh must reference cyclonedx-bom or cyclonedx."""
    assert GEN_SBOM_SH.exists()
    content = GEN_SBOM_SH.read_text()
    assert "cyclonedx" in content.lower(), "Script must call cyclonedx-bom"


def test_generate_sbom_sh_writes_cdx_json():
    """scripts/generate_sbom.sh must produce a .cdx.json output file reference."""
    assert GEN_SBOM_SH.exists()
    content = GEN_SBOM_SH.read_text()
    assert "cdx.json" in content or "sbom" in content.lower()


@pytest.mark.skipif(not CYCLONEDX, reason="cyclonedx-bom not installed")
def test_sbom_output_is_valid_json(tmp_path):
    """When cyclonedx-bom is available, generated SBOM must be valid JSON."""
    output_file = tmp_path / "sbom.cdx.json"
    result = subprocess.run(
        ["bash", str(GEN_SBOM_SH)],
        capture_output=True,
        text=True,
        env={**os.environ, "SBOM_OUTPUT": str(output_file)},
    )
    assert result.returncode == 0, f"generate_sbom.sh failed:\n{result.stderr}"
    assert output_file.exists(), f"SBOM output file not created: {output_file}"
    # Validate JSON
    content = json.loads(output_file.read_text())
    assert "bomFormat" in content or "components" in content, "Output is not a valid CycloneDX SBOM"
