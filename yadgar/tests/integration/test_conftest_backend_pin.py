"""Regression tests for conftest backend image version pin.

Ensures integration tests use current backend version from server.json,
not a hardcoded stale literal.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

# Path helpers — resolve relative to this file so tests are portable
_INTEGRATION_DIR = Path(__file__).resolve().parent
_CONFTEST = _INTEGRATION_DIR / "conftest.py"
_REPO_ROOT = _INTEGRATION_DIR.parents[2]
_SERVER_JSON = _REPO_ROOT / "server.json"


def test_conftest_uses_server_json_backend_version():
    """_backend_image() must return the version from server.json, not a stale literal."""
    # Import the function under test from conftest
    import importlib.util

    spec = importlib.util.spec_from_file_location("conftest_mod", _CONFTEST)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    result = mod._backend_image()

    # Read expected value from server.json directly
    data = json.loads(_SERVER_JSON.read_text())
    expected_version = data["backend_version"]
    expected_image = f"openfantasy/yadgar-backend:{expected_version}"

    assert result == expected_image, (
        f"_backend_image() returned {result!r}, expected {expected_image!r}. "
        "conftest.py may still contain a hardcoded version string."
    )


def test_conftest_skips_when_server_json_missing(tmp_path):
    """When server.json is missing, _backend_image() must call pytest.skip."""
    import importlib.util

    import pytest

    spec = importlib.util.spec_from_file_location("conftest_mod2", _CONFTEST)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Patch the module-level _SERVER_JSON path to point at a non-existent file
    missing = tmp_path / "does_not_exist.json"
    with patch.object(mod, "_SERVER_JSON", missing):
        with pytest.raises(pytest.skip.Exception):
            mod._backend_image()


def test_no_hardcoded_5_0_3_in_conftest():
    """Regression gate: 5.0.3 must not appear as a live value in conftest.py.

    Strips comment lines before checking — only executable source is scanned.
    """
    source_lines = _CONFTEST.read_text().splitlines()
    non_comment_lines = [line for line in source_lines if not line.lstrip().startswith("#")]
    non_comment_source = "\n".join(non_comment_lines)

    assert "5.0.3" not in non_comment_source, (
        "Hardcoded backend version '5.0.3' still present in conftest.py "
        "(non-comment source). Replace with _backend_image() lookup."
    )
