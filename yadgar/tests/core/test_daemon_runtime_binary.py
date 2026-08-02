"""Yadgar must invoke the DETECTED container runtime — never a literal binary name.

task:0083 — empirical bug (2026-07-29, fresh Debian 13 VM, yadgar 5.168.0, podman
5.4.2, no docker binary): ``yadgar daemon start`` died with
``FileNotFoundError: [Errno 2] No such file or directory: 'docker'`` because
``daemon.py`` templated ``["docker", "rm", ...]`` even though ``check_runtime()``
had already resolved podman.

task:0101 — the mirror image, on the ``yadgar upgrade`` path:
``core/update/orchestrator.py::_default_image_pull`` templated
``["podman", "pull", ...]``, which dies the same way on a docker-only host. It
survived because this guard used to be SCOPED to ``yadgar/core/daemon/`` plus
``core/cli/daemon.py`` — ``core/update/`` was never looked at — and because the
detector only knew the name ``"docker"``, so a hardcoded ``"podman"`` read as
clean. Both holes are closed here: the scope is now the whole repo (minus tests)
and the detector knows both names. Deliberate sites live in
``.container-runtime-allowlist.json`` with a written rationale; narrowing the
scope back down is what a widened guard exists to prevent.

task:0104 — the THIRD instance of the same class, and the one the first two
guards were structurally unable to see. ``yadgar/core/daemon/systemd.py``
hardcoded ``docker`` 13 times inside the systemd USER UNITS it generates:
``Requires=``/``After=docker.service``, ``ExecStartPre=-docker …``,
``ExecStart=docker run …``, ``ExecStop=docker stop …`` — for BOTH the backend
and the core unit. On a podman-only host the generated units name a service
that does not exist and a binary that is not installed, so the whole
``yadgar daemon install-systemd`` path is dead there.

The argv-head detector below could never catch it: these are f-string
TEMPLATES emitting unit text, not ``ast.List`` argv. So a second, differently
shaped detector is added — one that reads generated/checked-in systemd unit
DIRECTIVES (``Exec*=``, ``Requires=``/``After=``/…) and fails on a literal
runtime binary or a runtime ``.service``/``.socket`` dependency. Its scope
deliberately spans BOTH unit generators (the Python one in ``systemd.py`` and
the ``scripts/install/*.in`` templates the shell installer renders), because
the cross-generator tests prove both are live install paths.

These tests pin three things:
  1. behaviour — every subprocess invocation resolves the binary via
     ``_get_runtime()`` (daemon, CLI graceful-stop, and the upgrade orchestrator),
     and every GENERATED systemd unit names the resolved runtime, not a literal;
  2. source shape — an AST guard that fails if a literal ``"docker"`` / ``"podman"``
     argv head appears anywhere in ``yadgar/`` or ``scripts/`` unallowlisted;
  3. unit shape — a text guard that fails if a systemd unit directive in any
     generator or checked-in unit names a runtime binary literally.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest


class _RunRecorder:
    """Fake ``subprocess.run`` that records argv and can simulate a missing binary."""

    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: str = "",
        missing: tuple[str, ...] = (),
    ) -> None:
        self.calls: list[list[str]] = []
        self.returncode = returncode
        self.stdout = stdout
        self.missing = set(missing)

    def __call__(self, argv, *args, **kwargs):  # noqa: ANN001 — subprocess.run signature
        argv = list(argv)
        self.calls.append(argv)
        if argv and argv[0] in self.missing:
            raise FileNotFoundError(2, "No such file or directory", argv[0])
        # Runtime probes (`<rt> --version` / `<rt> version --format ...`) must succeed
        # so the detection path resolves to the installed runtime.
        if len(argv) > 1 and argv[1] in {"--version", "version"}:
            return subprocess.CompletedProcess(argv, 0, "5.4.2\n", "")
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, "")

    @property
    def binaries(self) -> list[str]:
        return [c[0] for c in self.calls if c]


def _no_docker(recorder: _RunRecorder) -> None:
    assert "docker" not in recorder.binaries, f"literal 'docker' argv head in {recorder.calls!r}"


@pytest.fixture
def podman_only(monkeypatch):
    """Simulate a podman-only host: no runtime env override, no cached runtime."""
    from yadgar.core.daemon import runtime as runtime_mod

    monkeypatch.delenv("YADGAR_CONTAINER_RUNTIME", raising=False)
    monkeypatch.setattr(runtime_mod, "_RUNTIME", None)


@pytest.fixture
def podman_env(monkeypatch):
    """Pin the runtime to podman via the documented env override."""
    monkeypatch.setenv("YADGAR_CONTAINER_RUNTIME", "podman")


@pytest.fixture
def docker_env(monkeypatch):
    """Pin the runtime to docker — the fixture that exposes a hardcoded "podman".

    task:0101: a test run under ``podman_env`` cannot tell a resolved runtime
    from a hardcoded ``"podman"`` literal, because both produce the same argv.
    Anything asserting "the DETECTED runtime is used" against a podman literal
    must pin docker instead.
    """
    monkeypatch.setenv("YADGAR_CONTAINER_RUNTIME", "docker")


# ── headline reproduction ─────────────────────────────────────────────────────


def test_start_on_podman_only_host_never_invokes_docker(podman_only, monkeypatch):
    """RED before the fix: start() raised FileNotFoundError on the `docker rm` line."""
    from yadgar.core.daemon import YadgarDaemon

    monkeypatch.delenv("YADGAR_CONTAINER", raising=False)
    rec = _RunRecorder(returncode=1, missing=("docker",))

    with patch("subprocess.run", rec):
        result = YadgarDaemon().start()

    # Bounded early exit: image not present → failed status, no network/backend work.
    assert result["status"] == "failed"
    assert "not found" in result["reason"]
    _no_docker(rec)
    assert "podman" in rec.binaries


def test_start_backend_on_podman_only_host_never_invokes_docker(podman_only):
    from yadgar.core.daemon import YadgarDaemon

    rec = _RunRecorder(returncode=1, missing=("docker",))
    with patch("subprocess.run", rec):
        result = YadgarDaemon().start_backend()

    assert result["status"] == "failed"
    _no_docker(rec)
    assert "podman" in rec.binaries


# ── per-method argv assertions ────────────────────────────────────────────────


def test_stop_uses_detected_runtime(podman_env):
    from yadgar.core.daemon import YadgarDaemon

    rec = _RunRecorder(returncode=0)
    with patch("subprocess.run", rec):
        YadgarDaemon().stop()

    _no_docker(rec)
    assert [c for c in rec.calls if c[1] == "stop"], "no stop invocation recorded"


def test_pull_uses_detected_runtime(podman_env):
    from yadgar.core.daemon import YadgarDaemon

    rec = _RunRecorder(returncode=0)
    with patch("subprocess.run", rec):
        result = YadgarDaemon().pull()

    assert result["ok"] is True
    _no_docker(rec)
    assert rec.calls[-1][:2] == ["podman", "pull"]


# task:0099 — empirical bug on a fresh Debian 13 VM (2026-07-31, yadgar 5.170.0):
# `daemon pull` fetched ONLY the core image. `daemon start` then required the
# backend image too, printed "Run: yadgar daemon pull" as the fix, and running
# that command again pulled the exact same (already-present) core image —
# a dead end with no command able to fetch the backend image at all.
def test_pull_pulls_both_core_and_backend_images(podman_env):
    """`pull()` must request BOTH the core and backend images, resolving the
    backend tag the same way start_backend()/build(backend=True) do — so
    `pull` and `start` can never disagree about which tag they want."""
    from yadgar.core.daemon import YadgarDaemon
    from yadgar.core.daemon.profiles import _prod_profile
    from yadgar.core.daemon.runtime import DOCKERHUB_BACKEND_IMAGE

    rec = _RunRecorder(returncode=0)
    with patch("subprocess.run", rec):
        result = YadgarDaemon().pull()

    assert result["ok"] is True
    _no_docker(rec)
    pull_calls = [c for c in rec.calls if len(c) > 1 and c[1] == "pull"]
    pulled_images = {c[2] for c in pull_calls if len(c) > 2}
    core_image = _prod_profile().image_name
    assert core_image in pulled_images, f"core image not pulled: {pulled_images!r}"
    assert DOCKERHUB_BACKEND_IMAGE in pulled_images, (
        "backend image not pulled — reproduces task:0099 dead-end "
        f"(`daemon pull` fetched only core, leaving `daemon start` with no "
        f"command able to fetch the backend image): {pulled_images!r}"
    )


def test_pull_resolves_backend_image_via_env_override(podman_env, monkeypatch):
    """pull() must honor YADGAR_BACKEND_IMAGE exactly like start_backend() does."""
    from yadgar.core.daemon import YadgarDaemon

    monkeypatch.setenv("YADGAR_BACKEND_IMAGE", "docker.io/openfantasy/yadgar-backend:custom-tag")
    rec = _RunRecorder(returncode=0)
    with patch("subprocess.run", rec):
        result = YadgarDaemon().pull()

    assert result["ok"] is True
    pull_calls = [c for c in rec.calls if len(c) > 1 and c[1] == "pull"]
    pulled_images = {c[2] for c in pull_calls if len(c) > 2}
    assert "docker.io/openfantasy/yadgar-backend:custom-tag" in pulled_images


def test_pull_reports_backend_pull_failure(podman_env):
    """A failed backend pull must surface as ok=False, not a silent partial success."""
    from yadgar.core.daemon import YadgarDaemon

    calls: list[list[str]] = []

    def fake_run(argv, *args, **kwargs):
        argv = list(argv)
        calls.append(argv)
        if len(argv) > 2 and "yadgar-backend" in argv[2]:
            return subprocess.CompletedProcess(argv, 1, "", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    with patch("subprocess.run", fake_run):
        result = YadgarDaemon().pull()

    assert result["ok"] is False
    assert any(len(c) > 2 and "yadgar-backend" in c[2] for c in calls), (
        "backend pull was never attempted"
    )


# ── `yadgar upgrade` image pull (yadgar/core/update/orchestrator.py) ──────────
#
# task:0101 — the SAME two defects as task:0099/task:0083, on the upgrade path.
# ``_default_image_pull`` pulled ONLY the core image, and did so with a literal
# ``"podman"`` argv head. So ``yadgar upgrade`` installed a fresh core image
# against whatever backend image happened to already be on disk (core and
# backend version independently — core 5.170.x / backend 5.60.x — and the daemon
# needs both), and it crashed outright on a docker-only host.
#
# It survived because the source-shape guard below used to be scoped to
# ``yadgar/core/daemon/`` + ``core/cli/daemon.py``; ``core/update/`` was never
# looked at. That scoping gap is fixed in the same change.


def test_upgrade_image_pull_uses_detected_runtime(docker_env):
    """RED before the fix: hardcoded "podman" argv head — a docker-only host dies here.

    The runtime is pinned to *docker* on purpose: pinning it to podman would let
    the hardcoded literal pass by coincidence.
    """
    from yadgar.core.update.orchestrator import _default_image_pull

    rec = _RunRecorder(returncode=0)
    with patch("subprocess.run", rec):
        _default_image_pull("9.9.9")

    assert "podman" not in rec.binaries, (
        f"literal 'podman' argv head — use the detected runtime: {rec.calls!r}"
    )
    assert rec.binaries and set(rec.binaries) == {"docker"}, (
        f"upgrade pull did not use the detected runtime: {rec.calls!r}"
    )


def test_upgrade_image_pull_pulls_both_core_and_backend_images(podman_env):
    """`yadgar upgrade` must fetch BOTH images, not just the core one.

    The backend tag is resolved exactly the way ``YadgarDaemon.pull()`` /
    ``start_backend()`` resolve it, so upgrade and start can never disagree.
    """
    from yadgar.core.daemon.runtime import DOCKERHUB_BACKEND_IMAGE
    from yadgar.core.update.orchestrator import _default_image_pull

    rec = _RunRecorder(returncode=0)
    with patch("subprocess.run", rec):
        _default_image_pull("9.9.9")

    pulled = {c[2] for c in rec.calls if len(c) > 2 and c[1] == "pull"}
    assert "docker.io/openfantasy/yadgar:9.9.9" in pulled, f"core image not pulled: {pulled!r}"
    assert DOCKERHUB_BACKEND_IMAGE in pulled, (
        "backend image not pulled — `yadgar upgrade` would install a fresh core "
        f"image against a stale backend image with no warning: {pulled!r}"
    )


def test_upgrade_image_pull_resolves_backend_image_via_env_override(podman_env, monkeypatch):
    """The upgrade path must honor YADGAR_BACKEND_IMAGE exactly like the daemon does."""
    from yadgar.core.update.orchestrator import _default_image_pull

    monkeypatch.setenv("YADGAR_BACKEND_IMAGE", "docker.io/openfantasy/yadgar-backend:custom-tag")
    rec = _RunRecorder(returncode=0)
    with patch("subprocess.run", rec):
        _default_image_pull("9.9.9")

    pulled = {c[2] for c in rec.calls if len(c) > 2 and c[1] == "pull"}
    assert "docker.io/openfantasy/yadgar-backend:custom-tag" in pulled, (
        f"YADGAR_BACKEND_IMAGE override ignored: {pulled!r}"
    )


def test_upgrade_image_pull_raises_when_backend_pull_fails(podman_env):
    """A failed backend pull must abort the upgrade (→ rollback), not pass silently."""
    from yadgar.core.update.orchestrator import _default_image_pull

    def fake_run(argv, *args, **kwargs):
        argv = list(argv)
        rc = 1 if any("yadgar-backend" in tok for tok in argv) else 0
        if kwargs.get("check") and rc != 0:
            raise subprocess.CalledProcessError(rc, argv)
        return subprocess.CompletedProcess(argv, rc, "", "")

    with patch("subprocess.run", fake_run), pytest.raises(subprocess.CalledProcessError):
        _default_image_pull("9.9.9")


def test_push_uses_detected_runtime(podman_env):
    from yadgar.core.daemon import YadgarDaemon

    rec = _RunRecorder(returncode=0)
    with patch("subprocess.run", rec):
        result = YadgarDaemon().push(tag="t")

    assert result["ok"] is True
    _no_docker(rec)
    assert [c for c in rec.calls if c[1] == "tag"]
    assert [c for c in rec.calls if c[1] == "push"]


def test_build_uses_detected_runtime(podman_env):
    from yadgar.core.daemon import YadgarDaemon

    rec = _RunRecorder(returncode=0)
    with patch("subprocess.run", rec):
        result = YadgarDaemon().build()

    assert result["ok"] is True
    _no_docker(rec)
    assert rec.calls[-1][:2] == ["podman", "build"]


def test_build_backend_uses_detected_runtime(podman_env):
    from yadgar.core.daemon import YadgarDaemon

    rec = _RunRecorder(returncode=0)
    with patch("subprocess.run", rec):
        result = YadgarDaemon().build(backend=True)

    assert result["ok"] is True
    _no_docker(rec)
    assert rec.calls[-1][:2] == ["podman", "build"]


def test_build_no_cache_keeps_flag_after_subcommand(podman_env):
    """`--no-cache` is inserted at index 2 — that position must stay valid."""
    from yadgar.core.daemon import YadgarDaemon

    rec = _RunRecorder(returncode=0)
    with patch("subprocess.run", rec):
        YadgarDaemon().build(no_cache=True)

    argv = rec.calls[-1]
    assert argv[:3] == ["podman", "build", "--no-cache"]


def test_exec_in_container_uses_detected_runtime(podman_env):
    from yadgar.core.daemon import YadgarDaemon

    rec = _RunRecorder(returncode=0, stdout="true\n")
    with patch("subprocess.run", rec):
        rc = YadgarDaemon().exec_in_container(["pytest"])

    assert rc == 0
    _no_docker(rec)
    assert rec.calls[-1][:2] == ["podman", "exec"]


def test_image_exists_uses_detected_runtime(podman_env):
    from yadgar.core.daemon import YadgarDaemon

    rec = _RunRecorder(returncode=0)
    with patch("subprocess.run", rec):
        assert YadgarDaemon()._image_exists("img") is True

    _no_docker(rec)
    assert rec.calls[-1][:3] == ["podman", "image", "inspect"]


def test_ensure_network_uses_detected_runtime(podman_env):
    from yadgar.core.daemon import _ensure_network

    rec = _RunRecorder(returncode=1)
    with patch("subprocess.run", rec):
        _ensure_network()

    _no_docker(rec)
    assert rec.calls[0][:3] == ["podman", "network", "inspect"]
    assert rec.calls[-1][:3] == ["podman", "network", "create"]


def test_start_container_exit_logs_use_detected_runtime(podman_env, monkeypatch):
    """The `logs --tail 20` diagnostic on a crashed container must not shell out to docker."""
    from yadgar.core.daemon import YadgarDaemon
    from yadgar.core.daemon import daemon as daemon_mod

    monkeypatch.setattr(daemon_mod, "_ensure_network", lambda: None)
    monkeypatch.setattr(daemon_mod.time, "sleep", lambda _s: None)

    d = YadgarDaemon()
    rec = _RunRecorder(returncode=0, stdout="boom\n")

    running = iter([False, True, False])  # core absent, backend up, then core exits
    monkeypatch.setattr(d, "_container_running", lambda _n: next(running, False))
    monkeypatch.setattr(d, "_image_exists", lambda _i: True)

    with patch("subprocess.run", rec):
        result = d.start()

    assert result["status"] == "failed"
    assert "container exited" in result["reason"]
    _no_docker(rec)
    assert [c for c in rec.calls if c[:2] == ["podman", "logs"]]


# ── CLI graceful-stop (yadgar/core/cli/daemon.py) ─────────────────────────────


def test_graceful_stop_uses_detected_runtime(podman_env):
    """`yadgar daemon graceful-stop` shells out to the runtime binary directly."""
    from types import SimpleNamespace

    from yadgar.core.cli.daemon import _handle_graceful_stop
    from yadgar.core.daemon import YadgarDaemon

    rec = _RunRecorder(returncode=0, stdout="true\n")
    args = SimpleNamespace(timeout=7)

    with patch("subprocess.run", rec), pytest.raises(SystemExit) as exc:
        _handle_graceful_stop(args, YadgarDaemon, dev=False, port=8765)

    assert exc.value.code == 0
    _no_docker(rec)
    assert [c for c in rec.calls if c[:2] == ["podman", "inspect"]]
    assert [c for c in rec.calls if c[:3] == ["podman", "stop", "--time=7"]]


def test_graceful_stop_on_podman_only_host_never_invokes_docker(podman_only):
    """RED before the fix: `docker inspect` raised FileNotFoundError on a podman-only host."""
    from types import SimpleNamespace

    from yadgar.core.cli.daemon import _handle_graceful_stop
    from yadgar.core.daemon import YadgarDaemon

    rec = _RunRecorder(returncode=0, stdout="true\n", missing=("docker",))
    args = SimpleNamespace(timeout=30)

    with patch("subprocess.run", rec), pytest.raises(SystemExit) as exc:
        _handle_graceful_stop(args, YadgarDaemon, dev=False, port=8765)

    assert exc.value.code == 0
    _no_docker(rec)
    assert "podman" in rec.binaries


# ── generated systemd units (yadgar/core/daemon/systemd.py) ───────────────────
#
# task:0104 — the same defect one layer out: not the argv Yadgar runs, but the
# argv it WRITES INTO A UNIT FILE for systemd to run later. A hardcoded
# ``ExecStart=docker run`` is exactly as fatal on a podman-only host as a
# hardcoded ``["docker", …]``; it just fails at ``systemctl --user start``
# instead of at ``yadgar daemon start``.

# A systemd ``Exec*=`` directive whose command is a literal runtime binary.
# The optional leading ``-`` is systemd's "failure is non-fatal" prefix.
_UNIT_EXEC_RUNTIME = re.compile(r"^\s*Exec[A-Za-z]*\s*=\s*-?\s*(docker|podman)\b", re.IGNORECASE)

# A systemd dependency directive naming a container-runtime unit.
_UNIT_DEP_RUNTIME = re.compile(
    r"^\s*(Requires|Requisite|Wants|BindsTo|PartOf|After|Before)\s*=.*"
    r"\b(docker|podman)\.(service|socket|target)\b",
    re.IGNORECASE,
)


# task:0105 — a runtime-conditional line-prefix marker in a ``.in`` template
# (``@PODMAN_ONLY@ExecStart=…``, which ``generate_systemd.sh`` either strips or
# uses to delete the whole line). The marker sits BEFORE the directive name, so
# both detectors above — which are anchored with ``^\s*`` — read a marked-up
# ``ExecStart=docker run`` as an ordinary text line and report clean. Stripping
# it first keeps the new template syntax from becoming a channel for exactly the
# literal this guard exists to catch.
_UNIT_LINE_MARKER = re.compile(r"^\s*@[A-Z_]+@")


def _strip_unit_markers(line: str) -> str:
    return _UNIT_LINE_MARKER.sub("", line)


def _is_offending_unit_line(line: str) -> bool:
    stripped = _strip_unit_markers(line)
    return bool(_UNIT_EXEC_RUNTIME.search(stripped) or _UNIT_DEP_RUNTIME.search(stripped))


def _offending_unit_lines(text: str) -> list[str]:
    """Return every line of *text* that names a container runtime as a unit directive.

    Deliberately NOT a substring search for "docker": a generated unit legitimately
    contains ``docker.io/openfantasy/yadgar`` image refs on its ``ExecStart``
    continuation lines, and those are registry hostnames, not the runtime binary.
    """
    return [ln.strip() for ln in text.splitlines() if _is_offending_unit_line(ln)]


def _render_units(tmp_path, monkeypatch) -> dict[str, str]:
    """Render both user units into *tmp_path* and return their text.

    HOME is redirected so nothing touches the real ``~/.config/systemd/user`` —
    unit generation is a pure text-rendering concern here, no systemctl involved.
    """
    from yadgar.core.daemon import systemd as systemd_mod
    from yadgar.core.daemon.profiles import _prod_profile

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("YADGAR_VOLUME", "yadgar-data")
    result = systemd_mod.install_systemd_service(_prod_profile(8765), dev=False)
    return {
        "backend": Path(result["backend_service"]).read_text(),
        "core": Path(result["core_service"]).read_text(),
    }


def test_generated_units_never_name_docker_on_a_podman_host(podman_env, tmp_path, monkeypatch):
    """RED before the fix: 13 hardcoded ``docker`` sites across the two units.

    This is the headline reproduction for task:0104 — the generated units are
    the entire ``install-systemd`` deliverable, and on a podman-only host every
    one of these lines names a binary that is not installed.
    """
    units = _render_units(tmp_path, monkeypatch)

    for name, text in units.items():
        offenders = [ln for ln in _offending_unit_lines(text) if "docker" in ln.lower()]
        assert offenders == [], (
            f"{name} unit names docker literally — a podman-only host cannot run it: {offenders}"
        )
        # The RENDERED unit must name podman — only the SOURCE template must not
        # name any runtime literally, which is the unit-directive guard's job.
        assert "podman run" in text, f"{name} unit does not invoke the resolved runtime"


def test_generated_units_never_name_podman_on_a_docker_host(docker_env, tmp_path, monkeypatch):
    """The inverse arm — a blind ``docker`` → ``podman`` swap must not pass.

    Pinning podman alone cannot distinguish a resolved runtime from a hardcoded
    ``podman`` literal (task:0101's lesson). Only this docker-pinned arm proves
    the unit text is actually derived from ``_get_runtime()``.
    """
    units = _render_units(tmp_path, monkeypatch)

    for name, text in units.items():
        offenders = [ln for ln in _offending_unit_lines(text) if "podman" in ln.lower()]
        assert offenders == [], (
            f"{name} unit names podman literally — a docker-only host cannot run it: {offenders}"
        )
        assert "docker run" in text, f"{name} unit does not invoke the resolved runtime"


def test_generated_units_declare_no_container_runtime_daemon_dependency(
    podman_env, tmp_path, monkeypatch
):
    """``Requires=docker.service`` has no correct per-runtime substitute — drop it.

    The shipped ``scripts/install/*.in`` templates — the generator that actually
    installs on real hosts — carry NO runtime-daemon dependency: the core unit
    depends only on ``yadgar-backend.service`` and the backend only on
    ``network.target``. The Python generator diverging from that is the defect.
    Rootless podman has no daemon to depend on at all, and ``podman.socket``
    serves only the Docker-compat API these units never use, so there is nothing
    to substitute. ``Restart=on-failure`` covers a not-yet-ready runtime either way.
    """
    units = _render_units(tmp_path, monkeypatch)

    for name, text in units.items():
        deps = [ln for ln in text.splitlines() if _UNIT_DEP_RUNTIME.search(ln)]
        assert deps == [], f"{name} unit depends on a container-runtime unit: {deps}"

    # The dependency that IS real must survive: core must not start before
    # backend.  "Real" means ORDERING, not lifecycle coupling (task:0111 /
    # ADR-0188) — see test_core_unit_wants_backend_not_requires below for why
    # the pull-in is Wants= rather than Requires=.
    core = units["core"]
    assert "Wants=yadgar-backend.service" in core, "core lost its backend dependency"
    after = [ln for ln in core.splitlines() if ln.startswith("After=")]
    assert any("yadgar-backend.service" in ln for ln in after), (
        f"core lost its backend ordering: {after}"
    )


def test_core_unit_wants_backend_not_requires(podman_env, tmp_path, monkeypatch):
    """task:0111 / ADR-0188 — the core must not be STOPPED with the backend.

    ``Requires=`` propagates stop: ``systemctl --user stop yadgar-backend``
    takes ``yadgar`` down with it, whoever asked.  That is the entire reason a
    vacuum (which must stop the backend to quiesce the store) took the whole
    memory engine down for ~68 s and dropped every connected MCP session.

    ``Wants=`` keeps the pull-in (starting core still starts the backend) and
    ``After=`` keeps boot ordering; only the stop propagation is dropped, which
    is exactly the behaviour wanted.  The private nix module was decoupled this
    way in v5.3.9; the in-repo generators never were.

    ``After=`` is asserted here too: flipping the dependency must NOT change
    START ordering, which ``After=`` plus the ADR-0185 readiness shape provide.
    """
    core = _render_units(tmp_path, monkeypatch)["core"]

    assert "yadgar-backend" in core, (
        "core unit names no backend dependency at all — this test would pass "
        "vacuously on a unit that lost the relationship entirely"
    )
    requires = [ln for ln in core.splitlines() if ln.startswith("Requires=")]
    assert not [ln for ln in requires if "yadgar-backend" in ln], (
        f"core still Requires= the backend — stopping the backend stops the core: {requires}"
    )
    assert "Wants=yadgar-backend.service" in core, (
        f"core lost the backend pull-in; it must Wants= the backend:\n{core}"
    )
    after = [ln for ln in core.splitlines() if ln.startswith("After=")]
    assert any("yadgar-backend.service" in ln for ln in after), (
        f"core lost its backend START ordering — Wants= alone does not order: {after}"
    )


# ── crash is surfaced, not swallowed (acceptance criterion 5) ─────────────────


def test_start_propagates_missing_runtime_instead_of_reporting_success(monkeypatch):
    """A missing runtime binary must NOT be converted into a success status."""
    from yadgar.core.daemon import YadgarDaemon

    monkeypatch.setenv("YADGAR_CONTAINER_RUNTIME", "definitely-not-installed")
    rec = _RunRecorder(missing=("definitely-not-installed",))

    with patch("subprocess.run", rec), pytest.raises(FileNotFoundError):
        YadgarDaemon().start()


# ── source-shape regression guard (acceptance criterion 4) ────────────────────


_ALLOWLIST_NAME = ".container-runtime-allowlist.json"
_MIN_RATIONALE = 40

# Literal argv heads that mean "a container runtime binary". BOTH names, not just
# "docker": task:0101's upgrade-path defect was a hardcoded ``"podman"``, which a
# docker-head-only detector reads as clean.
_RUNTIME_BINARIES = frozenset({"docker", "podman"})


def _repo_root() -> Path:
    """Repo root, derived from THIS file — the tests/_meta convention.

    Deliberately not ``Path(yadgar.__file__).parent.parent``: under a
    wheel-installed run that resolves to site-packages, where neither the
    ``scripts/`` tree nor the allowlist exists. A source-shape guard must read
    the source tree it lives in.
    """
    return Path(__file__).resolve().parents[3]


def _runtime_guarded_sources() -> list[Path]:
    """Every non-test source module in the repo — the guard's scope.

    task:0101 WIDENED this from "``yadgar/core/daemon/`` + ``core/cli/daemon.py``"
    to the whole of ``yadgar/`` + ``scripts/``. The old narrow scope is exactly
    why ``core/update/orchestrator.py`` could ship a hardcoded ``"podman"`` pull:
    the guard had never looked at that file. A narrow scope on an anti-recurrence
    guard only relocates the recurrence.

    Breadth is affordable because the detector keys on an argv-list HEAD, not on
    the substring "docker": the ``docker …`` hint strings that ``core/cli/setup.py``
    and friends legitimately print are never argv heads, and image refs such as
    ``"docker.io/openfantasy/yadgar"`` fail the exact-equality test. Deliberate
    sites (runtime detection, the dual-probe in ``scripts/check_image_size.py``)
    are governed by ``.container-runtime-allowlist.json`` rather than by narrowing
    the scope back down.

    Tests are excluded: a test asserting on ``["podman", "pull", …]`` argv is the
    guard's own subject matter, not a violation.
    """
    root = _repo_root()
    pkg_dir = root / "yadgar"
    assert pkg_dir.is_dir(), f"guard target moved: {pkg_dir}"

    paths = [p for p in pkg_dir.rglob("*.py") if "tests" not in p.relative_to(root).parts]
    scripts_dir = root / "scripts"
    if scripts_dir.is_dir():  # absent in a wheel-installed checkout
        paths.extend(scripts_dir.rglob("*.py"))
    return sorted(paths)


def _literal_runtime_argv_heads(path: Path, *, root: Path | None = None) -> list[str]:
    """Return ``path:line`` for every argv LIST in *path* headed by a runtime binary.

    ``ast.List`` only — an ``ast.Tuple`` headed by "docker"/"podman" is never an
    argv (``subprocess`` is always handed a list here); the two tuples in the repo
    are a detection-candidate loop and a membership test in the PreToolUse router.
    Keyed on every list node rather than only inline ``subprocess.run`` arguments,
    because ``build()`` / ``exec_in_container()`` bind their argv to a local first.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    label = str(path.relative_to(root)) if root else path.name
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.List) or not node.elts:
            continue
        head = node.elts[0]
        if isinstance(head, ast.Constant) and head.value in _RUNTIME_BINARIES:
            hits.append(f"{label}:{node.lineno}")
    return hits


def _load_runtime_allowlist() -> dict:
    raw = json.loads((_repo_root() / _ALLOWLIST_NAME).read_text(encoding="utf-8"))
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def _all_literal_runtime_sites() -> list[str]:
    root = _repo_root()
    offenders: list[str] = []
    for path in _runtime_guarded_sources():
        offenders.extend(_literal_runtime_argv_heads(path, root=root))
    return sorted(offenders)


def test_no_literal_runtime_argv_head_outside_the_allowlist():
    """Fail if any argv list anywhere starts with a literal "docker" / "podman".

    Both binaries are checked. task:0083 was a hardcoded ``"docker"`` crashing
    podman-only hosts; task:0101 was the mirror image — a hardcoded ``"podman"``
    on the ``yadgar upgrade`` path, which crashes docker-only hosts. A detector
    that knows only one of the two names is half a guard.
    """
    allowlist = _load_runtime_allowlist()
    offenders = [s for s in _all_literal_runtime_sites() if s not in allowlist]

    assert offenders == [], (
        "literal container-runtime argv head(s) found — resolve the binary via "
        "_get_runtime() (or allowlist with a written rationale): " + ", ".join(offenders)
    )


def test_runtime_allowlist_entries_are_governed_and_not_stale():
    """Every allowlist entry needs a real rationale and must still be a live site.

    Mirrors ``.route-literal-allowlist.json`` governance: rationale >= 40 chars,
    and a STALE entry (a site that no longer has a literal runtime head — moved,
    deleted, or fixed) is a HARD FAILURE, so the allowlist cannot rot into a
    permanent silence.
    """
    allowlist = _load_runtime_allowlist()
    live = set(_all_literal_runtime_sites()) | set(_all_literal_runtime_unit_directives())

    problems: list[str] = []
    for site, meta in sorted(allowlist.items()):
        rationale = (meta or {}).get("rationale", "")
        if len(rationale) < _MIN_RATIONALE:
            problems.append(f"MALFORMED {site}: rationale must be >= {_MIN_RATIONALE} chars")
        if site not in live:
            problems.append(f"STALE {site}: no literal runtime argv head there any more — drop it")

    assert problems == [], "; ".join(problems)


def test_guard_scope_reaches_beyond_the_daemon_package():
    """The guard's file set must include the modules the OLD narrow scope missed.

    ``core/update/orchestrator.py`` shipped a hardcoded ``"podman"`` pull for
    exactly as long as this guard was scoped to ``yadgar/core/daemon/``. Pinning
    the widened set means a future re-narrowing is a test failure, not a silent
    coverage hole.
    """
    root = _repo_root()
    guarded = {str(p.relative_to(root)) for p in _runtime_guarded_sources()}

    for required in (
        "yadgar/core/daemon/profiles.py",  # the original narrow scope
        "yadgar/core/cli/daemon.py",  # added by task:0083
        "yadgar/core/update/orchestrator.py",  # missed until task:0101
        "yadgar/core/ops/ops.py",
        "scripts/check_image_size.py",
    ):
        assert required in guarded, f"{required} missing from guard set"

    assert not any("/tests/" in p for p in guarded), "test modules must stay out of the guard set"


def test_guard_detects_a_regression_at_the_graceful_stop_sites(tmp_path):
    """The guard's detector must flag the exact shape that shipped in _handle_graceful_stop."""
    regressed = tmp_path / "daemon.py"
    regressed.write_text(
        "import subprocess\n"
        "def _handle_graceful_stop(container, timeout):\n"
        '    subprocess.run(["docker", "inspect", "--format", "{{.State.Running}}", container])\n'
        '    subprocess.run(["docker", "stop", f"--time={timeout}", container])\n',
        encoding="utf-8",
    )

    assert _literal_runtime_argv_heads(regressed) == ["daemon.py:3", "daemon.py:4"]


def test_guard_detects_a_hardcoded_podman_argv_head(tmp_path):
    """The exact shape that shipped in ``_default_image_pull`` (task:0101)."""
    regressed = tmp_path / "orchestrator.py"
    regressed.write_text(
        "import subprocess\n"
        "def _default_image_pull(version):\n"
        '    subprocess.run(["podman", "pull", f"docker.io/openfantasy/yadgar:{version}"])\n',
        encoding="utf-8",
    )

    assert _literal_runtime_argv_heads(regressed) == ["orchestrator.py:3"]


# ── unit-directive regression guard (task:0104) ───────────────────────────────
#
# The argv-head detector above is ``ast.List``-shaped, so it is structurally
# blind to a runtime binary baked into an f-string that RENDERS a unit file —
# which is precisely how task:0104 shipped 13 hardcoded ``docker`` sites inside
# ``systemd.py`` while that file was already inside the widened guard scope.
# This second detector reads the emitted TEXT instead of the AST.

# Every file in the repo that can carry a systemd/launchd unit directive:
# both live unit generators plus the checked-in units and shell installers.
# Scoping this to ``.py`` only would leave the ``.in`` templates — a second
# real install path, proven live by the cross-generator tests — unguarded.
_UNIT_TEXT_GLOBS = (
    "scripts/install/*.in",
    "scripts/install/launchd/*.in",
    "scripts/systemd-user/*",
    "deploy/systemd/*",
    "yadgar/core/systemd/*",
    "scripts/**/*.sh",
)


