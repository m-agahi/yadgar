"""Cross-generator regression: every in-repo backend-unit generator must wire
YADGAR_QUEUE_BASE as an actually-mounted path.

fix-systemd-generate-missing-queue-base-2026-07-28 (Car 3) found that
`generate_systemd.sh` (via `yadgar-backend.service.in`) and `generate_launchd.sh`
(via `com.openfantasy.yadgar-backend.plist.in`) set NO `YADGAR_QUEUE_BASE` at
all — `_queue_base_path()` (yadgar/backend/embed_service/embed_service_lifecycle.py)
has no fallback, so a real install's backend drainer never started and queued
writes never committed. `install_systemd_service` (yadgar/core/daemon/systemd.py)
was already fixed by #233 and is covered directly by
`yadgar/tests/core/test_daemon_queue_wiring.py::test_systemd_backend_unit_wires_shared_queue_volume`.

This is the anti-drift net the plan calls for: a FUTURE generator (or a change
to an existing one) that forgets the var — or sets it to a value that isn't
actually mounted (e.g. copy-pasting `/queue-data` from the systemd.py/daemon.py/
docker-compose.yml convention onto a surface that only mounts `/data`) — fails
this ONE shared test, not just its own generator-specific suite.

Nix (modules/home/yadgar.nix) renders the same contract but lives in the
dotfiles repo, out-of-repo and unreachable from this pytest suite — see the
`/data` YADGAR_QUEUE_BASE convention noted there.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from yadgar.core.daemon import systemd as systemd_mod
from yadgar.core.daemon.profiles import _prod_profile
from yadgar.tests._paths import REPO_ROOT
from yadgar.tests._unit_render import RENDERER_CLI

BASH = shutil.which("bash") or "/run/current-system/sw/bin/bash"
INSTALL_DIR = REPO_ROOT / "scripts" / "install"
GENERATE_SYSTEMD_SH = INSTALL_DIR / "generate_systemd.sh"
GENERATE_LAUNCHD_SH = INSTALL_DIR / "generate_launchd.sh"

_QUEUE_BASE_RE = re.compile(r"YADGAR_QUEUE_BASE=([^\s\\\"'&;>]+)")


def _assert_queue_base_is_mounted(rendered: str, label: str) -> None:
    """YADGAR_QUEUE_BASE must be set AND its value must be a `-v host:target`
    mount target in the same rendered unit — proves the base is actually
    reachable, not just an env var pointing at a path nothing mounts."""
    match = _QUEUE_BASE_RE.search(rendered)
    assert match, f"{label}: YADGAR_QUEUE_BASE not set in rendered unit"
    base = match.group(1)
    assert re.search(rf"-v\s+\S+:{re.escape(base)}\b", rendered), (
        f"{label}: YADGAR_QUEUE_BASE={base!r} is not a `-v <host>:{base}` mount "
        f"target anywhere in the rendered unit — the drainer would watch an "
        f"unmounted path"
    )


def _render_systemd_backend(tmp_path: Path) -> str:
    env = dict(os.environ)
    env.update(
        {
            "YADGAR_SYSTEMD_OUTPUT_DIR": str(tmp_path),
            "YADGAR_RUNTIME": "podman",
            "YADGAR_INSTALL_PREFIX": "/home/testuser/.yadgar",
            "YADGAR_SECRETS_ENV_FILE": "/home/testuser/.yadgar/secrets.env",
            "YADGAR_BACKEND_IMAGE": "openfantasy/yadgar-backend:test",
            "YADGAR_CORE_IMAGE": "openfantasy/yadgar:test",
            "YADGAR_RENDERER_CLI": RENDERER_CLI,
        }
    )
    result = subprocess.run(
        [BASH, str(GENERATE_SYSTEMD_SH)], capture_output=True, text=True, env=env
    )
    assert result.returncode == 0, f"generate_systemd.sh failed\n{result.stderr}"
    return (tmp_path / "yadgar-backend.service").read_text()


def _render_launchd_backend(tmp_path: Path) -> str:
    env = dict(os.environ)
    env.update(
        {
            "YADGAR_LAUNCHD_OUTPUT_DIR": str(tmp_path),
            "YADGAR_RUNTIME": "podman",
            "YADGAR_INSTALL_PREFIX": "/home/testuser/.yadgar",
            "YADGAR_SECRETS_ENV_FILE": "/home/testuser/.yadgar/secrets.env",
            "YADGAR_BACKEND_IMAGE": "openfantasy/yadgar-backend:test",
            "YADGAR_CORE_IMAGE": "openfantasy/yadgar:test",
            "YADGAR_HOME": "/home/testuser",
        }
    )
    result = subprocess.run(
        [BASH, str(GENERATE_LAUNCHD_SH)], capture_output=True, text=True, env=env
    )
    assert result.returncode == 0, f"generate_launchd.sh failed\n{result.stderr}"
    return (tmp_path / "com.openfantasy.yadgar-backend.plist").read_text()


def _render_python_systemd_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("YADGAR_VOLUME", "yadgar-data")
    profile = _prod_profile(8765)
    result = systemd_mod.install_systemd_service(profile, dev=False)
    return Path(result["backend_service"]).read_text()


@pytest.mark.parametrize(
    "label",
    [
        "generate_systemd.sh",
        "generate_launchd.sh",
        "install_systemd_service (Python)",
    ],
)
def test_backend_generator_wires_queue_base_as_mount_target(label, tmp_path, monkeypatch):
    """Every in-repo backend-unit generator renders YADGAR_QUEUE_BASE as a
    mounted path (anti-drift net for fix-systemd-generate-missing-queue-base-2026-07-28)."""
    if label == "generate_systemd.sh":
        rendered = _render_systemd_backend(tmp_path)
    elif label == "generate_launchd.sh":
        rendered = _render_launchd_backend(tmp_path)
    else:
        rendered = _render_python_systemd_backend(tmp_path, monkeypatch)
    _assert_queue_base_is_mounted(rendered, label)
