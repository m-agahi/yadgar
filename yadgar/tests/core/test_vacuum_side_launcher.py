"""Side-build launcher seam — the container-only host must be able to vacuum (Car 0092).

v5.170.0 shipped only the PREFLIGHT half of Car 0092: on a container install
``yadgar vacuum`` stops cleanly instead of dying after ``/export`` + stopping both
units + a full-size ``copytree``.  That stopped the damage; it did not let a
container-only host vacuum at all — the nightly became a permanent, honest no-op.

The bug is that Phase 3's side build spawns a throwaway ``surreal start``
HOST-side (``yadgar/core/_surreal_runner`` — a bare PATH-resolved
``subprocess.Popen(["surreal", ...])``), and on a container install that binary
exists ONLY inside the ``yadgar-backend`` image (``Dockerfile.backend``:
``COPY --from=surrealdb/surrealdb:v3.1.5``).

The fix is a LAUNCHER SEAM: the host binary when a usable ``surreal`` is on PATH
(dev boxes, nix hosts), a ONE-SHOT BACKEND CONTAINER otherwise, and the v5.170.0
SKIP only when neither is available.

**The load-bearing property under test is the graceful-stop assertion.**  The
host path proves a clean exit with ``proc.wait(timeout=15.0)``; a SIGKILL'd
surrealkv dir is half-flushed and corrupt-on-reopen (ADR-0090), so the side build
RAISES rather than swap.  ``podman run`` has no ``Popen`` exit code, so the
container path must reproduce the same proof via
``stop --time 30`` → ``wait`` → ``inspect '{{.State.ExitCode}}'``, and MUST raise
on a non-zero code or a timed-out stop.  ``TestStopCleanIsTheSwapGate`` asserts
that a failed stop leaves the canonical untouched with no ``.old``/``.new``.

No containers are created anywhere in this file: every runtime invocation goes
through a fake ``podman`` shell script that records its argv, so the argv
contract (``--user root``, ``label=disable``, the ``$DATA_DIR``→``/data`` bind,
the loopback-only publish, the entrypoint override) is asserted for real.
"""

from __future__ import annotations

import os
import stat
import tempfile
import types as _types
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

from yadgar.core.vacuum import launcher as _launcher_mod

# ---------------------------------------------------------------------------
# Fake container runtime: a shell script that records argv and answers the five
# subcommands the launcher uses.  YADGAR_CONTAINER_RUNTIME points _get_runtime()
# straight at it, so no podman/docker is consulted and no container is created.
# ---------------------------------------------------------------------------

_FAKE_RUNTIME_SH = """#!/bin/sh
echo "$@" >> "{log}"
case "$1" in
  image)   exit {image_rc} ;;
  run)     echo fake-container-id; exit {run_rc} ;;
  stop)    exit {stop_rc} ;;
  wait)    echo {exit_code}; exit 0 ;;
  inspect) echo {inspect_out}; exit {inspect_rc} ;;
  logs)    echo 'surrealkv: failed to open store'; exit 0 ;;
  rm)      exit {rm_rc} ;;
  ps)      exit 0 ;;
  *)       exit 0 ;;
esac
"""


def _write_fake_runtime(  # noqa: PLR0913 — one knob per simulated runtime outcome
    dirpath: Path,
    *,
    image_rc: int = 0,
    run_rc: int = 0,
    stop_rc: int = 0,
    exit_code: int = 0,
    inspect_out: str | None = None,
    inspect_rc: int = 0,
    rm_rc: int = 0,
) -> tuple[Path, Path]:
    """Write the fake runtime; return (binary_path, argv_log_path)."""
    log = dirpath / "runtime-argv.log"
    binary = dirpath / "fake-podman"
    binary.write_text(
        _FAKE_RUNTIME_SH.format(
            log=log,
            image_rc=image_rc,
            run_rc=run_rc,
            stop_rc=stop_rc,
            exit_code=exit_code,
            inspect_out=exit_code if inspect_out is None else inspect_out,
            inspect_rc=inspect_rc,
            rm_rc=rm_rc,
        )
    )
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return binary, log


