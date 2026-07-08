"""v5.46.3 TDD — pyproject.toml test extra includes pytest-asyncio + anyio (B7 fix).

Verifies:
1. [project.optional-dependencies].test includes pytest-asyncio>=0.24
2. [project.optional-dependencies].test includes anyio>=4.0
3. [tool.pytest.ini_options] has asyncio_mode = "auto"
"""

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]

from yadgar.tests._paths import REPO_ROOT

PYPROJECT = REPO_ROOT / "pyproject.toml"


def _load_pyproject():
    return tomllib.loads(PYPROJECT.read_text())


class TestPytestAsyncioExtra:
    """pyproject.toml must include pytest-asyncio and anyio in test extra."""

    def test_pyproject_exists(self):
        assert PYPROJECT.exists(), f"pyproject.toml not found at {PYPROJECT}"

    def test_test_extra_has_pytest_asyncio(self):
        data = _load_pyproject()
        test_deps = data["project"]["optional-dependencies"]["test"]
        asyncio_deps = [d for d in test_deps if "pytest-asyncio" in d]
        assert asyncio_deps, (
            f"[project.optional-dependencies].test must include pytest-asyncio. "
            f"Current test deps: {test_deps}"
        )

    def test_pytest_asyncio_version_constraint(self):
        data = _load_pyproject()
        test_deps = data["project"]["optional-dependencies"]["test"]
        asyncio_dep = next((d for d in test_deps if "pytest-asyncio" in d), None)
        assert asyncio_dep is not None
        # Must have a version constraint (>= or ==)
        assert ">=" in asyncio_dep or "==" in asyncio_dep, (
            f"pytest-asyncio dep must have version constraint: {asyncio_dep}"
        )

    def test_test_extra_has_anyio(self):
        data = _load_pyproject()
        test_deps = data["project"]["optional-dependencies"]["test"]
        anyio_deps = [d for d in test_deps if "anyio" in d]
        assert anyio_deps, (
            f"[project.optional-dependencies].test must include anyio. "
            f"Current test deps: {test_deps}"
        )

    def test_anyio_version_constraint(self):
        data = _load_pyproject()
        test_deps = data["project"]["optional-dependencies"]["test"]
        anyio_dep = next((d for d in test_deps if "anyio" in d), None)
        assert anyio_dep is not None
        assert ">=" in anyio_dep or "==" in anyio_dep, (
            f"anyio dep must have version constraint: {anyio_dep}"
        )

    def test_pytest_asyncio_mode_auto(self):
        data = _load_pyproject()
        ini_opts = data.get("tool", {}).get("pytest", {}).get("ini_options", {})
        assert "asyncio_mode" in ini_opts, (
            "[tool.pytest.ini_options] must set asyncio_mode "
            "(avoids per-test @pytest.mark.asyncio noise)"
        )
        assert ini_opts["asyncio_mode"] == "auto", (
            f"asyncio_mode must be 'auto', got: {ini_opts['asyncio_mode']!r}"
        )