def _unit_directive_guarded_files() -> list[Path]:
    """Python sources (the guard's existing scope) + every unit file/template."""
    root = _repo_root()
    paths = list(_runtime_guarded_sources())
    for pattern in _UNIT_TEXT_GLOBS:
        paths.extend(p for p in root.glob(pattern) if p.is_file())
    return sorted(set(paths))


def _literal_runtime_unit_directives(path: Path, *, root: Path | None = None) -> list[str]:
    """Return ``path:line`` for every unit directive in *path* naming a runtime binary.

    A raw line scan rather than an AST walk: systemd directives are line-anchored
    in the source exactly as they are in the rendered unit, so the reported line
    is the real one. Reading f-string chunks out of the AST instead yields the
    lineno of the chunk's START, which lands one line early on every directive
    that follows an interpolation — useless for an allowlist key.
    """
    label = str(path.relative_to(root)) if root else path.name
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        return []
    return [f"{label}:{i}" for i, ln in enumerate(lines, 1) if _is_offending_unit_line(ln)]


def _all_literal_runtime_unit_directives() -> list[str]:
    root = _repo_root()
    offenders: list[str] = []
    for path in _unit_directive_guarded_files():
        offenders.extend(_literal_runtime_unit_directives(path, root=root))
    return sorted(offenders)


