"""Side-build launchers — how vacuum obtains the throwaway SurrealDB it builds into.

Phase 3 of ``yadgar vacuum`` builds the compacted DB on a side path by starting a
THROWAWAY SurrealDB against it, importing, verifying per-table counts, and then
stopping it gracefully before the atomic swap.  Until Car 0092 that throwaway was
always a host-side ``subprocess.Popen(["surreal", ...])``
(:mod:`yadgar.core._surreal_runner`) — and on a container install the ``surreal``
binary exists ONLY inside the ``yadgar-backend`` image (``Dockerfile.backend``:
``COPY --from=surrealdb/surrealdb:v3.1.5``).  v5.170.0 turned the resulting
late-abort wedge into a clean SKIP; this module is what lets such a host actually
vacuum.

Two implementations behind one seam:

===================== ==========================================================
:class:`HostBinaryLauncher`  a usable ``surreal`` is on PATH — dev boxes, nix
                             hosts, manual installs.  Unchanged behaviour.
:class:`ContainerLauncher`   otherwise, when the backend image is present: a
                             one-shot container running THE SAME binary that
                             will later open the result, so version skew between
                             builder and opener is structurally impossible.
``None``                     neither — the caller SKIPs (v5.170.0 path).
===================== ==========================================================

**The graceful-stop assertion is the whole safety property of the swap.**  A
SIGKILL'd surrealkv directory is half-flushed and corrupt-on-reopen (ADR-0090),
so a side build that cannot PROVE a clean exit must raise and leave the canonical
untouched rather than swap.  The host path proves it with
``proc.wait(timeout=...)``; the container path has no ``Popen`` exit code, so it
reproduces the proof with ``stop --time 30`` → ``wait`` → ``inspect
'{{.State.ExitCode}}'`` and raises on a non-zero code or a timed-out stop.

Two invocation details are load-bearing rather than cosmetic:

* **No ``--rm``.**  A ``--rm`` container is reaped the instant it exits, so the
  ``inspect`` that reads the exit code races removal — and the only way to make
  that race pass is to weaken the assertion the whole design rests on.  The
  container is therefore run detached under a DETERMINISTIC name and removed
  explicitly once its exit code has been read.  The same name is what lets a
  crashed previous run be reaped before this one starts.
* **The entrypoint is overridden to ``surreal``.**  The image's own
  ``CMD ["/entrypoint-backend.sh"]`` starts uvicorn alongside SurrealDB, binds
  8000/8001 and traps TERM in bash.  Running it would put a shell at PID 1, so
  ``stop``'s SIGTERM would reach the shell rather than SurrealDB — and any
  escalation to SIGKILL is exactly the state the assertion exists to refuse.

Guarded by ``yadgar/tests/core/test_vacuum_side_launcher.py`` (no container is
created there: the runtime is a fake that records argv).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from abc import ABC, abstractmethod
from pathlib import Path

from yadgar._shared.observability.observe import observe

#: Deterministic name for the throwaway side container.  Deterministic ON PURPOSE:
#: a crashed run must be findable and reapable by the next one (a random name
#: would leak a container holding the staging dir until a human noticed).
SIDE_CONTAINER_NAME = "yadgar-vacuum-side-build"

#: Port SurrealDB binds INSIDE the container.  The host-visible port is chosen by
#: the caller (a free port) and published loopback-only onto this one.
SIDE_CONTAINER_PORT = 8000

#: Grace SurrealDB gets to flush and exit on SIGTERM before the runtime escalates
#: to SIGKILL.  An escalation shows up as a non-zero exit code and ABORTS the swap.
STOP_GRACE_SEC = 30

#: Worker-thread stack size, mirroring ``entrypoint-backend.sh``.  The default
#: tokio stack (~2 MiB) overflows on deep queries and ABORTS the whole process —
#: and the side build's whole job is a full-size ``/import``.  Overriding the
#: entrypoint means these have to be carried here explicitly.
SURREAL_STACK_BYTES = "33554432"

_RUN_TIMEOUT_SEC = 120.0
_SHORT_TIMEOUT_SEC = 30.0
#: Must exceed STOP_GRACE_SEC — otherwise the subprocess timeout fires before the
#: runtime has finished escalating, and we would report "timeout" for a stop that
#: was merely slow.
_STOP_TIMEOUT_SEC = STOP_GRACE_SEC + 30.0


@observe(tier="stage")
def _run(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
    """Single choke point for every container-runtime invocation.

    Everything the container launcher does goes through here, so the whole path
    is exercisable with a fake runtime that records argv — no real containers in
    the test suite, and no podman/docker required to prove the argv contract.
    """
    return subprocess.run(  # noqa: S603 — argv list, no shell; binary from _get_runtime()
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


@observe(tier="stage")
def _runtime() -> str:
    from yadgar.core.daemon.runtime import _get_runtime  # noqa: PLC0415 — avoid import cycle

    return _get_runtime()


@observe(tier="stage")
def backend_image() -> str:
    """The backend image the side container would run (same resolution as daemon)."""
    from yadgar.core.daemon.runtime import DOCKERHUB_BACKEND_IMAGE  # noqa: PLC0415

    return os.environ.get("YADGAR_BACKEND_IMAGE", DOCKERHUB_BACKEND_IMAGE)


@observe(tier="stage")
def backend_image_present() -> bool:
    """True iff the backend image is in the local runtime store.

    EXISTENCE only — a version-compatibility gate between the image and any host
    binary is deliberately out of scope (the one-shot container makes skew
    structurally impossible on the path that uses it).  Never raises: a runtime
    that is absent or will not answer means "cannot side-build in a container",
    which is a SKIP, not a failure.
    """
    try:
        return (
            _run([_runtime(), "image", "inspect", backend_image()], _SHORT_TIMEOUT_SEC).returncode
            == 0
        )
    except Exception as exc:  # noqa: BLE001 — absence of a runtime is a normal answer
        print(f"[vacuum] preflight: container runtime unavailable ({exc})", file=sys.stderr)
        return False


class SideBackendLauncher(ABC):
    """Start / prove-a-clean-stop / force-reap a throwaway SurrealDB for the side build."""

    #: Human-readable name for the log line naming which path a run took.
    label: str = "side backend"

    @abstractmethod
    def start(self, *, side_path: Path, port: int, user: str, password: str) -> None:
        """Start the throwaway serving *side_path* on 127.0.0.1:*port*."""

    @abstractmethod
    def stop_clean(self, side_url: str) -> None:
        """Stop gracefully and PROVE it exited cleanly, else raise.

        Raising is the contract: the caller aborts the swap and leaves the
        canonical untouched, because a store that was not flushed is
        corrupt-on-reopen (ADR-0090).
        """

    @abstractmethod
    def abandon(self) -> None:
        """Force-reap after a failed build.  Best-effort; never raises."""


class HostBinaryLauncher(SideBackendLauncher):
    """Today's path: a host-side ``surreal start`` subprocess."""

    label = "host `surreal` process"

    def __init__(self) -> None:
        self._proc = None

    @observe(tier="stage")
    def start(self, *, side_path: Path, port: int, user: str, password: str) -> None:
        from yadgar.core._surreal_runner import spawn_surreal  # noqa: PLC0415

        self._proc = spawn_surreal(
            port=port,
            data_dir=str(side_path),
            surreal_user=user,
            surreal_pass=password,
        )

    @observe(tier="stage")
    def stop_clean(self, side_url: str) -> None:
        """SIGTERM and require a clean exit; a process that needs SIGKILL raises.

        Moved verbatim from ``vacuum._stop_side_backend_clean`` (Car 0092) so both
        launchers present the same proof-of-graceful-exit contract.
        """
        import time  # noqa: PLC0415

        import httpx  # noqa: PLC0415

        proc = self._proc
        if proc is None:
            return
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            proc.wait(timeout=15.0)
        except Exception as exc:  # subprocess.TimeoutExpired or similar
            # Escalate to kill so we don't leak the process, but ABORT the swap:
            # a non-graceful stop means the segments may not be flushed.
            try:
                proc.kill()
                proc.wait(timeout=5.0)
            except Exception:
                pass
            raise RuntimeError(
                f"side backend at {side_url} did not exit gracefully on SIGTERM "
                f"({exc}); refusing to swap a possibly half-flushed surrealkv dir"
            ) from exc
        # Belt-and-suspenders: poll the URL until it stops answering (lock released).
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                httpx.get(f"{side_url}/health", timeout=1.0)
            except Exception:
                break  # connection refused → port released → process gone
            time.sleep(0.2)

    @observe(tier="stage")
    def abandon(self) -> None:
        from yadgar.core._surreal_runner import teardown_surreal_proc  # noqa: PLC0415

        if self._proc is not None:
            teardown_surreal_proc(self._proc, wait_timeout=5)