def _argv_lines(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


# ---------------------------------------------------------------------------
# Scaffolding (mirrors test_vacuum_preflight.py)
# ---------------------------------------------------------------------------


def _fake_db(td: str) -> Path:
    p = Path(td)
    db = p / "surreal_db"
    for sub in ("vlog", "sstables", "wal"):
        (db / sub).mkdir(parents=True)
    (db / "vlog" / "00001.vlog").write_bytes(b"x" * 1000)
    (db / "sentinel.txt").write_bytes(b"original")
    return db


def _vacuum_args(db: Path) -> _types.SimpleNamespace:
    return _types.SimpleNamespace(
        backend_url="http://127.0.0.1:8080",
        service_mode="manual",
        db_path=str(db),
        yes=True,
    )


_FAKE_SURQL = "-- TABLE DATA: memory ----\nUPSERT memory:1 CONTENT {};\n"


def _fake_get(url: str, **kwargs) -> MagicMock:
    m = MagicMock()
    m.status_code = 200
    m.text = _FAKE_SURQL if "/export" in url else ""
    return m


def _fake_post(url: str, **kwargs) -> MagicMock:
    m = MagicMock()
    m.status_code = 200
    m.text = "OK"
    m.json.return_value = {"ok": True}
    return m


class _Spies:
    """The steps a container-capable host must actually REACH (vs. a skip)."""

    def __init__(self) -> None:
        self.export = MagicMock(name="_vacuum_export")
        self.snapshot = MagicMock(name="_vacuum_snapshot_and_drop")

    def install(self, stack: ExitStack, td: str) -> None:
        real_export = __import__("yadgar.core.vacuum", fromlist=["_vacuum_export"])._vacuum_export
        real_snapshot = __import__(
            "yadgar.core.vacuum", fromlist=["_vacuum_snapshot_and_drop"]
        )._vacuum_snapshot_and_drop
        self.export.side_effect = real_export
        self.snapshot.side_effect = real_snapshot
        stack.enter_context(patch("yadgar.core.vacuum._vacuum_export", self.export))
        stack.enter_context(patch("yadgar.core.vacuum._vacuum_snapshot_and_drop", self.snapshot))


def _base_patches(stack: ExitStack) -> MagicMock:
    """Patch the service/health/HTTP seams; return the consolidation_log mock.

    ``_build_and_verify_side_db`` is deliberately NOT stubbed — the container
    launcher inside it is the thing under test.
    """
    row_log = stack.enter_context(patch("yadgar.core.vacuum._log_consolidation_row"))
    stack.enter_context(patch("yadgar.core.vacuum.ServiceController"))
    stack.enter_context(patch("yadgar.core.vacuum._wait_for_health", return_value=True))
    stack.enter_context(patch("yadgar.core.vacuum._wait_for_yadgar_health", return_value=True))
    stack.enter_context(patch("yadgar.core.vacuum._bootstrap_namespace"))
    stack.enter_context(patch("yadgar.core.vacuum._redefine_users_post_import"))
    stack.enter_context(
        patch("yadgar.core.vacuum._capture_table_counts", return_value={"memory": 1})
    )
    stack.enter_context(patch("yadgar.core.vacuum._assert_backend_quiesced", return_value=True))
    stack.enter_context(
        patch("yadgar.core.vacuum._verify_live_store_coherence", return_value=(True, set()))
    )
    return row_log


def _run_vacuum(monkeypatch, td: str, db: Path, extra=lambda _s: None):
    """Drive cmd_vacuum_impl inside *td*; return (exit_code, row_log, spies)."""
    monkeypatch.setattr(httpx, "get", _fake_get)
    monkeypatch.setattr(httpx, "post", _fake_post)
    monkeypatch.setenv("YADGAR_HOME", td)
    monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", "test-token")

    from yadgar.core.vacuum import cmd_vacuum_impl

    spies = _Spies()
    with ExitStack() as stack:
        row_log = _base_patches(stack)
        spies.install(stack, td)
        extra(stack)
        result = cmd_vacuum_impl(_vacuum_args(db))
    return result, row_log, spies


def _skip_reason_of(row_log: MagicMock) -> str | None:
    if not row_log.call_args_list:
        return None
    return row_log.call_args_list[-1].args[0].get("skip_reason")


def _container_only_host(monkeypatch, td: str, **runtime_kwargs) -> Path:
    """No host `surreal` resolvable at all; a container runtime that answers.

    Returns the log.  Task 0107: PATH alone no longer proves "no binary" —
    ``_resolve_surreal_binary`` also checks fixed candidate dirs including
    ``~/.local/bin/surreal``, so HOME must be pinned to the empty tmp dir too,
    or this helper silently stops being container-only on a workstation with a
    pipx-installed ``surreal`` (exactly the layout task 0107 is about).

    HOME is still not enough: three of the four candidates (``/usr/local/bin``,
    ``/opt/homebrew/bin``, ``/usr/bin``) are ABSOLUTE and no environment
    variable can neutralise them.  The CI image ships a real `surreal` in
    ``/usr/local/bin``, so this helper resolved one there, the host branch was
    taken, and "container-only" was a lie (PR #22).  The candidate LIST is
    therefore redirected to a single HOME-anchored entry, which the empty HOME
    above makes unresolvable — and NOT to ``()``, which would keep these tests
    green even if the candidate-dir loop were deleted outright.
    """
    empty_bin = Path(td) / "empty-bin"
    empty_bin.mkdir(exist_ok=True)
    monkeypatch.setenv("PATH", str(empty_bin))
    monkeypatch.setenv("HOME", td)
    monkeypatch.delenv("YADGAR_SURREAL_BIN", raising=False)
    monkeypatch.setattr(_launcher_mod, "_SURREAL_BIN_CANDIDATES", ("~/.local/bin/surreal",))
    binary, log = _write_fake_runtime(Path(td), **runtime_kwargs)
    monkeypatch.setenv("YADGAR_CONTAINER_RUNTIME", str(binary))
    monkeypatch.setenv("YADGAR_BACKEND_IMAGE", "docker.io/openfantasy/yadgar-backend:test")
    return log


# ---------------------------------------------------------------------------
# THE BUG: a container-only host must complete a vacuum, not skip forever.
# ---------------------------------------------------------------------------


class TestContainerOnlyHostCompletesVacuum:
    def test_no_host_surreal_but_backend_image_present_vacuums(self, monkeypatch):
        """The container-only install case — this is the whole point of the car.

        Before the launcher seam this returned 0 via the v5.170.0 SKIP with
        ``skip_reason='no_surreal_binary'`` and never exported, never stopped a
        unit and never swapped: an honest but permanent no-op.
        """
        with tempfile.TemporaryDirectory() as td:
            db = _fake_db(td)
            log = _container_only_host(monkeypatch, td)

            result, row_log, spies = _run_vacuum(monkeypatch, td, db)

            assert _skip_reason_of(row_log) is None, (
                "a host with the backend image must NOT skip — the side build can "
                "run in a one-shot container"
            )
            assert result == 0, f"container-path vacuum must succeed; got exit {result}"
            assert spies.export.called, "Phase 1 /export must run on a container-only host"
            assert spies.snapshot.called, "Phase 2 snapshot must run on a container-only host"

            argv = _argv_lines(log)
            assert any(line.startswith("run ") for line in argv), (
                f"the side build must have started a one-shot container; argv log:\n{argv}"
            )
            assert not (Path(td) / "surreal_db" / "sentinel.txt").exists(), (
                "the compacted side DB must have been swapped in for the canonical"
            )

    def test_neither_binary_nor_image_still_skips_with_its_named_reason(self, monkeypatch):
        """The v5.170.0 SKIP path survives, reason string intact (telemetry continuity)."""
        with tempfile.TemporaryDirectory() as td:
            db = _fake_db(td)
            _container_only_host(monkeypatch, td, image_rc=1)

            result, row_log, spies = _run_vacuum(monkeypatch, td, db)

            assert result == 0, "no launcher at all is a SKIP (exit 0), not a failure"
            assert _skip_reason_of(row_log) == "no_surreal_binary"
            assert not spies.export.called, "the skip must precede Phase 1 /export"
            assert not spies.snapshot.called, "the skip must precede Phase 2 stop+copytree"
            assert (Path(td) / "surreal_db" / "sentinel.txt").exists(), (
                "a skipped run must leave the canonical untouched"
            )

    def test_host_binary_path_is_not_regressed(self, monkeypatch):
        """A host WITH `surreal` keeps using the host process — no container runtime call."""
        from yadgar.core.vacuum.launcher import HostBinaryLauncher, select_side_launcher

        with tempfile.TemporaryDirectory() as td:
            fake_bin = Path(td) / "bin"
            fake_bin.mkdir()
            surreal = fake_bin / "surreal"
            surreal.write_text("#!/bin/sh\necho 3.1.5\n")
            surreal.chmod(surreal.stat().st_mode | stat.S_IEXEC)
            monkeypatch.setenv("PATH", str(fake_bin))
            _binary, log = _write_fake_runtime(Path(td))
            monkeypatch.setenv("YADGAR_CONTAINER_RUNTIME", str(_binary))

            launcher = select_side_launcher()

        assert isinstance(launcher, HostBinaryLauncher)
        assert not _argv_lines(log), (
            "a host with `surreal` on PATH must not consult the container runtime at all"
        )


# ---------------------------------------------------------------------------
# The graceful-stop proof: stop --time → wait → inspect ExitCode.
# ---------------------------------------------------------------------------


class TestContainerStopCleanProvesGracefulExit:
    def _launcher(self, monkeypatch, td: str, **runtime_kwargs):
        from yadgar.core.vacuum.launcher import ContainerLauncher

        binary, log = _write_fake_runtime(Path(td), **runtime_kwargs)
        monkeypatch.setenv("YADGAR_CONTAINER_RUNTIME", str(binary))
        return ContainerLauncher(), log

    def test_exit_code_zero_returns(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            launcher, log = self._launcher(monkeypatch, td, exit_code=0)
            launcher.stop_clean("http://127.0.0.1:12345")
            argv = _argv_lines(log)

        assert any(line.startswith("stop --time 30 ") for line in argv), (
            f"the stop must carry an explicit grace window; argv:\n{argv}"
        )
        assert any(line.startswith("wait ") for line in argv), (
            f"the exit must be WAITED for, not assumed; argv:\n{argv}"
        )
        assert any("State.ExitCode" in line for line in argv), (
            f"the exit CODE must be read — this is the proof of a graceful stop; argv:\n{argv}"
        )

    def test_sigkill_exit_code_raises(self, monkeypatch):
        """137 = killed after the grace window = a possibly half-flushed store."""
        import pytest

        with tempfile.TemporaryDirectory() as td:
            launcher, _log = self._launcher(monkeypatch, td, exit_code=137)
            with pytest.raises(RuntimeError, match="137"):
                launcher.stop_clean("http://127.0.0.1:12345")

    def test_failed_stop_command_raises(self, monkeypatch):
        import pytest

        with tempfile.TemporaryDirectory() as td:
            launcher, _log = self._launcher(monkeypatch, td, stop_rc=1)
            with pytest.raises(RuntimeError, match="stop"):
                launcher.stop_clean("http://127.0.0.1:12345")

    def test_unreadable_exit_code_raises(self, monkeypatch):
        """An inspect that will not answer is NOT evidence of a clean exit."""
        import pytest

        with tempfile.TemporaryDirectory() as td:
            launcher, _log = self._launcher(monkeypatch, td, inspect_rc=1)
            with pytest.raises(RuntimeError):
                launcher.stop_clean("http://127.0.0.1:12345")

    def test_unparsable_exit_code_raises(self, monkeypatch):
        import pytest

        with tempfile.TemporaryDirectory() as td:
            launcher, _log = self._launcher(monkeypatch, td, inspect_out="nonsense")
            with pytest.raises(RuntimeError):
                launcher.stop_clean("http://127.0.0.1:12345")

    def test_timed_out_stop_raises(self, monkeypatch):
        import subprocess

        import pytest

        from yadgar.core.vacuum import launcher as _launcher_mod

        with tempfile.TemporaryDirectory() as td:
            launcher, _log = self._launcher(monkeypatch, td)
            monkeypatch.setattr(
                _launcher_mod,
                "_run",
                MagicMock(side_effect=subprocess.TimeoutExpired(cmd="stop", timeout=60)),
            )
            with pytest.raises(RuntimeError, match="did not stop"):
                launcher.stop_clean("http://127.0.0.1:12345")


class TestStopCleanIsTheSwapGate:
    """A failed stop must leave the canonical untouched — the load-bearing property."""

    def _run_side_build(self, td: str, monkeypatch, *, exit_code: int):
        from yadgar.core.vacuum import _side_build_swap_and_start
        from yadgar.core.vacuum.launcher import ContainerLauncher

        binary, _log = _write_fake_runtime(Path(td), exit_code=exit_code)
        monkeypatch.setenv("YADGAR_CONTAINER_RUNTIME", str(binary))
        monkeypatch.setattr(httpx, "post", _fake_post)

        home = Path(td)
        db = _fake_db(td)
        filtered = home / "export.filtered.surql"
        filtered.write_bytes(b"-- surql")
        svc = MagicMock()

        with ExitStack() as stack:
            stack.enter_context(patch("yadgar.core.vacuum._wait_for_health", return_value=True))
            stack.enter_context(patch("yadgar.core.vacuum._bootstrap_namespace"))
            stack.enter_context(patch("yadgar.core.vacuum._redefine_users_post_import"))
            stack.enter_context(
                patch("yadgar.core.vacuum._capture_table_counts", return_value={"memory": 1})
            )
            stack.enter_context(
                patch("yadgar.core.vacuum._assert_backend_quiesced", return_value=True)
            )
            stack.enter_context(
                patch(
                    "yadgar.core.vacuum.launcher.select_side_launcher",
                    return_value=ContainerLauncher(),
                )
            )
            result = _side_build_swap_and_start(
                "http://127.0.0.1:8080", filtered, db, home, {"memory": 1}, svc
            )
        return result, db, home

    def test_killed_side_container_does_not_swap(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            result, db, home = self._run_side_build(td, monkeypatch, exit_code=137)

            assert result is None, "a non-graceful side stop must ABORT the swap"
            assert (db / "sentinel.txt").exists(), (
                "the canonical must be untouched when the side stop was not provably clean"
            )
            assert not list(home.glob("surreal_db.old-*")), "no .old may exist after the abort"
            assert not list(home.glob("surreal_db.new-*")), (
                "an unverified/unflushed build must never wear the .new name — recovery "
                "promotes .new WITHOUT re-verifying"
            )
            assert not list(home.glob("surreal_db.building-*")), "the staging dir must be cleaned"

    def test_clean_side_container_does_swap(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            result, db, home = self._run_side_build(td, monkeypatch, exit_code=0)

            assert result is not None, "a provably clean stop must allow the swap"
            assert (result / "sentinel.txt").exists(), ".old must hold the previous canonical"


# ---------------------------------------------------------------------------
# Container invocation contract (plan §3.3)
# ---------------------------------------------------------------------------


class TestContainerInvocationContract:
    def _start(self, td: str, monkeypatch):
        from yadgar.core.vacuum.launcher import ContainerLauncher

        binary, log = _write_fake_runtime(Path(td))
        monkeypatch.setenv("YADGAR_CONTAINER_RUNTIME", str(binary))
        monkeypatch.setenv("YADGAR_BACKEND_IMAGE", "docker.io/openfantasy/yadgar-backend:test")
        side = Path(td) / "surreal_db.building-20260801_000000"
        side.mkdir()
        ContainerLauncher().start(side_path=side, port=54321, user="root", password="secret")
        return [line for line in _argv_lines(log) if line.startswith("run ")][0], side

    def test_rootless_ownership_and_label_flags(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            run, _side = self._start(td, monkeypatch)
        assert "--user root" in run, "the store must be written with canonical ownership"
        assert "--security-opt label=disable" in run, (
            "SELinux relabelling of the shared data dir would deny the write"
        )

    def test_data_dir_is_bind_mounted_at_slash_data(self, monkeypatch):
        """The $DATA_DIR → /data rewrite task 0100 made universally true."""
        with tempfile.TemporaryDirectory() as td:
            run, side = self._start(td, monkeypatch)
        assert f"-v {side.parent}:/data" in run, (
            "the PARENT data dir must be mounted (the host renames the staging dir "
            f"afterwards); argv was:\n{run}"
        )
        assert f"surrealkv:///data/{side.name}" in run, (
            "the store path must be expressed container-side under /data"
        )

    def test_port_is_published_loopback_only(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            run, _side = self._start(td, monkeypatch)
        assert "-p 127.0.0.1:54321:8000" in run, (
            "the side port must be loopback-only on the host and bound container-wide inside"
        )
        assert "--bind 0.0.0.0:8000" in run, (
            "binding 127.0.0.1 INSIDE the container makes the published port connect-refuse"
        )

    def test_non_loopback_side_host_publishes_on_all_daemon_interfaces(self, monkeypatch):
        """A REMOTE daemon's loopback is not ours — publishing there is unreachable.

        ``docker:dind`` sidecar case (task 228): the runtime that executes the
        ``-p`` lives in a different network namespace, so ``127.0.0.1:<port>``
        binds THAT namespace's loopback and the caller can never connect. When the
        side host is not loopback the port must go on all of the daemon's
        interfaces instead, and be reached at that host.
        """
        monkeypatch.setenv("YADGAR_VACUUM_SIDE_HOST", "dind")
        with tempfile.TemporaryDirectory() as td:
            run, _side = self._start(td, monkeypatch)
        assert "-p 54321:8000" in run, (
            f"a non-loopback side host must publish on all daemon interfaces; argv:\n{run}"
        )
        assert "-p 127.0.0.1:54321:8000" not in run, (
            "the loopback bind is exactly what is unreachable across the namespace split"
        )

    def test_entrypoint_is_surreal_not_the_backend_script(self, monkeypatch):
        """PID 1 must BE surreal, or `stop`'s SIGTERM lands on a bash trap instead."""
        with tempfile.TemporaryDirectory() as td:
            run, _side = self._start(td, monkeypatch)
        assert "--entrypoint surreal" in run
        assert "entrypoint-backend.sh" not in run

    def test_deterministic_name_and_no_rm(self, monkeypatch):
        """`--rm` would reap the container before its exit code could be read."""
        from yadgar.core.vacuum.launcher import SIDE_CONTAINER_NAME

        with tempfile.TemporaryDirectory() as td:
            run, _side = self._start(td, monkeypatch)
        assert f"--name {SIDE_CONTAINER_NAME}" in run
        assert " --rm" not in run

    def test_credentials_are_passed_through(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            run, _side = self._start(td, monkeypatch)
        assert "--user root --pass secret" in run

    def test_leftover_container_is_reaped_before_start(self, monkeypatch):
        """A crashed previous run must not block this one (plan §3.3)."""
        from yadgar.core.vacuum.launcher import SIDE_CONTAINER_NAME

        with tempfile.TemporaryDirectory() as td:
            binary, log = _write_fake_runtime(Path(td))
            monkeypatch.setenv("YADGAR_CONTAINER_RUNTIME", str(binary))
            side = Path(td) / "surreal_db.building-x"
            side.mkdir()
            from yadgar.core.vacuum.launcher import ContainerLauncher

            ContainerLauncher().start(side_path=side, port=1, user="u", password="p")
            argv = _argv_lines(log)

        assert argv[0] == f"rm -f {SIDE_CONTAINER_NAME}", (
            f"the leftover reap must be the FIRST thing start() does; argv:\n{argv}"
        )

    def test_absent_leftover_is_not_fatal(self, monkeypatch):
        """`rm -f` on a container that does not exist must not abort the run."""
        with tempfile.TemporaryDirectory() as td:
            binary, _log = _write_fake_runtime(Path(td), rm_rc=1)
            monkeypatch.setenv("YADGAR_CONTAINER_RUNTIME", str(binary))
            side = Path(td) / "surreal_db.building-x"
            side.mkdir()
            from yadgar.core.vacuum.launcher import ContainerLauncher

            ContainerLauncher().start(side_path=side, port=1, user="u", password="p")

    def test_abandoned_container_is_diagnosed_before_it_is_reaped(self, monkeypatch, capsys):
        """`run -d` exits 0 as soon as the container exists — a SurrealDB that dies
        INSIDE it surfaces only as a health-wait timeout, and the reap would then
        destroy the one place the reason was written.  This car exists BECAUSE an
        undiagnosable vacuum failure wedged the nightly.
        """
        from yadgar.core.vacuum.launcher import ContainerLauncher

        with tempfile.TemporaryDirectory() as td:
            binary, log = _write_fake_runtime(Path(td))
            monkeypatch.setenv("YADGAR_CONTAINER_RUNTIME", str(binary))
            side = Path(td) / "surreal_db.building-x"
            side.mkdir()
            launcher = ContainerLauncher()
            launcher.start(side_path=side, port=1, user="u", password="p")
            launcher.abandon()
            argv = _argv_lines(log)

        err = capsys.readouterr().err
        assert "failed to open store" in err, (
            f"the container's tail must reach the operator before the reap; stderr:\n{err}"
        )
        logs_idx = next(i for i, line in enumerate(argv) if line.startswith("logs "))
        rm_idx = max(i for i, line in enumerate(argv) if line.startswith("rm "))
        assert logs_idx < rm_idx, f"logs must be read BEFORE `rm -f`; argv:\n{argv}"

    def test_clean_stop_reaps_without_dumping_logs(self, monkeypatch):
        """A container that did its job is not noise — only failures get dumped."""
        from yadgar.core.vacuum.launcher import ContainerLauncher

        with tempfile.TemporaryDirectory() as td:
            binary, log = _write_fake_runtime(Path(td), exit_code=0)
            monkeypatch.setenv("YADGAR_CONTAINER_RUNTIME", str(binary))
            side = Path(td) / "surreal_db.building-x"
            side.mkdir()
            launcher = ContainerLauncher()
            launcher.start(side_path=side, port=1, user="u", password="p")
            launcher.stop_clean("http://127.0.0.1:1")
            argv = _argv_lines(log)

        assert not any(line.startswith("logs ") for line in argv), (
            f"a provably clean stop must not dump logs; argv:\n{argv}"
        )

    def test_failed_run_raises(self, monkeypatch):
        import pytest

        with tempfile.TemporaryDirectory() as td:
            binary, _log = _write_fake_runtime(Path(td), run_rc=125)
            monkeypatch.setenv("YADGAR_CONTAINER_RUNTIME", str(binary))
            side = Path(td) / "surreal_db.building-x"
            side.mkdir()
            from yadgar.core.vacuum.launcher import ContainerLauncher

            with pytest.raises(RuntimeError, match="125"):
                ContainerLauncher().start(side_path=side, port=1, user="u", password="p")


class TestBackendImageProbe:
    def test_absent_runtime_is_a_normal_no(self, monkeypatch):
        """A host with no container runtime answers "no image", never raises."""
        from yadgar.core.vacuum.launcher import backend_image_present

        with tempfile.TemporaryDirectory() as td:
            monkeypatch.setenv("YADGAR_CONTAINER_RUNTIME", str(Path(td) / "no-such-runtime"))
            assert backend_image_present() is False

    def test_image_override_is_honoured(self, monkeypatch):
        from yadgar.core.vacuum.launcher import backend_image

        monkeypatch.setenv("YADGAR_BACKEND_IMAGE", "example.invalid/custom:9")
        assert backend_image() == "example.invalid/custom:9"


class TestSideBuildHost:
    """The publish host and the connect host must be the SAME resolved value.

    Layer 4 of the dind namespace split (task 228): ``launcher.py`` published the
    side port and ``vacuum/__init__.py`` built ``side_url`` from two independent
    hardcoded ``127.0.0.1`` literals. They agreed only by coincidence, and stopped
    agreeing the moment the runtime moved into another namespace.
    """

    def test_default_is_loopback(self, monkeypatch):
        """Production behaviour is unchanged — a wider default would leak the corpus."""
        from yadgar.core.vacuum.launcher import SIDE_HOST_DEFAULT, side_build_host

        monkeypatch.delenv("YADGAR_VACUUM_SIDE_HOST", raising=False)
        assert side_build_host() == "127.0.0.1"
        assert SIDE_HOST_DEFAULT == "127.0.0.1"

    def test_override_is_honoured_and_trimmed(self, monkeypatch):
        from yadgar.core.vacuum.launcher import side_build_host

        monkeypatch.setenv("YADGAR_VACUUM_SIDE_HOST", "  dind  ")
        assert side_build_host() == "dind"

    def test_blank_override_falls_back_to_loopback(self, monkeypatch):
        """An empty string must not produce ``http://:1234``."""
        from yadgar.core.vacuum.launcher import side_build_host

        monkeypatch.setenv("YADGAR_VACUUM_SIDE_HOST", "   ")
        assert side_build_host() == "127.0.0.1"

    def test_publish_spec_matches_the_host_it_will_be_reached_at(self, monkeypatch):
        from yadgar.core.vacuum.launcher import side_publish_spec

        monkeypatch.delenv("YADGAR_VACUUM_SIDE_HOST", raising=False)
        assert side_publish_spec(9999) == "127.0.0.1:9999:8000"
        monkeypatch.setenv("YADGAR_VACUUM_SIDE_HOST", "dind")
        assert side_publish_spec(9999) == "9999:8000"


def test_no_stray_side_container_name_collision():
    """The side name must not be one of the real service containers."""
    from yadgar.core.vacuum.launcher import SIDE_CONTAINER_NAME

    assert SIDE_CONTAINER_NAME not in {"yadgar", "yadgar-backend"}
    assert os.sep not in SIDE_CONTAINER_NAME