def test_no_literal_runtime_in_generated_unit_directives():
    """Fail if any generated or checked-in unit directive names a runtime literally.

    Covers both shapes that shipped in task:0104: an ``Exec*=`` command headed by
    ``docker``/``podman``, and a ``Requires=``/``After=`` on ``docker.service``.
    Unit generators must interpolate the resolved runtime (``_get_runtime()`` in
    Python, ``@RUNTIME@`` / ``@YADGAR_RUNTIME@`` in the shell templates).
    """
    allowlist = _load_runtime_allowlist()
    offenders = [s for s in _all_literal_runtime_unit_directives() if s not in allowlist]

    assert offenders == [], (
        "literal container-runtime name(s) in systemd unit directives — interpolate "
        "the resolved runtime instead: " + ", ".join(offenders)
    )


def test_unit_directive_guard_scope_covers_both_unit_generators():
    """Both live unit generators must be in the file set, not just the Python one.

    task:0101's post-mortem was that a narrow guard scope only relocates the
    recurrence. The scope must reach wherever unit TEXT is produced, on every
    surface the installer writes.

    task:0110 Stage D moved the shell installer's unit text: ``.in`` templates are
    gone and ``scripts/install/generate_systemd.sh`` renders nothing, so the two
    template entries below are replaced by the Python builders that took their
    place. This is a REPLACEMENT, not a narrowing — those modules are already
    inside ``_runtime_guarded_sources``' ``yadgar/`` scope, and naming them here
    is what stops a future edit from quietly dropping them the way this test
    exists to prevent.
    """
    root = _repo_root()
    guarded = {str(p.relative_to(root)) for p in _unit_directive_guarded_files()}

    for required in (
        "yadgar/core/daemon/systemd.py",  # the task:0104 site
        "yadgar/core/daemon/units.py",  # core + backend unit builders (task:0110)
        "yadgar/core/daemon/maintenance_units.py",  # target/timers/path builders
        "scripts/install/launchd/com.openfantasy.yadgar.plist.in",  # macOS core
        "yadgar/core/systemd/yadgar.service",  # checked-in unit
    ):
        assert required in guarded, f"{required} missing from unit-directive guard set"


