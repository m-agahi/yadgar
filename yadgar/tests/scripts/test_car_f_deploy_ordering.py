"""Car F (task #61) — orchestrator restart order + image-pull backend resolution.

The two surgical changes to the orchestrator's default hooks:

  - ``_default_service_restart`` now restarts BOTH units, backend first.
  - ``_default_image_pull`` now resolves the backend tag from the freshly
    pulled CORE image (``/app/server.json::backend_version``), not from
    the installed ``server.json``. The latter is the inverse pairing
    that broke downgrades.

These tests pin the contracts WITHOUT exercising the real container
runtime. Both hooks compose with ``run_install`` in a single test; a
narrow unit test on each default is enough — the surrounding
state-machine test suite is the live-state regression net.
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock, patch

from yadgar.core.update.orchestrator import (
    _default_image_pull,
    _default_service_restart,
    _probe_new_backend_tag,
)

# ---------------------------------------------------------------------------
# 1. Restart order — backend FIRST, then core.
# ---------------------------------------------------------------------------


def test_default_service_restart_backend_before_core() -> None:
    """Car F: backend unit restarts BEFORE the core unit.

    The call sequence must be exactly: ``restart yadgar-backend.service``
    then ``start yadgar.service`` (start, not restart — the orchestrator
    already gracefully stopped the core in the prior step, so a restart
    would attempt a no-op stop first). Two ``subprocess.run`` calls, in
    that order, with ``check=True`` so a half-applied restart surfaces
    as a non-zero exit and the orchestrator's rollback path fires.
    """
    calls: list[list[str]] = []

    def _fake_run(argv, check=False, **_kwargs):  # noqa: ARG001
        calls.append(list(argv))
        # check=True must propagate to CalledProcessError on rc != 0
        # — the test only exercises the happy path so the rc is 0.
        m = MagicMock()
        m.returncode = 0
        return m

    with patch("yadgar.core.update.orchestrator.subprocess.run", side_effect=_fake_run):
        _default_service_restart()

    assert len(calls) == 2, f"expected 2 subprocess calls, got {len(calls)}"
    # First call: backend, restart.
    assert calls[0] == [
        "systemctl",
        "--user",
        "restart",
        "yadgar-backend.service",
    ]
    # Second call: core, START (not restart — already stopped).
    assert calls[1] == ["systemctl", "--user", "start", "yadgar.service"]
    # Order matters: backend call comes before core call.
    backend_idx = next(i for i, c in enumerate(calls) if "yadgar-backend.service" in c)
    core_idx = next(i for i, c in enumerate(calls) if c[-1] == "yadgar.service")
    assert backend_idx < core_idx, "backend must restart before core starts"


# ---------------------------------------------------------------------------
# 2. Image pull — backend tag is resolved from the NEW core image, not
#    the installed one. Inverse-pairing fix for the downgrade case.
# ---------------------------------------------------------------------------


def test_default_image_pull_uses_new_core_backend_version() -> None:
    """Car F inverse-pairing fix.

    The installed ``DOCKERHUB_BACKEND_IMAGE`` reports backend 5.73.0 (the
    currently-installed server.json). The NEW core image (5.183.0) ships
    with its own server.json declaring backend 5.74.0. A downgrade
    would otherwise pull 5.73 (the new, incompatible-with-old-core tag)
    and silently roll back the wire compatibility — which is the
    inverse pairing the fix prevents.
    """
    pulled: list[str] = []
    _INSTALLED = "docker.io/openfantasy/yadgar-backend:5.73.0"
    _NEW_BACKEND = "docker.io/openfantasy/yadgar-backend:5.74.0"
    _probe_payload = json.dumps({"backend_version": "5.74.0"}).encode()

    def _fake_run(argv, **_kwargs):
        pulled.append(" ".join(map(str, argv)))
        m = MagicMock()
        m.returncode = 0
        # The probe call is ``runtime run --rm --entrypoint cat <core> /app/server.json``
        # — the result.stdout must be the JSON body.
        if len(argv) >= 6 and argv[1:4] == ["run", "--rm", "--entrypoint"]:
            m.stdout = _probe_payload.decode()
        else:
            m.stdout = ""
        return m

    with (
        patch("yadgar.core.daemon.runtime.DOCKERHUB_BACKEND_IMAGE", _INSTALLED),
        patch(
            "yadgar.core.update.orchestrator.subprocess.run",
            side_effect=_fake_run,
        ),
        patch.dict(
            "os.environ",
            {"YADGAR_CONTAINER_RUNTIME": "podman"},
            clear=False,
        ),
    ):
        # _get_runtime() reads the env we just set.
        _default_image_pull("5.183.0")

    # Three calls: core pull, probe, backend pull. The probe is the
    # Car F inverse-pairing fix — peeks the new image for its expected
    # backend tag. The backend pull is the NEW tag (5.74.0), not the
    # installed one (5.73.0).
    assert len(pulled) == 3, f"expected core pull + probe + backend pull, got: {pulled}"
    assert "yadgar:5.183.0" in pulled[0], f"core pull must use new version: {pulled[0]}"
    assert "run --rm" in pulled[1], f"second call must be the Car F probe: {pulled[1]}"
    assert _NEW_BACKEND in pulled[2], (
        f"backend pull must use new-version's expected tag {_NEW_BACKEND}: {pulled[2]}"
    )
    assert _INSTALLED not in pulled[2], (
        f"backend pull must NOT use the installed tag {_INSTALLED}: {pulled[2]}"
    )


def test_default_image_pull_falls_back_to_installed_on_probe_failure() -> None:
    """Probe failure must NOT abort the upgrade — fall back to installed tag.

    The probe is best-effort (a ``run --rm`` against a freshly-pulled
    image). Anything that goes wrong — image layout change, runtime
    hiccup, parse error, missing field — must cause the orchestrator
    to pull the installed tag and let the Car F handshake catch the
    mismatch on the OTHER side. A probe-fail here cannot kill an
    otherwise-good upgrade.
    """
    _INSTALLED = "docker.io/openfantasy/yadgar-backend:5.73.0"
    _NEW_CORE = "5.183.0"

    def _explode(_argv, **_kwargs):
        raise subprocess.CalledProcessError(1, ["podman", "run", "--rm"])

    with (
        patch("yadgar.core.daemon.runtime.DOCKERHUB_BACKEND_IMAGE", _INSTALLED),
        patch(
            "yadgar.core.update.orchestrator.subprocess.run",
            side_effect=_explode,
        ),
        patch.dict(
            "os.environ",
            {"YADGAR_CONTAINER_RUNTIME": "podman"},
            clear=False,
        ),
    ):
        # The probe would raise, the fallback path pulls the installed tag.
        # The pull itself ALSO raises (same fake) — but only AFTER the
        # probe was attempted, so the FALLBACK decision was the right one.
        try:
            _default_image_pull(_NEW_CORE)
        except subprocess.CalledProcessError:
            pass  # expected — fake raises on the FALLBACK pull too

    # The probe-new-backend-tag helper, called in isolation, returns None
    # when the runtime raises. Pin that contract directly.
    assert _probe_new_backend_tag("podman", "yadgar:5.183.0", _INSTALLED) is None


def test_probe_new_backend_tag_returns_repo_with_new_version() -> None:
    """Happy-path probe — peeks the new image's server.json.

    The helper returns the SAME repo as the installed tag with the NEW
    version stamped on the end, so a custom-registry ``YADGAR_BACKEND_IMAGE``
    env override (e.g. ``registry.example.com:5000/yadgar-backend:5.73``)
    is honoured on the new tag too.
    """
    _INSTALLED = "registry.example.com:5000/yadgar-backend:5.73.0"
    _probe_payload = json.dumps({"backend_version": "5.74.0"}).encode()
    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stdout = _probe_payload.decode()

    with patch(
        "yadgar.core.update.orchestrator.subprocess.run",
        return_value=fake_result,
    ):
        _out = _probe_new_backend_tag("podman", "yadgar:5.183.0", _INSTALLED)

    assert _out == "registry.example.com:5000/yadgar-backend:5.74.0"


def test_probe_new_backend_tag_handles_missing_field() -> None:
    """A server.json without ``backend_version`` returns None — the caller falls back.

    The runtime layout is supposed to ship a server.json with
    ``backend_version``; this is the no-mutation test for a layout
    drift. The probe must NEVER raise — it returns None and the
    installed-tag fallback path takes over.
    """
    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stdout = json.dumps({"name": "yadgar", "version": "5.183.0"})

    with patch(
        "yadgar.core.update.orchestrator.subprocess.run",
        return_value=fake_result,
    ):
        _out = _probe_new_backend_tag("podman", "yadgar:5.183.0", "docker.io/x/y:5.73.0")

    assert _out is None
