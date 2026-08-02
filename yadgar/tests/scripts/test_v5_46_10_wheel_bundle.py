"""v5.46.10 — wheel bundle inventory.

Builds the distribution wheel and asserts every required helper script and
install-asset template is present inside the archive.

Bug: since v5.45.0 the wheel shipped only scripts/install/yadgar-setup.sh.
All helper scripts it calls (detect_runtime.sh, generate_systemd.sh, etc.)
were absent, breaking pipx installs on fresh hosts.

Fix: pyproject.toml shared-data now maps the entire scripts/install/ directory
to share/yadgar/scripts/, shipping all helpers + .in templates recursively.

This test builds the wheel fresh and walks the zip to assert completeness.

Wheel ZIP layout note: shared-data files appear as
  yadgar-X.Y.Z.data/data/share/yadgar/scripts/<file>
The EXPECTED_* lists below use the bare share/yadgar/... suffix; the helper
`_wheel_names_normalised()` strips the `.data/data/` prefix for matching.
"""

import subprocess
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent.parent

# All files that must be present in share/yadgar/scripts/ inside the wheel.
REQUIRED_SCRIPTS = [
    "share/yadgar/scripts/yadgar-setup.sh",
    "share/yadgar/scripts/detect_runtime.sh",
    "share/yadgar/scripts/detect_os.sh",
    "share/yadgar/scripts/install_runtime.sh",
    "share/yadgar/scripts/generate_systemd.sh",
    "share/yadgar/scripts/generate_launchd.sh",
    "share/yadgar/scripts/bootstrap_secrets.sh",
    "share/yadgar/scripts/restore.sh",
    "share/yadgar/scripts/uninstall.sh",
    "share/yadgar/scripts/append_claude_rules.sh",
    # NO systemd .in templates: task:0110 Stage D (ADR-0190) deleted all nine.
    # generate_systemd.sh renders nothing — it resolves the co-shipped `yadgar`
    # CLI and delegates to `yadgar daemon render-units`, so the systemd unit
    # definitions ride in the PACKAGE (yadgar/core/daemon/units.py et al) and
    # need no shared-data entry. The launchd surface still uses templates.
    # launchd .in templates (generate_launchd.sh uses ${SCRIPT_DIR}/launchd/...)
    "share/yadgar/scripts/launchd/com.openfantasy.yadgar.plist.in",
    "share/yadgar/scripts/launchd/com.openfantasy.yadgar-backend.plist.in",
]

# install_assets/ entries already shipped since v5.45.0 — verify not regressed.
REQUIRED_INSTALL_ASSETS = [
    "share/yadgar/install_assets/CLAUDE.md.fragment",
    "share/yadgar/install_assets/seeds/anchors.yaml",
]

# Package-data files under the yadgar/ tree that MUST ship inside the wheel
# (hatchling packages the whole tree; these are load-bearing at runtime —
# importlib.resources reads them, so a missing file = broken install).
REQUIRED_PACKAGE_DATA = [
    # v5.123.0 Car 3 (#34): stop-hook checkpoint prompt template
    "yadgar/core/hooks/templates/stop_checkpoint_prompt.md",
    # v5.88/v5.122.0: seed genesis corpus (agent prompts + contract + disciplines)
    "yadgar/core/seed/materials/agent_prompts.yaml",
]


def _find_wheel() -> Path:
    """Return path to the most recent yadgar wheel in dist/."""
    dist = REPO_ROOT / "dist"
    wheels = sorted(dist.glob("yadgar-*.whl"))
    if not wheels:
        pytest.fail(
            f"No .whl found in {dist}. Run 'uv build --wheel' first or use the build fixture."
        )
    return wheels[-1]


def _wheel_names_normalised(whl: Path) -> set[str]:
    """Return set of zip member names, stripping the yadgar-X.Y.Z.data/data/ prefix.

    Wheel shared-data entries are stored as:
        yadgar-5.46.10-py3-none-any.data/data/share/yadgar/scripts/foo.sh
    We strip the leading `<dist_info_base>.data/data/` segment so callers can
    match against bare `share/yadgar/...` paths.
    """
    with zipfile.ZipFile(whl) as zf:
        raw = zf.namelist()

    normalised: set[str] = set()
    for name in raw:
        # Strip yadgar-X.Y.Z.data/data/ prefix when present
        parts = name.split("/", 2)
        if len(parts) == 3 and parts[0].endswith(".data") and parts[1] == "data":
            normalised.add(parts[2])
        else:
            normalised.add(name)
    return normalised


@pytest.fixture(scope="module")
def built_wheel() -> Path:
    """Build the wheel with uv and return path to the .whl file."""
    dist_dir = REPO_ROOT / "dist"
    result = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(dist_dir)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(f"uv build --wheel failed:\n{result.stderr}")
    wheels = sorted(dist_dir.glob("yadgar-*.whl"))
    if not wheels:
        pytest.fail(f"No .whl found in {dist_dir} after build")
    return wheels[-1]


@pytest.mark.parametrize("expected_path", REQUIRED_SCRIPTS)
def test_wheel_contains_required_script(built_wheel: Path, expected_path: str) -> None:
    """Every helper script and .in template must be present in the wheel archive."""
    names = _wheel_names_normalised(built_wheel)
    assert expected_path in names, (
        f"Missing from wheel: {expected_path}\n"
        f"  Wheel: {built_wheel.name}\n"
        f"  Scripts present: {sorted(n for n in names if 'scripts' in n)}\n"
        f"  Fix: verify pyproject.toml [tool.hatch.build.targets.wheel.shared-data] "
        f"maps 'scripts/install' -> 'share/yadgar/scripts'"
    )


@pytest.mark.parametrize("expected_path", REQUIRED_INSTALL_ASSETS)
def test_wheel_contains_install_asset(built_wheel: Path, expected_path: str) -> None:
    """install_assets/ entries must not have regressed."""
    names = _wheel_names_normalised(built_wheel)
    assert expected_path in names, (
        f"Regression — install_asset missing from wheel: {expected_path}\n"
        f"  Wheel: {built_wheel.name}"
    )


@pytest.mark.parametrize("expected_path", REQUIRED_PACKAGE_DATA)
def test_wheel_contains_package_data(built_wheel: Path, expected_path: str) -> None:
    """Runtime-load-bearing package data must ship inside the wheel."""
    names = _wheel_names_normalised(built_wheel)
    assert expected_path in names, (
        f"Package data missing from wheel: {expected_path}\n"
        f"  Wheel: {built_wheel.name}\n"
        f"  Fix: hatchling ships all files under yadgar/ by default — check the "
        f"file exists in the source tree and is not excluded by build config"
    )


def test_wheel_scripts_count(built_wheel: Path) -> None:
    """Wheel must contain at least 10 files under share/yadgar/scripts/."""
    names = _wheel_names_normalised(built_wheel)
    script_entries = [n for n in names if n.startswith("share/yadgar/scripts/")]
    assert len(script_entries) >= 10, (
        f"Expected >=10 entries under share/yadgar/scripts/, got {len(script_entries)}:\n"
        + "\n".join(f"  {e}" for e in sorted(script_entries))
    )