def test_unit_directive_guard_detects_the_systemd_py_regression(tmp_path):
    """The detector must flag the exact shape that shipped in ``install_systemd_service``."""
    regressed = tmp_path / "systemd.py"
    regressed.write_text(
        "def render(name):\n"
        '    return f"""\\\n'
        "[Unit]\n"
        "Requires=docker.service\n"
        "After=docker.service\n"
        "\n"
        "[Service]\n"
        "ExecStartPre=-docker rm {name}\n"
        "ExecStart=docker run --rm docker.io/openfantasy/yadgar\n"
        "ExecStop=docker stop {name}\n"
        '"""\n',
        encoding="utf-8",
    )

    assert _literal_runtime_unit_directives(regressed) == [
        "systemd.py:4",
        "systemd.py:5",
        "systemd.py:8",
        "systemd.py:9",
        "systemd.py:10",
    ]


def test_unit_directive_guard_detects_a_hardcoded_podman_unit(tmp_path):
    """Both names, not just docker — a podman-literal unit is dead on a docker host."""
    regressed = tmp_path / "yadgar.service.in"
    regressed.write_text(
        "[Unit]\nAfter=podman.socket\n\n[Service]\nExecStart=podman run --rm @IMAGE@\n",
        encoding="utf-8",
    )

    assert _literal_runtime_unit_directives(regressed) == [
        "yadgar.service.in:2",
        "yadgar.service.in:5",
    ]