class ContainerLauncher(SideBackendLauncher):
    """One-shot backend container running the SAME ``surreal`` the real backend runs.

    The store directory is reached through the data-dir bind mount the backend
    already uses, so the container writes ownership and SELinux labels identical
    to the canonical store's under a rootless userns — ``--user root`` and
    ``--security-opt label=disable`` are required for that, not cosmetic.
    """

    label = "one-shot backend container"

    def __init__(self, name: str = SIDE_CONTAINER_NAME) -> None:
        self._name = name
        #: True once THIS object started a container.  Gates the log dump in
        #: ``abandon`` so the pre-start leftover reap stays silent (there is
        #: nothing of ours to diagnose yet) while a failed build is not thrown
        #: away undiagnosed.
        self._started = False

    @observe(tier="stage")
    def start(self, *, side_path: Path, port: int, user: str, password: str) -> None:
        runtime = _runtime()
        image = backend_image()
        # A previous crashed run must not block this one: reap the leftover
        # container first.  Best-effort — "no such container" is the normal case.
        self.abandon()
        # Mount the PARENT (the data dir the real backend mounts at /data) rather
        # than the staging dir itself: the host later RENAMES .building-<ts> →
        # .new-<ts> → surreal_db, and the layout the container writes must be the
        # one the real backend will open.
        argv = [
            runtime,
            "run",
            "-d",
            "--name",
            self._name,
            "--user",
            "root",
            "--security-opt",
            "label=disable",
            "-p",
            f"127.0.0.1:{port}:{SIDE_CONTAINER_PORT}",
            "-v",
            f"{side_path.parent}:/data",
            "-e",
            f"SURREAL_RUNTIME_STACK_SIZE={SURREAL_STACK_BYTES}",
            "-e",
            f"RUST_MIN_STACK={SURREAL_STACK_BYTES}",
            # PID 1 must BE surreal — see the module docstring.
            "--entrypoint",
            "surreal",
            image,
            "start",
            "--no-banner",
            # Bind all interfaces INSIDE the container; the publish above is what
            # keeps it loopback-only on the host.  127.0.0.1 here would make the
            # published port connect-refuse.
            "--bind",
            f"0.0.0.0:{SIDE_CONTAINER_PORT}",
            "--user",
            user,
            "--pass",
            password,
            f"surrealkv:///data/{side_path.name}",
        ]
        result = _run(argv, _RUN_TIMEOUT_SEC)
        if result.returncode != 0:
            raise RuntimeError(
                f"could not start the side-build container {self._name!r} from {image}: "
                f"exit {result.returncode}\n{(result.stderr or '').strip()[:500]}"
            )
        self._started = True

    @observe(tier="stage")
    def stop_clean(self, side_url: str) -> None:
        """``stop --time`` → ``wait`` → ``inspect ExitCode``; raise unless it is 0.

        This is the container-side equivalent of the host path's
        ``proc.wait(timeout=...)`` proof.  A stop that had to escalate to SIGKILL
        surfaces as a non-zero exit code (typically 137) — precisely the
        half-flushed store this must refuse to swap in.  Every failure mode
        (timeout, unreadable code, non-zero code) raises; NONE of them may be
        downgraded to a warning.
        """
        runtime = _runtime()
        try:
            stop = _run(
                [runtime, "stop", "--time", str(STOP_GRACE_SEC), self._name], _STOP_TIMEOUT_SEC
            )
            if stop.returncode != 0:
                raise RuntimeError(
                    f"`{runtime} stop` failed for {self._name!r}: exit {stop.returncode}\n"
                    f"{(stop.stderr or '').strip()[:500]}"
                )
            _run([runtime, "wait", self._name], _STOP_TIMEOUT_SEC)
            inspected = _run(
                [runtime, "inspect", "--format", "{{.State.ExitCode}}", self._name],
                _SHORT_TIMEOUT_SEC,
            )
            if inspected.returncode != 0:
                raise RuntimeError(
                    f"could not read the exit code of {self._name!r}: exit "
                    f"{inspected.returncode}\n{(inspected.stderr or '').strip()[:500]}"
                )
            raw = (inspected.stdout or "").strip()
            try:
                exit_code = int(raw)
            except ValueError as exc:
                raise RuntimeError(f"unparsable exit code {raw!r} for {self._name!r}") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"side backend at {side_url} did not stop within the grace window "
                f"({exc}); refusing to swap a possibly half-flushed surrealkv dir"
            ) from exc
        if exit_code != 0:
            raise RuntimeError(
                f"side backend container {self._name!r} at {side_url} exited "
                f"{exit_code} (a graceful SurrealDB SIGTERM shutdown exits 0; a "
                f"non-zero code means it was killed after the {STOP_GRACE_SEC}s "
                f"grace window); refusing to swap a possibly half-flushed "
                f"surrealkv dir"
            )
        # Provably clean exit — nothing left to diagnose, so the reap below stays
        # silent rather than dumping the logs of a container that did its job.
        self._started = False
        self.abandon()

    @observe(tier="stage")
    def _dump_logs(self) -> None:
        """Surface the side container's tail BEFORE reaping it.

        This car exists because a vacuum failure that could not be diagnosed
        wedged the nightly.  ``run -d`` returns 0 as soon as the container is
        created, so a SurrealDB that fails to start INSIDE it shows up only as a
        health-wait timeout — and the reap would then destroy the one place the
        reason was written.  Best-effort and never raising: this runs on a path
        that is already failing.
        """
        try:
            logs = _run([_runtime(), "logs", "--tail", "50", self._name], _SHORT_TIMEOUT_SEC)
        except Exception as exc:  # noqa: BLE001 — diagnostics only; never masks the real error
            print(f"[vacuum] WARNING: could not read side container logs: {exc}", file=sys.stderr)
            return
        tail = ((logs.stdout or "") + (logs.stderr or "")).strip()
        if tail:
            print(
                f"[vacuum] side container {self._name!r} last output before reap:\n{tail[-2000:]}",
                file=sys.stderr,
            )

    @observe(tier="stage")
    def abandon(self) -> None:
        if self._started:
            self._dump_logs()
        try:
            _run([_runtime(), "rm", "-f", self._name], _SHORT_TIMEOUT_SEC)
        except Exception as exc:  # noqa: BLE001 — best-effort reap; never masks the real error
            print(
                f"[vacuum] WARNING: could not reap side container {self._name!r}: {exc}",
                file=sys.stderr,
            )
        self._started = False


@observe(tier="stage")
def select_side_launcher() -> SideBackendLauncher | None:
    """Pick the side-build launcher for this host, or None when neither is available.

    Host binary FIRST: it is the cheaper path and the one dev/nix hosts have been
    using all along, so choosing it keeps those installs bit-for-bit unchanged.
    """
    if shutil.which("surreal") is not None:
        return HostBinaryLauncher()
    if backend_image_present():
        return ContainerLauncher()
    return None
