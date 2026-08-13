"""Shared podman/docker subprocess env helper for engine-#2 integration tests.

Follows the repo's `_`-prefixed shared-helper convention (``_paths.py``,
``_surreal_helpers.py``, ``_entrypoint_sql.py``) rather than living in
``conftest.py`` — a conftest module is pytest plumbing (fixtures/hooks
discovered by pytest itself), not a place other test modules should import
plain functions from.
"""

from __future__ import annotations

import os
import pwd
import shutil
import subprocess
import uuid
from pathlib import Path


def podman_env() -> dict[str, str]:
    """Environment for every container command — with the REAL home restored.

    ``yadgar/tests/conftest.py`` redirects ``HOME`` / ``XDG_DATA_HOME`` to a tmp
    dir, at session scope and AGAIN per test (#64 hook-install isolation). A
    rootless podman reads its container store from ``$XDG_DATA_HOME/containers``,
    so a container created under one redirect is invisible to a command run
    under the next: ``podman exec`` answers ``no such container`` for a
    container that is demonstrably running, and — worse and silently — a
    teardown ``podman rm -f`` removes nothing and leaks the container.

    Restoring the real home from the PASSWORD DATABASE rather than
    ``os.environ`` is what makes this immune: by the time any test body runs,
    the environment no longer holds the value we need.

    Shared here (rather than a per-file copy) by every engine-#2 fixture.
    ``test_mariadb_migrations.py`` (car G1) used to keep its own copy — car G6
    dropped that when the socket-directory derivation below made a third
    private copy an actual drift risk rather than a stylistic one.
    """
    env = dict(os.environ)
    real_home = pwd.getpwuid(os.getuid()).pw_dir
    env["HOME"] = real_home
    env["XDG_DATA_HOME"] = f"{real_home}/.local/share"
    env["XDG_CONFIG_HOME"] = f"{real_home}/.config"
    env["XDG_STATE_HOME"] = f"{real_home}/.local/state"
    return env


def select_container_runtime(*, env: dict[str, str] | None = None) -> str | None:
    """Return the path to the first container runtime that actually WORKS.

    ``shutil.which("podman") or shutil.which("docker")`` only proves a binary
    is on PATH. Car G5 found the self-hosted GH runner (github-runners_runner_1)
    carries a `podman` binary that fails EVERY invocation with ``Error: cannot
    re-exec process`` (rootless podman cannot nest inside the runner's own
    container — confirmed live: ``podman info`` and ``podman run`` both fail
    instantly, no timeout), while `docker` — proxied through the
    ``docker:dind`` sidecar (``DOCKER_HOST=tcp://dind:2375``) — works fine. A
    presence-only check picks the broken binary every time, so the fixture's
    subsequent container-start attempt fails instantly and every dependent
    test skips — for all four engine-#2 test modules, in well under the ~2s
    it takes pytest to even report the run.

    Probing ``<runtime> info`` catches this: near-instant, no image pull, and
    it fails the exact same way ``run`` would if the runtime cannot actually
    start anything. Preference order (podman before docker) is unchanged —
    only presence-vs-function changed.
    """
    probe_env = podman_env() if env is None else env
    for name in ("podman", "docker"):
        path = shutil.which(name)
        if path is None:
            continue
        probe = subprocess.run(
            [path, "info"],
            capture_output=True, check=False, timeout=15, env=probe_env,
        )  # fmt: skip
        if probe.returncode == 0:
            return path
    return None


# ── where a bind mount is visible to BOTH sides ──────────────────────────────
#
# Every engine-#2 fixture hands the server a unix socket through a bind mount
# and then connects to that socket as an ORDINARY HOST PROCESS. That only works
# where `-v <src>:/sockets` and this process mean the SAME directory — and
# `<src>` is resolved by whatever daemon actually creates the container, NOT by
# us. The four fixtures hardcoded `/tmp`, which holds only while the daemon
# shares our filesystem.
#
# It does not on the self-hosted GH runner. That runner is a container
# (github-runners_runner_1) whose `docker` is a client for a SEPARATE
# `docker:dind` sidecar — `DOCKER_HOST=tcp://dind:2375` (ADR-0166). `docker run
# -v /tmp/ymdb-x:/sockets` therefore resolves `/tmp/ymdb-x` inside DIND, where
# dockerd silently creates it because it does not exist. mysqld puts its socket
# there; our `/tmp/ymdb-x` stays the empty directory we made and never receives
# anything. The client then reports, for the full 180s boot wait:
#
#   (2003, "Can't connect to MySQL server on 'localhost'
#           ([Errno 2] No such file or directory)")
#
# Errno 2 rather than ECONNREFUSED is the tell: a MISSING SOCKET FILE, not a
# refused TCP connect. Nothing was ever listening on a port, so no amount of
# host/port derivation addresses it.
#
# Something DOES cross on that runner: both containers bind the same host path
# at the same destination — `/home/max/.gh-runners/r1/data -> /runner/work` —
# so anything under `/runner/work` is one directory to both sides. That is what
# the candidates below reach via `RUNNER_TEMP` / `RUNNER_WORKDIR`.
#
# Deriving the DB HOST from `DOCKER_HOST` instead (i.e. publishing a port and
# connecting to `dind:3306`) was considered and rejected: the accounts these
# fixtures create are `'yadgar_app'@'localhost'` — replayed VERBATIM out of
# entrypoint-backend.sh by test_mariadb_migrations.py, which reads them back
# with `SHOW GRANTS FOR '…'@'localhost'`. Over TCP the client presents as
# 10.89.1.x and authentication fails before any assertion runs; making it work
# would mean forking the entrypoint replay into `@'%'` accounts, which is the
# exact drift that file exists to prevent. Keeping the unix socket keeps the
# production option-file shape (`socket = …`) and the production grant shape.