def test_unit_directive_guard_sees_through_runtime_conditional_markers(tmp_path):
    """task:0105 — a marker-prefixed directive must NOT become a guard blind spot.

    The runtime-conditional readiness work introduces line-prefix markers in the
    ``.in`` templates (``@PODMAN_ONLY@`` / ``@DOCKER_ONLY@``), which
    ``generate_systemd.sh`` either strips or uses to delete the whole line. The
    marker sits BEFORE the directive name, so ``@DOCKER_ONLY@ExecStart=docker …``
    does not match ``^\\s*Exec[A-Za-z]*=`` and the task:0104 detector reads it as
    clean — a fresh channel for exactly the literal this guard exists to catch.

    RED before the detector strips markers: returns ``[]``.
    """
    regressed = tmp_path / "yadgar.service.in"
    regressed.write_text(
        "[Unit]\n"
        "@DOCKER_ONLY@After=docker.service\n"
        "\n"
        "[Service]\n"
        "Type=@SERVICE_TYPE@\n"
        "@PODMAN_ONLY@ExecStartPre=-podman rm yadgar\n"
        "@DOCKER_ONLY@ExecStart=docker run --rm @IMAGE@\n"
        "ExecStop=@RUNTIME@ stop yadgar\n",
        encoding="utf-8",
    )

    assert _literal_runtime_unit_directives(regressed) == [
        "yadgar.service.in:2",
        "yadgar.service.in:6",
        "yadgar.service.in:7",
    ]


