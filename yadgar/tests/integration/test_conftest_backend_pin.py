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


def test_conftest_uses_server_json_backend_version(monkeypatch):
    """_backend_image() must return the version from server.json, not a stale literal.

    The env override is cleared explicitly: the CI job that runs this file also
    exports ``YADGAR_TEST_BACKEND_IMAGE`` (it resolves a pullable tag because
    server.json's PENDING version is unpublished until release). Without the
    delenv this test asserts the derivation path while the override is active,
    and fails for a reason that has nothing to do with the pin it guards.
    """
    monkeypatch.delenv("YADGAR_TEST_BACKEND_IMAGE", raising=False)
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


def test_conftest_skips_when_server_json_missing(tmp_path, monkeypatch):
    """When server.json is missing, _backend_image() must call pytest.skip.

    Env override cleared for the same reason as the test above — with it set,
    _backend_image() returns before ever reading _SERVER_JSON.
    """
    monkeypatch.delenv("YADGAR_TEST_BACKEND_IMAGE", raising=False)
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


def test_backend_image_env_override_wins(monkeypatch):
    """YADGAR_TEST_BACKEND_IMAGE overrides the server.json derivation.

    Pins the escape hatch the CI job depends on. server.json carries the PENDING
    backend_version and images are published at RELEASE, so on a version-bumping
    PR the derived tag does not exist; the job resolves a pullable tag and passes
    it in through this variable. If the override stopped being honoured the job
    would silently go back to deriving an unpullable tag, the fixture would SKIP,
    and the skip-is-not-a-pass gate would fail the build with a misleading reason.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("conftest_mod3", _CONFTEST)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    pinned = "docker.io/openfantasy/yadgar-backend:9.99.99-test"
    monkeypatch.setenv("YADGAR_TEST_BACKEND_IMAGE", pinned)
    assert mod._backend_image() == pinned

    # Whitespace-only is not an override — it must fall through to server.json.
    monkeypatch.setenv("YADGAR_TEST_BACKEND_IMAGE", "   ")
    data = json.loads(_SERVER_JSON.read_text())
    assert mod._backend_image() == f"openfantasy/yadgar-backend:{data['backend_version']}"


def test_docker_endpoint_host_resolves_remote_and_local_daemons(monkeypatch):
    """_docker_endpoint_host() must follow DOCKER_HOST, not assume 127.0.0.1.

    The CI runner reaches a docker:dind SIDECAR over DOCKER_HOST=tcp://dind:2375,
    so the daemon creating the container is in a different network namespace from
    pytest. A port published by that daemon is NOT on the runner's loopback —
    measured on the self-hosted runner 2026-08-19: runner 10.89.1.6, dind
    10.89.1.2. Probing 127.0.0.1 there could only ever time out, which is exactly
    what the health wait did on every CI run.

    Pinned in BOTH directions: a remote daemon must NOT resolve to loopback, and
    the ordinary local case must NOT regress to something else.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("conftest_mod4", _CONFTEST)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Remote TCP daemon → its hostname, never loopback.
    monkeypatch.setenv("DOCKER_HOST", "tcp://dind:2375")
    assert mod._docker_endpoint_host() == "dind"

    monkeypatch.setenv("DOCKER_HOST", "tcp://10.89.1.2:2375")
    assert mod._docker_endpoint_host() == "10.89.1.2"

    # Local daemon shapes → loopback (unset, unix socket, empty).
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    assert mod._docker_endpoint_host() == "127.0.0.1"

    monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")
    assert mod._docker_endpoint_host() == "127.0.0.1"

    monkeypatch.setenv("DOCKER_HOST", "   ")
    assert mod._docker_endpoint_host() == "127.0.0.1"


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