_SHARED_ROOT_CACHE: dict[str, Path] = {}

# A NAME, not an inline tuple, on purpose. `except (A, B):` written inline is
# rewritten by ruff format 0.16.x into the PEP 758 bare form `except A, B:`,
# which this repo's own guard (test_v5_46_16_except_tuple_sweep.py) then fails
# the build on. The usual pin is a trailing `# fmt: skip`, but that pin was
# lost once during a pre-commit rewrite that `git add -A` then swept into a
# commit containing a file that did not parse. Binding the tuple to a name
# removes the hazard instead of suppressing it: there are no parentheses left
# for a formatter to strip.
_RUN_ERRORS = (OSError, subprocess.SubprocessError)


def _mount_crosses(runtime: str, image: str, candidate: Path, env: dict[str, str]) -> bool:
    """Does a bind mount of ``candidate`` reach THIS process's view of it?

    Empirical, not inferred: start a throwaway container that writes a random
    token into the mount, then try to read that token back from our own
    filesystem. Under dind the write lands on the daemon's disk and we read
    nothing, so the candidate is correctly rejected — no ``DOCKER_HOST``
    parsing, no topology assumption, and a future runner layout that differs
    from today's is handled by the same code.

    Uses the caller's own MariaDB image rather than introducing a second one:
    it is already required by every fixture that calls this, so probing cannot
    fail for a reason (an unpullable image) that the fixture itself would not
    have failed for anyway.
    """
    probe_dir = candidate / f"ymnt-{uuid.uuid4().hex[:8]}"
    try:
        probe_dir.mkdir(mode=0o777, parents=True)
        probe_dir.chmod(0o777)  # mkdir mode is umask-masked
    except OSError:
        return False

    token = uuid.uuid4().hex
    try:
        wrote = subprocess.run(
            [
                runtime, "run", "--rm", "-v", f"{probe_dir}:/probe:Z",
                "--entrypoint", "sh", image,
                "-c", f"printf %s {token} > /probe/sentinel",
            ],
            capture_output=True, text=True, check=False, timeout=600, env=env,
        )  # fmt: skip
        if wrote.returncode != 0:
            return False
        sentinel = probe_dir / "sentinel"
        return sentinel.is_file() and sentinel.read_text(encoding="utf-8") == token
    except _RUN_ERRORS:
        return False
    finally:
        remove_container_dir(runtime, probe_dir, image=image, env=env)