def test_unit_directive_guard_ignores_placeholders_and_image_refs(tmp_path):
    """A resolved-runtime unit is clean, image refs and prose notwithstanding."""
    clean = tmp_path / "yadgar.service.in"
    clean.write_text(
        "[Unit]\n"
        "Description=Yadgar (Docker)\n"
        "After=network.target yadgar-backend.service\n"
        "Requires=yadgar-backend.service\n"
        "\n"
        "[Service]\n"
        "Environment=DOCKER_HOST=unix:///run/podman/podman.sock\n"
        "ExecStartPre=-@RUNTIME@ rm yadgar\n"
        "ExecStart=@RUNTIME@ run --rm docker.io/openfantasy/yadgar\n"
        "ExecStop=@RUNTIME@ stop yadgar\n",
        encoding="utf-8",
    )

    assert _literal_runtime_unit_directives(clean) == []


def test_guard_ignores_image_refs_and_runtime_detection_tuples(tmp_path):
    """Exact-equality on the head keeps image refs and candidate TUPLES out."""
    benign = tmp_path / "benign.py"
    benign.write_text(
        'IMAGES = ["docker.io/openfantasy/yadgar", "docker.io/openfantasy/yadgar-backend"]\n'
        'for rt in ("podman", "docker"):\n'
        "    pass\n",
        encoding="utf-8",
    )

    assert _literal_runtime_argv_heads(benign) == []
