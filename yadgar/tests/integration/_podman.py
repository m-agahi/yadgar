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

    Shared here (rather than a fourth copy) for the files that did NOT
    already have one: test_mariadb_connection.py, test_mariadb_dump_arm.py,
    test_mariadb_restore_arm.py (car G3). ``test_mariadb_migrations.py`` (car
    G1) keeps its own copy rather than importing this one — extracting a
    landed car's helper into its diff for no behavioural gain was declined
    there on purpose (see that file's module docstring).
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
