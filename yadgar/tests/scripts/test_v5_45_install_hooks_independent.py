"""v5.45.0 Step 1 TDD — install_hooks_lib.py daemon-independence (GREEN even now)."""

import ast

import pytest

from yadgar.tests._paths import REPO_ROOT

HOOKS_LIB = REPO_ROOT / "yadgar" / "core" / "install" / "install_hooks_lib.py"


class TestV5_45InstallHooksIndependent:
    """install_hooks_lib.py must have zero daemon imports (DP4 confirmation)."""

    def test_v5_45_install_hooks_lib_exists(self):
        """install_hooks_lib.py must exist."""
        assert HOOKS_LIB.exists(), f"install_hooks_lib.py not found at {HOOKS_LIB}"

    def test_v5_45_install_hooks_lib_no_daemon_import(self):
        """install_hooks_lib.py must not import yadgar.daemon or yadgar.server."""
        content = HOOKS_LIB.read_text()
        tree = ast.parse(content, filename=str(HOOKS_LIB))
        daemon_imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if "daemon" in alias.name or "server" in alias.name:
                        daemon_imports.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module and ("daemon" in node.module or "server" in node.module):
                    daemon_imports.append(node.module)
        assert not daemon_imports, (
            f"install_hooks_lib.py must not import daemon/server modules.\nFound: {daemon_imports}"
        )

    def test_v5_45_install_hooks_lib_no_config_import(self):
        """install_hooks_lib.py must not import yadgar.config (which needs pydantic_settings)."""
        content = HOOKS_LIB.read_text()
        tree = ast.parse(content, filename=str(HOOKS_LIB))
        config_imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module and "yadgar.config" in node.module:
                    config_imports.append(node.module)
        assert not config_imports, (
            f"install_hooks_lib.py must not import yadgar.config (requires pydantic_settings).\n"
            f"Found: {config_imports}"
        )

    def test_v5_45_make_install_hooks_independent_of_install_units(self):
        """Makefile install-hooks target must NOT depend on install-units or setup."""
        makefile = REPO_ROOT / "Makefile"
        if not makefile.exists():
            pytest.skip("Makefile not yet created (Step 4)")
        content = makefile.read_text()
        # Find install-hooks recipe
        lines = content.splitlines()
        for line in lines:
            if line.startswith("install-hooks:"):
                # Check deps on same line
                deps = line.split(":", 1)[1].strip()
                assert "install-units" not in deps, (
                    "install-hooks must NOT depend on install-units (daemon-independent)"
                )
                assert "setup" not in deps or deps == "", (
                    "install-hooks must NOT depend on setup target"
                )
                break
