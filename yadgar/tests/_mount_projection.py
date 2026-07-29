"""Shared helpers: parse container `-v host:target` binds and project a
container-absolute path back to the host path it lands on.

Extracted from ``yadgar/tests/core/test_macos_launchd_plists.py`` (which
implemented this for launchd only) so the vacuum-trigger invariant can be
asserted uniformly across every in-repo unit generator — see
``yadgar/tests/scripts/test_vacuum_trigger_cross_generator.py``.

Underscore-prefixed module name keeps pytest from collecting it as a suite.
"""

from __future__ import annotations

import re

__all__ = ["extract_env", "parse_mounts", "project_to_host"]

_MOUNT_RE = re.compile(r"-v\s+(\S+)")


def parse_mounts(run_cmd: str) -> dict[str, str]:
    """Return ``{container_path: host_path}`` for every ``-v host:container`` bind.

    A trailing ``:ro`` / ``:rw`` option on the container side is stripped.
    Host tokens are returned verbatim — an unexpanded template placeholder
    (``@DATA_DIR@``) or a Nix interpolation (``${stateDir}``) is kept as-is so
    callers can compare rendered/unrendered text symmetrically.
    """
    mounts: dict[str, str] = {}
    for raw in _MOUNT_RE.findall(run_cmd):
        host, _, container = raw.partition(":")
        container = container.split(":")[0]
        if host and container:
            mounts[container] = host
    return mounts


def project_to_host(container_path: str, mounts: dict[str, str]) -> str | None:
    """Map *container_path* through *mounts* to its host path.

    Returns ``None`` when the path is under no bind mount — i.e. a write there
    would stay inside the container and never reach a host-side watcher.
    """
    for container_mount, host_mount in mounts.items():
        if container_path == container_mount or container_path.startswith(container_mount + "/"):
            return host_mount + container_path[len(container_mount) :]
    return None


def extract_env(text: str, name: str) -> str | None:
    """Return the value of a ``-e NAME=VALUE`` container env flag, or None."""
    match = re.search(rf"-e\s+{re.escape(name)}=(\S+)", text)
    return match.group(1) if match else None
