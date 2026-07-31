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

These tests pin two things:
  1. behaviour — every subprocess invocation resolves the binary via
     ``_get_runtime()`` (daemon, CLI graceful-stop, and the upgrade orchestrator);
  2. source shape — an AST guard that fails if a literal ``"docker"`` / ``"podman"``
     argv head appears anywhere in ``yadgar/`` or ``scripts/`` unallowlisted.
"""

from __future__ import annotations

import ast
import json
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
    live = set(_all_literal_runtime_sites())

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
