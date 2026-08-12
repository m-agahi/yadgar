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
