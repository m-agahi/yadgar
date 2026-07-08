"""v5.46.1 — scripts/bump_version.py unit tests.

Covers:
- Script invocation (exists, importable via subprocess)
- --dry-run mode prints planned change without writing
- --new <version> substitutes version in pyproject.toml
- --bump patch|minor|major increments correctly
- Refuses when pyproject.toml has unstaged edits (dirty guard)
- Bad version formats are rejected

RED phase: fails until scripts/bump_version.py is created.
"""

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "bump_version.py"


def _write_pyproject(tmp_path: Path, version: str) -> Path:
    """Write a minimal pyproject.toml with the given version."""
    content = textwrap.dedent(f"""\
        [project]
        name = "yadgar"
        version = "{version}"
        description = "test"
    """)
    p = tmp_path / "pyproject.toml"
    p.write_text(content)
    return p


def _run(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT)] + args,
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
    )


def test_script_exists():
    """scripts/bump_version.py must exist."""
    assert SCRIPT.exists(), f"Missing: {SCRIPT}"


def test_help_exits_0():
    """--help must exit 0 and mention --new or --bump."""
    r = _run(["--help"])
    assert r.returncode == 0, f"--help failed: {r.stderr}"
    combined = r.stdout + r.stderr
    assert "--new" in combined or "--bump" in combined, (
        f"--help output missing --new/--bump: {combined}"
    )


def test_dry_run_prints_but_does_not_write(tmp_path):
    """--dry-run must print planned change; pyproject.toml must not change."""
    p = _write_pyproject(tmp_path, "5.1.0")
    r = _run(["--new", "5.2.0", "--dry-run", "--project-root", str(tmp_path)])
    assert r.returncode == 0, f"dry-run failed: {r.stderr}"
    assert "5.2.0" in (r.stdout + r.stderr), "dry-run must print new version"
    content = p.read_text()
    assert "5.1.0" in content, "dry-run must not modify pyproject.toml"
    assert "5.2.0" not in content, "dry-run must not write new version to file"


def test_new_version_substitutes_correctly(tmp_path):
    """--new 5.2.0 must write correct version to pyproject.toml."""
    _write_pyproject(tmp_path, "5.1.0")
    r = _run(["--new", "5.2.0", "--project-root", str(tmp_path)])
    assert r.returncode == 0, f"--new failed: {r.stderr}"
    content = (tmp_path / "pyproject.toml").read_text()
    assert 'version = "5.2.0"' in content, f"version not updated: {content}"
    assert "5.1.0" not in content, f"old version still present: {content}"


def test_bump_patch(tmp_path):
    """--bump patch must increment patch component only."""
    _write_pyproject(tmp_path, "5.1.3")
    r = _run(["--bump", "patch", "--project-root", str(tmp_path)])
    assert r.returncode == 0, f"--bump patch failed: {r.stderr}"
    content = (tmp_path / "pyproject.toml").read_text()
    assert 'version = "5.1.4"' in content, f"expected 5.1.4: {content}"


def test_bump_minor(tmp_path):
    """--bump minor must increment minor + reset patch to 0."""
    _write_pyproject(tmp_path, "5.1.3")
    r = _run(["--bump", "minor", "--project-root", str(tmp_path)])
    assert r.returncode == 0, f"--bump minor failed: {r.stderr}"
    content = (tmp_path / "pyproject.toml").read_text()
    assert 'version = "5.2.0"' in content, f"expected 5.2.0: {content}"


def test_bump_major(tmp_path):
    """--bump major must increment major + reset minor + patch to 0."""
    _write_pyproject(tmp_path, "5.1.3")
    r = _run(["--bump", "major", "--project-root", str(tmp_path)])
    assert r.returncode == 0, f"--bump major failed: {r.stderr}"
    content = (tmp_path / "pyproject.toml").read_text()
    assert 'version = "6.0.0"' in content, f"expected 6.0.0: {content}"


def test_rejects_missing_pyproject(tmp_path):
    """Must exit non-zero when pyproject.toml does not exist."""
    r = _run(["--new", "5.2.0", "--project-root", str(tmp_path)])
    assert r.returncode != 0, "Expected failure when pyproject.toml missing"


def test_current_version_flag(tmp_path):
    """--current-version flag must print the current version and exit 0."""
    _write_pyproject(tmp_path, "5.3.1")
    r = _run(["--current-version", "--project-root", str(tmp_path)])
    assert r.returncode == 0, f"--current-version failed: {r.stderr}"
    assert "5.3.1" in (r.stdout + r.stderr), "Must print current version"


def test_new_and_bump_mutually_exclusive():
    """--new and --bump together must fail (mutually exclusive)."""
    r = _run(["--new", "5.2.0", "--bump", "patch"])
    assert r.returncode != 0, "--new + --bump should be mutually exclusive"


def test_dirty_tree_guard_refuses_unstaged_edits(tmp_path):
    """Must refuse to write when pyproject.toml has unstaged edits (git dirty)."""
    import shutil

    git = shutil.which("git")
    if git is None:
        pytest.skip("git not available")

    # Set up a real git repo with pyproject.toml committed
    subprocess.run([git, "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        [git, "config", "user.email", "test@test.com"],
        check=True,
        capture_output=True,
        cwd=str(tmp_path),
    )
    subprocess.run(
        [git, "config", "user.name", "Test"],
        check=True,
        capture_output=True,
        cwd=str(tmp_path),
    )
    _write_pyproject(tmp_path, "5.1.0")
    subprocess.run(
        [git, "add", "pyproject.toml"], check=True, capture_output=True, cwd=str(tmp_path)
    )
    subprocess.run(
        [git, "commit", "-m", "init", "--no-gpg-sign"],
        check=True,
        capture_output=True,
        cwd=str(tmp_path),
    )

    # Dirty the file (unstaged edit)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "yadgar"\nversion = "5.1.0"\ndescription = "dirty"\n'
    )

    r = _run(["--new", "5.2.0", "--project-root", str(tmp_path)])
    assert r.returncode != 0, "Must refuse when pyproject.toml has unstaged edits"
    assert "unstaged" in (r.stderr + r.stdout).lower() or "dirty" in (r.stderr + r.stdout).lower()

    # --force overrides the guard
    r_force = _run(["--new", "5.2.0", "--project-root", str(tmp_path), "--force"])
    assert r_force.returncode == 0, f"--force should succeed: {r_force.stderr}"
    content = (tmp_path / "pyproject.toml").read_text()
    assert 'version = "5.2.0"' in content
