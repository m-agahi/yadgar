"""v5.46.12 — backend image version canonical in yadgar/__init__.py.BACKEND_VERSION.

Bug: yadgar-setup.sh and Makefile both pulled ``yadgar-backend:${core_version}``
instead of the independent backend image track version. On Rocky Linux fresh install,
step 2 pulled yadgar:5.46.11 (OK) then tried yadgar-backend:5.46.11 (FAIL — backend
tag is 5.4.0).

Fix: add ``BACKEND_VERSION = "5.4.0"`` to ``yadgar/__init__.py`` as the single
canonical source for the backend image version. setup.sh gets a parallel
``_resolve_backend_version()`` helper (mirrors ``_resolve_yadgar_version``). Makefile
gets ``YADGAR_BACKEND_VERSION`` grepped from ``yadgar/__init__.py``.

Drift guards: pyproject.toml ``[project].version`` must match server.json ``version``
field (file-to-file, independent of install state since importlib.metadata returns
'unknown' in uninstalled dev environments).

Runner note: pure static tests — no server/MCP dependencies.
Run with --noconftest to avoid autouse fixture failures:
  pytest yadgar/tests/test_v5_46_12_backend_version_canonical.py --noconftest
"""

import json
import re
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent
SETUP_SH = REPO_ROOT / "scripts" / "install" / "yadgar-setup.sh"
MAKEFILE = REPO_ROOT / "Makefile"
INIT_PY = REPO_ROOT / "yadgar" / "__init__.py"
PYPROJECT = REPO_ROOT / "pyproject.toml"
SERVER_JSON = REPO_ROOT / "server.json"


@pytest.fixture(scope="module")
def setup_sh_text() -> str:
    assert SETUP_SH.exists(), f"yadgar-setup.sh not found at {SETUP_SH}"
    return SETUP_SH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def makefile_text() -> str:
    assert MAKEFILE.exists(), f"Makefile not found at {MAKEFILE}"
    return MAKEFILE.read_text(encoding="utf-8")


class TestBackendVersionConstant:
    """Tests 1-2: yadgar.BACKEND_VERSION importable and correct."""

    def test_backend_version_importable(self) -> None:
        """yadgar.BACKEND_VERSION must be importable and match semver."""
        import yadgar

        assert hasattr(yadgar, "BACKEND_VERSION"), (
            "yadgar.BACKEND_VERSION not found.\n"
            "Fix: add BACKEND_VERSION = '5.4.0' to yadgar/__init__.py after __version__."
        )
        assert re.match(r"^\d+\.\d+\.\d+$", yadgar.BACKEND_VERSION), (
            f"yadgar.BACKEND_VERSION = {yadgar.BACKEND_VERSION!r} does not match semver.\n"
            "Expected format: X.Y.Z"
        )

    def test_backend_version_value(self) -> None:
        """yadgar.BACKEND_VERSION must be '5.15.0' (bumped in recall backend forward-only)."""
        import yadgar

        assert yadgar.BACKEND_VERSION == "5.15.0", (
            f"yadgar.BACKEND_VERSION = {yadgar.BACKEND_VERSION!r}, expected '5.15.0'.\n"
            "Fix: set BACKEND_VERSION = '5.15.0' in yadgar/__init__.py."
        )


