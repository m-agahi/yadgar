"""v5.46.0 — install_assets discoverable via importlib.resources.

Verifies:
- yadgar/install_assets/ (package-data) contains agents/ + CLAUDE.md.fragment
- yadgar/seed/materials/ (package-data, v5.88) holds the canonical seed CONTENT:
  anchors.yaml + agent_prompts.yaml
- top-level install_assets/ (shared-data, shipped via wheel.shared-data) has
  CLAUDE.md.fragment; anchors.yaml ships at share/.../seeds/ via per-file mapping

The package-data path uses importlib.resources.files("yadgar.core.install_assets").
The shared-data path uses top-level install_assets/ directory (repo-relative).
"""

from importlib.resources import files
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent.parent


# ── package-data (yadgar.install_assets) ─────────────────────────────────────


def test_package_install_assets_importable():
    """yadgar.install_assets must be importable as a package resource."""
    pkg = files("yadgar.core.install_assets")
    assert pkg is not None


def test_package_install_assets_has_agents():
    """yadgar/install_assets/agents/ must exist with at least one template."""
    agents_dir = Path(__file__).parent.parent.parent / "core" / "install_assets" / "agents"
    assert agents_dir.is_dir(), f"agents/ dir missing: {agents_dir}"
    templates = list(agents_dir.iterdir())
    assert templates, "yadgar/install_assets/agents/ is empty"


def test_package_install_assets_has_rules_template():
    """yadgar/install_assets/rules/AGENTS.md.template must exist (Car 2, D2).

    The canonical rules template is the single source for all client rules files.
    """
    rules_dir = Path(__file__).parent.parent.parent / "core" / "install_assets" / "rules"
    assert rules_dir.is_dir(), f"rules/ dir missing: {rules_dir}"
    template = rules_dir / "AGENTS.md.template"
    assert template.exists(), f"AGENTS.md.template missing: {template}"


def test_package_install_assets_has_rules_addenda():
    """yadgar/install_assets/rules/addenda/ must exist with CC addendum files."""
    addenda_dir = (
        Path(__file__).parent.parent.parent / "core" / "install_assets" / "rules" / "addenda"
    )
    assert addenda_dir.is_dir(), f"addenda/ dir missing: {addenda_dir}"
    for key in ("compaction_shield", "auto_capture"):
        path = addenda_dir / f"{key}.md"
        assert path.exists(), f"Addendum file missing: {path}"


def test_package_install_assets_has_claude_fragment():
    """CLAUDE.md.fragment exists in yadgar/install_assets/ OR top-level install_assets/.

    v5.45.0 ships CLAUDE.md.fragment in the top-level install_assets/ (shared-data);
    the package-level yadgar/install_assets/ holds agent templates.
    Either location is acceptable for the asset to be wheel-discoverable.
    """
    # Check top-level first (v5.45.0 location via wheel.shared-data)
    fragment_top = REPO_ROOT / "install_assets" / "CLAUDE.md.fragment"
    # Check package-level (yadgar/install_assets/)
    fragment_pkg = (
        Path(__file__).parent.parent.parent.parent / "install_assets" / "CLAUDE.md.fragment"
    )
    assert fragment_top.exists() or fragment_pkg.exists(), (
        f"CLAUDE.md.fragment missing from both:\n  {fragment_top}\n  {fragment_pkg}"
    )


def test_package_install_assets_has_seeds():
    """anchors.yaml seed content is discoverable as package data.

    v5.88 seed consolidation: anchors.yaml moved from install_assets/seeds/ to the
    canonical seed materials dir yadgar/seed/materials/ (so all seed CONTENT is
    edited in one place). It ships as package data under the yadgar/ tree.
    """
    anchors_materials = (
        Path(__file__).parent.parent.parent / "core" / "seed" / "materials" / "anchors.yaml"
    )
    assert anchors_materials.is_file(), (
        f"anchors.yaml missing from canonical materials dir: {anchors_materials}"
    )


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
    """anchors.yaml must exist in the canonical seed materials dir (v5.88).

    Moved from install_assets/seeds/ to yadgar/seed/materials/ so all seed CONTENT
    is edited in one place; it still ships at the historical share/ wheel
    destination via shared-data per-file mapping (see test_v5_46_10_wheel_bundle).
    """
    anchors = REPO_ROOT / "yadgar" / "core" / "seed" / "materials" / "anchors.yaml"
    assert anchors.exists(), f"Missing: {anchors}"


# DELETED task:0110 Stage D — test_top_level_install_assets_has_systemd_templates.
# It required scripts/install/*.in to exist so the wheel could ship them.
# ADR-0190 deletes all nine and makes generate_systemd.sh delegate to
# `yadgar daemon render-units`, so the systemd unit definitions ride in the
# package rather than in shared-data. Not retargeted here — the inverse property
# ("no template grew back") is asserted once, in
# test_v5_45_generate_systemd.py::test_v5_45_no_systemd_templates_remain, and a
# second copy of it would be the duplication this car exists to remove. The
# launchd templates below still ship and are still checked.


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