def shared_mount_root(runtime: str, *, image: str, env: dict[str, str] | None = None) -> Path:
    """A directory that is the SAME files for this process and for the daemon.

    Candidates, in order, each PROVEN by ``_mount_crosses`` before it is used:

    ``YADGAR_TEST_SHARED_MOUNT_ROOT``
        Explicit override, for a host whose layout none of the rest describes.
    ``/tmp``
        Correct whenever the daemon shares our filesystem — rootless podman and
        a local dockerd. This is the path every developer machine takes, and it
        is first so that the common case costs one probe and nothing else.
    ``RUNNER_TEMP``
        The dind case. GitHub points this inside ``RUNNER_WORKDIR``, which is
        the volume both the runner and the dind sidecar mount at the same
        destination. Preferred over the workspace because the workspace is the
        git checkout — a scratch socket directory there would show up in ``git
        status`` and in the repo's own tree checks.
    ``RUNNER_WORKDIR``
        The same volume, one level up, for a runner that does not set
        ``RUNNER_TEMP``.

    Raises rather than skipping when nothing crosses. A skip here would be the
    silent green this whole job exists to abolish, and the failure names every
    directory tried so the next person does not have to rediscover the
    topology.
    """
    probe_env = podman_env() if env is None else env
    cached = _SHARED_ROOT_CACHE.get(runtime)
    if cached is not None:
        return cached

    candidates: list[Path] = []
    for source in (
        probe_env.get("YADGAR_TEST_SHARED_MOUNT_ROOT"),
        "/tmp",
        probe_env.get("RUNNER_TEMP"),
        probe_env.get("RUNNER_WORKDIR"),
    ):
        if not source:
            continue
        path = Path(source)
        if path.is_dir() and path not in candidates:
            candidates.append(path)

    for candidate in candidates:
        if _mount_crosses(runtime, image, candidate, probe_env):
            _SHARED_ROOT_CACHE[runtime] = candidate
            return candidate

    raise RuntimeError(
        "no directory on this host is visible to BOTH this process and the "
        f"{Path(runtime).name} daemon, so a bind-mounted unix socket cannot be "
        "reached. Tried: " + ", ".join(str(c) for c in candidates) + ". "
        "Set YADGAR_TEST_SHARED_MOUNT_ROOT to a path both sides mount at the "
        "same location (under dind, something inside the runner's own work "
        "volume)."
    )


def make_socket_dir(
    runtime: str, *, image: str, prefix: str, env: dict[str, str] | None = None
) -> Path:
    """A world-writable scratch dir under the shared root, for a mysqld socket.

    Short by construction. A unix socket path caps at ~107 bytes, which is why
    none of these fixtures use ``tmp_path``; the shared root can itself be a
    nested runner path, so the leaf stays a prefix plus eight hex characters.
    """
    sock_dir = shared_mount_root(runtime, image=image, env=env) / f"{prefix}-{uuid.uuid4().hex[:8]}"
    sock_dir.mkdir(mode=0o777, parents=True)
    sock_dir.chmod(0o777)  # mkdir mode is umask-masked
    return sock_dir


def remove_container_dir(
    runtime: str, target: Path, *, image: str, env: dict[str, str] | None = None
) -> None:
    """Delete a bind-mount dir, including what the container's uid took over.

    Three escalating attempts, because the obstacle differs per runtime:

    1. ``shutil.rmtree`` — enough whenever we still own the directory.
    2. ``podman unshare`` — rootless podman chowns the mount to the image's
       ``mysql`` user, which lands on a SUBUID the host user cannot even rmdir
       ("Operation not permitted") despite owning the parent. ``unshare``
       re-enters the namespace where that subuid maps to root.
    3. a container-side ``rm -rf`` — the docker/dind case, which has no
       ``unshare`` and where the mount is a real bind on a shared volume owned
       by a real uid we are not. The daemon runs as root there, so a throwaway
       container deletes what we cannot. Without this the runner leaks a
       datadir-adjacent directory into the work volume on every CI run.
    """
    run_env = podman_env() if env is None else env
    shutil.rmtree(target, ignore_errors=True)
    if not target.exists():
        return

    if Path(runtime).name == "podman":
        subprocess.run(
            [runtime, "unshare", "rm", "-rf", str(target)],
            capture_output=True, check=False, timeout=120, env=run_env,
        )  # fmt: skip
        if not target.exists():
            return

    parent = target.parent
    subprocess.run(
        [
            runtime, "run", "--rm", "-v", f"{parent}:/cleanup:Z",
            "--entrypoint", "sh", image,
            "-c", f"rm -rf /cleanup/{target.name}",
        ],
        capture_output=True, check=False, timeout=300, env=run_env,
    )  # fmt: skip


def container_is_running(runtime: str, name: str, *, env: dict[str, str] | None = None) -> bool:
    """Is the named container still up?

    The readiness waits below poll for a server that may already be DEAD — a
    crashed mysqld otherwise burns the full boot timeout proving nothing. One
    cheap inspect per poll turns that into an immediate, explained failure.
    """
    probe = subprocess.run(
        [runtime, "inspect", "-f", "{{.State.Running}}", name],
        capture_output=True, text=True, check=False, timeout=30,
        env=podman_env() if env is None else env,
    )  # fmt: skip
    return probe.returncode == 0 and probe.stdout.strip() == "true"


def container_logs(
    runtime: str, name: str, *, env: dict[str, str] | None = None, tail: int = 40
) -> str:
    """Last lines of a container's output, for a failure message worth reading."""
    got = subprocess.run(
        [runtime, "logs", "--tail", str(tail), name],
        capture_output=True, text=True, check=False, timeout=60,
        env=podman_env() if env is None else env,
    )  # fmt: skip
    return ((got.stdout or "") + (got.stderr or "")).strip()