class TestSetupShBackendVersion:
    """Tests 3-6: yadgar-setup.sh _resolve_backend_version() + correct image references."""

    def test_resolve_backend_version_defined(self, setup_sh_text: str) -> None:
        """Test 3: setup.sh must define _resolve_backend_version() function."""
        assert "_resolve_backend_version()" in setup_sh_text, (
            "_resolve_backend_version() not found in yadgar-setup.sh.\n"
            "Fix: add _resolve_backend_version() parallel to _resolve_yadgar_version()."
        )

    def test_resolve_backend_version_uses_shim_pattern(self, setup_sh_text: str) -> None:
        """Test 4: _resolve_backend_version() must use shim shebang pattern.

        Must use 'command -v yadgar' (shim lookup) and read BACKEND_VERSION
        from the venv python via the shim shebang, mirroring _resolve_yadgar_version.
        """
        # Extract the _resolve_backend_version function body
        match = re.search(
            r"_resolve_backend_version\(\)\s*\{(.+?)^}",
            setup_sh_text,
            re.DOTALL | re.MULTILINE,
        )
        assert match is not None, (
            "_resolve_backend_version() function body not found.\n"
            "Fix: define the function with a closing } at column 0."
        )
        fn_body = match.group(1)
        has_shim_lookup = "command -v yadgar" in fn_body
        has_shebang_read = "head -1" in fn_body or "yadgar_shim" in fn_body
        has_backend_attr = "BACKEND_VERSION" in fn_body
        assert has_shim_lookup and (has_shebang_read or has_backend_attr), (
            "_resolve_backend_version() does not use the shim shebang pattern.\n"
            f"has_shim_lookup={has_shim_lookup}, has_shebang_read={has_shebang_read}, "
            f"has_backend_attr={has_backend_attr}.\n"
            "Fix: mirror _resolve_yadgar_version — extract venv python from shim shebang, "
            "then call: $venv_python -c 'import yadgar; print(yadgar.BACKEND_VERSION)'"
        )

    def test_no_backend_image_using_core_version_var(self, setup_sh_text: str) -> None:
        """Test 5: no line uses yadgar-backend:${version} (core version var for backend)."""
        # Match lines where backend image uses the core version variable
        pattern = re.compile(r"yadgar-backend:\$\{version\}")
        bad_lines = []
        for lineno, line in enumerate(setup_sh_text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if pattern.search(line):
                bad_lines.append((lineno, stripped))

        assert not bad_lines, (
            "Found backend image references using core version variable ${version}:\n"
            + "\n".join(f"  line {ln}: {text}" for ln, text in bad_lines)
            + "\n\nFix: replace ${version} with ${backend_version} for yadgar-backend image."
        )

    def test_backend_image_references_use_backend_var(self, setup_sh_text: str) -> None:
        """Test 6: all backend image references use ${backend_version}."""
        pattern = re.compile(r"yadgar-backend:\$\{backend_version\}")
        matches = [
            (lineno, line.strip())
            for lineno, line in enumerate(setup_sh_text.splitlines(), start=1)
            if not line.strip().startswith("#") and pattern.search(line)
        ]
        assert len(matches) >= 3, (
            f"Expected >= 3 backend image references using ${{backend_version}}, found {len(matches)}.\n"
            "Sites: _step_pull_images (line ~328), _step_generate_units systemd (~363), "
            "_step_generate_units launchd (~381).\n"
            "Fix: replace ${version} with ${backend_version} at all 3 backend image sites."
        )


class TestMakefileBackendVersion:
    """Tests 7-9: Makefile YADGAR_BACKEND_VERSION variable."""

    def test_makefile_defines_backend_version_var(self, makefile_text: str) -> None:
        """Test 7: Makefile defines YADGAR_BACKEND_VERSION variable."""
        assert "YADGAR_BACKEND_VERSION" in makefile_text, (
            "YADGAR_BACKEND_VERSION not found in Makefile.\n"
            "Fix: add 'YADGAR_BACKEND_VERSION := $(shell grep -m1 ...)' after YADGAR_VERSION."
        )

    def test_makefile_backend_version_reads_from_init_py(self, makefile_text: str) -> None:
        """Test 8: YADGAR_BACKEND_VERSION reads from yadgar/__init__.py via grep."""
        pattern = re.compile(
            r"YADGAR_BACKEND_VERSION\s*:=\s*\$\(shell\s+grep\s+-m1\s+'?\^?BACKEND_VERSION"
        )
        found = pattern.search(makefile_text)
        assert found is not None, (
            "YADGAR_BACKEND_VERSION does not read from yadgar/__init__.py via grep.\n"
            "Expected: YADGAR_BACKEND_VERSION := $(shell grep -m1 '^BACKEND_VERSION' "
            "...yadgar/__init__.py | cut -d'\"' -f2)\n"
            "Fix: add the grep-based assignment in Makefile."
        )

    def test_no_backend_image_using_yadgar_version(self, makefile_text: str) -> None:
        """Test 9: no Makefile line uses yadgar-backend:$(YADGAR_VERSION)."""
        pattern = re.compile(r"yadgar-backend:\$\(YADGAR_VERSION\)")
        bad_lines = []
        for lineno, line in enumerate(makefile_text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if pattern.search(line):
                bad_lines.append((lineno, stripped))

        assert not bad_lines, (
            "Found backend image references using $(YADGAR_VERSION) in Makefile:\n"
            + "\n".join(f"  line {ln}: {text}" for ln, text in bad_lines)
            + "\n\nFix: replace $(YADGAR_VERSION) with $(YADGAR_BACKEND_VERSION) "
            "for yadgar-backend image references."
        )


class TestDriftGuards:
    """Tests 10-11: drift guards — pyproject + server.json versions must match."""

    def test_pyproject_version_matches_server_json(self) -> None:
        """Test 10 (drift guard): pyproject.toml [project].version == server.json version.

        File-to-file comparison (does not depend on install state of the package).
        """
        pyproject_data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
        pyproject_version = pyproject_data["project"]["version"]

        server_data = json.loads(SERVER_JSON.read_text(encoding="utf-8"))
        server_version = server_data["version"]

        assert pyproject_version == server_version, (
            f"Version drift: pyproject.toml says {pyproject_version!r}, "
            f"server.json says {server_version!r}.\n"
            "Fix: bump both to the same version string."
        )

    def test_init_py_backend_version_matches_server_json_backend_version(self) -> None:
        """Test 11 (drift guard): yadgar.BACKEND_VERSION == server.json backend_version.

        server.json already has a 'backend_version' field — keep it in sync.
        """
        import yadgar

        server_data = json.loads(SERVER_JSON.read_text(encoding="utf-8"))
        server_backend_version = server_data.get("backend_version", "MISSING")

        assert yadgar.BACKEND_VERSION == server_backend_version, (
            f"Backend version drift: yadgar.BACKEND_VERSION={yadgar.BACKEND_VERSION!r}, "
            f"server.json backend_version={server_backend_version!r}.\n"
            "Fix: keep yadgar/__init__.py BACKEND_VERSION and server.json backend_version in sync."
        )
