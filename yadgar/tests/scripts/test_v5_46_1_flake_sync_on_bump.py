"""v5.46.1 — Regression test: scripts/sync_version.py auto-updates flake.nix.

Validates that when pyproject.toml version is bumped and sync_version.py runs:
- flake.nix line with `version = "..."` is updated to match
- server.json version is also updated
- Script exits 1 (signals pre-commit to re-stage) when it made changes

This is a regression test for existing @53de97a behavior — it was GREEN when
written. Kept here so future refactors don't regress flake.nix sync.
"""

import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent.parent
SYNC_SCRIPT = REPO_ROOT / "scripts" / "sync_version.py"


def _make_fixtures(tmp_path: Path, version: str) -> dict[str, Path]:
    """Create minimal fixture files mimicking repo structure."""
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()

    # Copy the actual sync_version.py script
    script_dest = scripts_dir / "sync_version.py"
    shutil.copy(SYNC_SCRIPT, script_dest)

    # pyproject.toml with bumped version
    (tmp_path / "pyproject.toml").write_text(
        textwrap.dedent(f"""\
            [project]
            name = "yadgar"
            version = "{version}"
        """)
    )

    # server.json with old version
    import json

    (tmp_path / "server.json").write_text(
        json.dumps(
            {
                "version": "5.46.0",
                "packages": [{"version": "5.46.0"}],
            },
            indent=2,
        )
        + "\n"
    )

    # flake.nix with old version (matches real file structure: version = "5.46.0";)
    # Includes coreVersion mkOption block required by sync_version.py step 2.
    (tmp_path / "flake.nix").write_text(
        textwrap.dedent("""\
            {
              description = "test";
              outputs = { self, nixpkgs, lib }: {
                packages.x86_64-linux.default = {
                  pname = "yadgar";
                  version = "5.46.0";
                };
                options.programs.yadgar = {
                  coreVersion = lib.mkOption {
                    type = lib.types.str;
                    default = "5.46.0";
                    description = "Container image tag for the yadgar core service.";
                  };
                };
              };
            }
        """)
    )

    return {
        "pyproject": tmp_path / "pyproject.toml",
        "server_json": tmp_path / "server.json",
        "flake_nix": tmp_path / "flake.nix",
        "script": script_dest,
    }


def test_sync_version_updates_flake_nix(tmp_path):
    """sync_version.py must update flake.nix version when pyproject.toml bumps."""
    fixtures = _make_fixtures(tmp_path, "5.46.1")

    r = subprocess.run(
        [sys.executable, str(fixtures["script"])],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )

    # Exit 1 is expected when files were changed (pre-commit re-stage signal)
    assert r.returncode in (0, 1), f"sync_version.py error: {r.stderr}"

    flake_content = fixtures["flake_nix"].read_text()
    assert 'version = "5.46.1"' in flake_content, (
        f"flake.nix version not updated to 5.46.1: {flake_content}"
    )
    assert 'version = "5.46.0"' not in flake_content, (
        f"flake.nix still shows old version 5.46.0: {flake_content}"
    )


def test_sync_version_updates_server_json(tmp_path):
    """sync_version.py must update server.json when pyproject.toml bumps."""
    import json

    fixtures = _make_fixtures(tmp_path, "5.46.1")

    subprocess.run(
        [sys.executable, str(fixtures["script"])],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )

    data = json.loads(fixtures["server_json"].read_text())
    assert data["version"] == "5.46.1", f"server.json version not updated: {data}"


def test_sync_version_exits_1_when_changes_made(tmp_path):
    """sync_version.py must exit 1 when it writes changes (pre-commit re-stage)."""
    fixtures = _make_fixtures(tmp_path, "5.46.1")

    r = subprocess.run(
        [sys.executable, str(fixtures["script"])],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )

    assert r.returncode == 1, (
        f"sync_version.py must exit 1 when files changed (triggers pre-commit re-stage); "
        f"got exit {r.returncode}"
    )


def test_sync_version_exits_0_when_no_changes(tmp_path):
    """sync_version.py must exit 0 when all files already match."""
    import json

    version = "5.46.1"
    fixtures = _make_fixtures(tmp_path, version)

    # Pre-align server.json and flake.nix to match
    data = json.loads(fixtures["server_json"].read_text())
    data["version"] = version
    data["packages"][0]["version"] = version
    fixtures["server_json"].write_text(json.dumps(data, indent=2) + "\n")

    flake = fixtures["flake_nix"].read_text()
    flake = flake.replace("5.46.0", version)
    fixtures["flake_nix"].write_text(flake)

    r = subprocess.run(
        [sys.executable, str(fixtures["script"])],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )

    assert r.returncode == 0, (
        f"sync_version.py must exit 0 when no changes needed; got {r.returncode}: {r.stderr}"
    )


def test_sync_version_leaves_cbm_version_pin_untouched(tmp_path):
    """The `_cbm_version` pin (codebase-memory-mcp, #83 Car A) must NOT be synced.

    Regression for the v5.163.0 bug: the un-anchored ``(version\\s*=\\s*")`` regex
    matched the suffix of ``_cbm_version = "0.9.0";`` FIRST under ``count=1`` — it
    corrupted the CBM release tag AND left the yadgar-pkg ``version`` unsynced. The
    line-anchored regex must skip ``_cbm_version`` and hit the bare ``version`` field.
    """
    import json

    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    shutil.copy(SYNC_SCRIPT, scripts_dir / "sync_version.py")

    (tmp_path / "pyproject.toml").write_text(
        textwrap.dedent("""\
            [project]
            name = "yadgar"
            version = "5.163.0"
        """)
    )
    (tmp_path / "server.json").write_text(
        json.dumps({"version": "5.162.0", "packages": [{"version": "5.162.0"}]}, indent=2) + "\n"
    )
    # flake.nix with a _cbm_version pin BEFORE the yadgar-pkg version (mirrors the
    # real file: the pin's suffix is `version = "` which the old regex matched first).
    (tmp_path / "flake.nix").write_text(
        textwrap.dedent("""\
            {
              outputs = { self, nixpkgs, lib }: {
                _cbm = {
                  _cbm_version = "0.9.0";
                };
                packages.x86_64-linux.default = {
                  pname = "yadgar";
                  version = "5.162.0";
                };
                options.programs.yadgar = {
                  coreVersion = lib.mkOption {
                    type = lib.types.str;
                    default = "5.162.0";
                    description = "Container image tag for the yadgar core service.";
                  };
                };
              };
            }
        """)
    )

    r = subprocess.run(
        [sys.executable, str(scripts_dir / "sync_version.py")],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )
    assert r.returncode in (0, 1), f"sync_version.py error: {r.stderr}"

    flake = (tmp_path / "flake.nix").read_text()
    # The CBM binary pin survives (it tracks the binary release, not yadgar).
    assert '_cbm_version = "0.9.0";' in flake, f"_cbm_version pin corrupted: {flake}"
    # The yadgar-pkg version IS synced.
    assert 'version = "5.163.0";' in flake, f"yadgar version not synced: {flake}"
    assert 'version = "5.162.0";' not in flake, f"stale yadgar version remains: {flake}"


def test_real_repo_flake_nix_has_version_field():
    """Sanity: real repo flake.nix has a version = "..."; line (sync target exists)."""
    flake_path = REPO_ROOT / "flake.nix"
    assert flake_path.exists(), "flake.nix must exist"
    content = flake_path.read_text()
    assert 'version = "' in content, "flake.nix must have version field"
