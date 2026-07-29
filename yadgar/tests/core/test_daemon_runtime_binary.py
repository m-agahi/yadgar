"""task:0083 — the daemon must invoke the DETECTED container runtime, never literal "docker".

Empirical bug (2026-07-29, fresh Debian 13 VM, yadgar 5.168.0, podman 5.4.2, no
docker binary): ``yadgar daemon start`` died with
``FileNotFoundError: [Errno 2] No such file or directory: 'docker'`` because
``daemon.py`` templated ``["docker", "rm", ...]`` even though ``check_runtime()``
had already resolved podman.

These tests pin two things:
  1. behaviour — every daemon subprocess invocation uses ``_get_runtime()``;
  2. source shape — an AST guard that fails if a literal ``"docker"`` argv head
     re-appears anywhere in ``yadgar/core/daemon/``.
"""

from __future__ import annotations

import ast
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


# ── crash is surfaced, not swallowed (acceptance criterion 5) ─────────────────


def test_start_propagates_missing_runtime_instead_of_reporting_success(monkeypatch):
    """A missing runtime binary must NOT be converted into a success status."""
    from yadgar.core.daemon import YadgarDaemon

    monkeypatch.setenv("YADGAR_CONTAINER_RUNTIME", "definitely-not-installed")
    rec = _RunRecorder(missing=("definitely-not-installed",))

    with patch("subprocess.run", rec), pytest.raises(FileNotFoundError):
        YadgarDaemon().start()


# ── source-shape regression guard (acceptance criterion 4) ────────────────────


def test_no_literal_docker_argv_head_in_daemon_package():
    """Fail if any argv list in yadgar/core/daemon/ starts with the literal "docker".

    Keyed on every ``ast.List`` node — not just the ones passed inline to
    ``subprocess.run`` — because ``build()`` / ``exec_in_container()`` bind the
    argv to a local first. Exact equality on the first element means image refs
    such as ``"docker.io/openfantasy/yadgar"`` and the ``["podman", "docker"]``
    detection candidate list are unaffected.
    """
    from yadgar.core.daemon import runtime as runtime_mod

    pkg_dir = Path(runtime_mod.__file__).resolve().parent
    offenders: list[str] = []

    for path in sorted(pkg_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.List) or not node.elts:
                continue
            head = node.elts[0]
            if isinstance(head, ast.Constant) and head.value == "docker":
                offenders.append(f"{path.name}:{node.lineno}")

    assert offenders == [], (
        "literal 'docker' argv head(s) found — use _get_runtime() instead: " + ", ".join(offenders)
    )
