"""v5.46.0 — install_assets discoverable via importlib.resources.

Verifies:
- yadgar/install_assets/ (package-data) contains agents/ + CLAUDE.md.fragment + seeds/
- top-level install_assets/ (shared-data, shipped via wheel.shared-data) has
  CLAUDE.md.fragment + seeds/anchors.yaml + yadgar-backend.service.in + launchd/

The package-data path uses importlib.resources.files("yadgar.install_assets").
The shared-data path uses top-level install_assets/ directory (repo-relative).
"""

from importlib.resources import files
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent


# ── package-data (yadgar.install_assets) ─────────────────────────────────────


def test_package_install_assets_importable():
    """yadgar.install_assets must be importable as a package resource."""
    pkg = files("yadgar.install_assets")
    assert pkg is not None


def test_package_install_assets_has_agents():
    """yadgar/install_assets/agents/ must exist with at least one template."""
    agents_dir = Path(__file__).parent.parent / "install_assets" / "agents"
    assert agents_dir.is_dir(), f"agents/ dir missing: {agents_dir}"
    templates = list(agents_dir.iterdir())
    assert templates, "yadgar/install_assets/agents/ is empty"


def test_package_install_assets_has_claude_fragment():
    """yadgar/install_assets/CLAUDE.md.fragment must exist."""
    fragment = Path(__file__).parent.parent / "install_assets" / "CLAUDE.md.fragment"
    assert fragment.exists(), f"CLAUDE.md.fragment missing: {fragment}"


def test_package_install_assets_has_seeds():
    """yadgar/install_assets/seeds/ must exist."""
    seeds_dir = Path(__file__).parent.parent / "install_assets" / "seeds"
    assert seeds_dir.is_dir(), f"seeds/ dir missing: {seeds_dir}"


# ── shared-data (top-level install_assets/, wheel.shared-data) ───────────────


def test_top_level_install_assets_exists():
    """Top-level install_assets/ must exist (shipped via wheel.shared-data)."""
    top = REPO_ROOT / "install_assets"
    assert top.is_dir(), f"install_assets/ missing at repo root: {top}"


def test_top_level_install_assets_has_claude_fragment():
    """install_assets/CLAUDE.md.fragment must exist."""
    fragment = REPO_ROOT / "install_assets" / "CLAUDE.md.fragment"
    assert fragment.exists(), f"Missing: {fragment}"


def test_top_level_install_assets_has_anchors_yaml():
    """install_assets/seeds/anchors.yaml must exist."""
    anchors = REPO_ROOT / "install_assets" / "seeds" / "anchors.yaml"
    assert anchors.exists(), f"Missing: {anchors}"


def test_top_level_install_assets_has_systemd_templates():
    """install_assets/ (scripts/install/) must have .in systemd unit templates."""
    scripts_dir = REPO_ROOT / "scripts" / "install"
    templates = list(scripts_dir.glob("*.in"))
    assert templates, f"No .in templates found in {scripts_dir}"
    names = {t.name for t in templates}
    assert "yadgar.service.in" in names, f"yadgar.service.in missing; found: {names}"
    assert "yadgar-backend.service.in" in names, "yadgar-backend.service.in missing"
    assert "yadgar.target.in" in names, "yadgar.target.in missing"


def test_top_level_install_assets_has_launchd_templates():
    """scripts/install/launchd/ must have plist.in templates (v5.45.1)."""
    launchd_dir = REPO_ROOT / "scripts" / "install" / "launchd"
    assert launchd_dir.is_dir(), f"launchd/ missing: {launchd_dir}"
    plists = list(launchd_dir.glob("*.plist.in"))
    assert plists, f"No .plist.in files in {launchd_dir}"


def test_yadgar_setup_sh_present_for_wheel():
    """scripts/install/yadgar-setup.sh must exist to be shipped in the wheel."""
    setup_sh = REPO_ROOT / "scripts" / "install" / "yadgar-setup.sh"
    assert setup_sh.exists(), f"Missing (needed for wheel.shared-data): {setup_sh}"
