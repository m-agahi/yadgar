"""v5.49.2 hotfix TDD — backend image tag + core start DB env vars (Rocky VM iter-2).

Tests T1–T7 cover the 2 bugs fixed in this hotfix:
  Bug 12: DOCKERHUB_BACKEND_IMAGE resolves wrong tag (core version instead of backend_version)
  Bug 13: core start() doesn't pass DB env vars (YADGAR_DB_USER/PASS etc.)
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _start_core_argv(monkeypatch, tmp_path, secrets_exists: bool = True):
    """Return the argv list passed to subprocess.run during start() core container launch."""
    secrets_path = tmp_path / "secrets.env"
    if secrets_exists:
        secrets_path.write_text("YADGAR_DB_USER=root\n")
    monkeypatch.setenv("YADGAR_SECRETS_ENV_FILE", str(secrets_path))

    captured = []

    def fake_run(cmd, **kwargs):
        captured.append(cmd)
        m = MagicMock()
        m.returncode = 0
        m.stdout = "abc123deadbeef"
        m.stderr = ""
        return m

    monkeypatch.setattr(subprocess, "run", fake_run)

    from yadgar.daemon import YadgarDaemon

    daemon = YadgarDaemon()
    daemon._container_running = lambda name: False
    daemon._image_exists = lambda img: True
    daemon._health_ok = lambda port: True
    daemon.start_backend = lambda: {"status": "started", "container": "yadgar-backend"}

    daemon.start()

    docker_run_calls = [c for c in captured if len(c) > 2 and c[1] == "run"]
    assert docker_run_calls, f"No 'docker run' call captured. All calls: {captured}"
    return docker_run_calls[-1]


# ---------------------------------------------------------------------------
# T1: DOCKERHUB_BACKEND_IMAGE uses backend_version, not core version
# ---------------------------------------------------------------------------


def test_dockerhub_backend_image_uses_backend_version():
    """Bug 12: DOCKERHUB_BACKEND_IMAGE must end with the backend_version tag (5.4.0),
    not the core pip version (5.49.2).
    """
    import json

    from yadgar import daemon as daemon_mod

    server_json = Path(__file__).resolve().parent.parent.parent / "server.json"
    data = json.loads(server_json.read_text())
    backend_version = data["backend_version"]
    core_version = data["version"]

    assert daemon_mod.DOCKERHUB_BACKEND_IMAGE.endswith(f":{backend_version}"), (
        f"DOCKERHUB_BACKEND_IMAGE={daemon_mod.DOCKERHUB_BACKEND_IMAGE!r} "
        f"does not end with backend_version :{backend_version}. "
        f"Core version is {core_version!r}."
    )
    if core_version != backend_version:
        assert not daemon_mod.DOCKERHUB_BACKEND_IMAGE.endswith(f":{core_version}"), (
            f"DOCKERHUB_BACKEND_IMAGE erroneously uses core version :{core_version}. "
            f"Expected :{backend_version}."
        )


# ---------------------------------------------------------------------------
# T2: DOCKERHUB_IMAGE still uses core version
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    reason="v5.49.4 bisect: daemon.DOCKERHUB_IMAGE uses importlib.metadata.version('yadgar') which returns the system-installed package version, not the dev version in pyproject.toml. Fails in worktree dev environments where yadgar is installed system-wide at a different version. Not a code bug. Refactor: use server.json as source of truth in daemon._default_image() too. Tracked as v5.50+.",
    strict=False,
)
def test_dockerhub_image_still_uses_core_version():
    """Bug 12 sanity: DOCKERHUB_IMAGE (core) must still use the core pip version."""
    import json

    from yadgar import daemon as daemon_mod

    server_json = Path(__file__).resolve().parent.parent.parent / "server.json"
    data = json.loads(server_json.read_text())
    core_version = data["version"]

    assert daemon_mod.DOCKERHUB_IMAGE.endswith(
        f":{core_version}"
    ) or daemon_mod.DOCKERHUB_IMAGE.endswith(":latest"), (
        f"DOCKERHUB_IMAGE={daemon_mod.DOCKERHUB_IMAGE!r} does not end with :{core_version} or :latest."
    )
    assert "yadgar-backend" not in daemon_mod.DOCKERHUB_IMAGE, (
        f"DOCKERHUB_IMAGE should be the core image, got: {daemon_mod.DOCKERHUB_IMAGE!r}"
    )


# ---------------------------------------------------------------------------
# T3: core start() passes -e YADGAR_DB_USER
# ---------------------------------------------------------------------------


def test_core_start_passes_yadgar_db_user_env_flag(monkeypatch, tmp_path):
    """Bug 13: core start() must pass -e YADGAR_DB_USER (no =value suffix) to docker run."""
    argv = _start_core_argv(monkeypatch, tmp_path)
    e_vals = []
    for i, tok in enumerate(argv):
        if tok == "-e" and i + 1 < len(argv):
            e_vals.append(argv[i + 1])

    assert "YADGAR_DB_USER" in e_vals, (
        f"-e YADGAR_DB_USER not found in core start() argv. Got e_vals={e_vals}"
    )
    # Must be bare (no =value)
    for val in e_vals:
        if val.startswith("YADGAR_DB_USER"):
            assert "=" not in val, (
                f"-e {val!r} must be bare (no =value suffix) so docker passes env through."
            )


# ---------------------------------------------------------------------------
# T4: core start() passes -e YADGAR_DB_PASS
# ---------------------------------------------------------------------------


def test_core_start_passes_yadgar_db_pass_env_flag(monkeypatch, tmp_path):
    """Bug 13: core start() must pass -e YADGAR_DB_PASS (no =value suffix) to docker run."""
    argv = _start_core_argv(monkeypatch, tmp_path)
    e_vals = []
    for i, tok in enumerate(argv):
        if tok == "-e" and i + 1 < len(argv):
            e_vals.append(argv[i + 1])

    assert "YADGAR_DB_PASS" in e_vals, (
        f"-e YADGAR_DB_PASS not found in core start() argv. Got e_vals={e_vals}"
    )
    for val in e_vals:
        if val.startswith("YADGAR_DB_PASS"):
            assert "=" not in val, f"-e {val!r} must be bare (no =value suffix)."


# ---------------------------------------------------------------------------
# T5: core start() passes all RW/RO env flags
# ---------------------------------------------------------------------------


def test_core_start_passes_rw_ro_env_flags(monkeypatch, tmp_path):
    """Bug 13: core start() must pass -e for all 4 RW/RO secret vars."""
    argv = _start_core_argv(monkeypatch, tmp_path)
    e_vals = []
    for i, tok in enumerate(argv):
        if tok == "-e" and i + 1 < len(argv):
            e_vals.append(argv[i + 1])

    for required in ("YADGAR_RW_USER", "YADGAR_RW_PASS", "YADGAR_RO_USER", "YADGAR_RO_PASS"):
        assert required in e_vals, (
            f"-e {required} not found in core start() argv. Got e_vals={e_vals}"
        )


# ---------------------------------------------------------------------------
# T6: core start() passes --env-file when secrets file exists
# ---------------------------------------------------------------------------


def test_core_start_passes_env_file_when_present(monkeypatch, tmp_path):
    """Bug 13: core start() must pass --env-file <SECRETS_ENV_PATH> when file exists."""
    secrets_path = tmp_path / "secrets.env"
    secrets_path.write_text("YADGAR_DB_USER=root\n")
    monkeypatch.setenv("YADGAR_SECRETS_ENV_FILE", str(secrets_path))

    argv = _start_core_argv(monkeypatch, tmp_path, secrets_exists=True)

    assert "--env-file" in argv, f"--env-file not found in core start() argv: {argv}"
    assert str(secrets_path) in argv, f"secrets path not in argv: {argv}"


# ---------------------------------------------------------------------------
# T7: backend start_backend() also passes YADGAR_DB_USER (bootstrap safety)
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason="entrypoint-backend.sh uses SURREAL_USER/PASS (not YADGAR_DB_USER/PASS) "
    "for SurrealDB bootstrap — backend does not require YADGAR_DB_USER. "
    "Test skipped per v5.49.2 investigation of entrypoint-backend.sh."
)
def test_backend_start_passes_yadgar_db_user_for_bootstrap(monkeypatch, tmp_path):
    """T7 (skipped): entrypoint-backend.sh does not read YADGAR_DB_USER/PASS.
    Backend bootstrap uses SURREAL_USER/PASS, which are already passed in start_backend().
    No action required.
    """
    pass
