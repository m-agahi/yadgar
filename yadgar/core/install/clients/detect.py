"""Auto-detect installed agentic clients — Car 3.

A client is considered "installed" when its global config dir (or file)
exists, or — when the descriptor has no global config path — when its
primary binary is on PATH. The detection logic keys off the same
``ClientDescriptor`` paths defined in ``registry.py`` so the probe is
always in sync with the install target.

Usage::

    from yadgar.core.install.clients.detect import detect_installed_clients
    from yadgar.core.install.clients.registry import CLIENT_REGISTRY

    present = detect_installed_clients()          # uses global registry
    for d in present:
        print(d.name)

The public API exposes two functions:

``is_client_present(descriptor, binary_name=None)``
    Single-client probe.  Returns True when the config dir exists, or when
    *binary_name* is on PATH (binary fallback for descriptor with no config
    path).

``detect_installed_clients(registry=None)``
    Probe all clients in *registry* (defaults to ``CLIENT_REGISTRY``) and
    return the descriptors for clients that pass ``is_client_present``.
    Order is deterministic: sorted by name.
"""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

from yadgar._shared.observability.observe import observe

if TYPE_CHECKING:
    from yadgar.core.install.clients.descriptor import ClientDescriptor


@observe(tier="stage")
def is_client_present(
    descriptor: ClientDescriptor,
    binary_name: str | None = None,
) -> bool:
    """Return True when *descriptor*'s client appears to be installed.

    Detection strategy (in order of preference):

    1. **Config-dir probe**: if the descriptor has a global config path, check
       whether its parent directory exists. A non-existent parent means the
       client has never been run (it would have created the dir). A file that
       exists directly also counts.
    2. **Binary probe** (fallback): if *binary_name* is provided and strategy 1
       finds no path, check whether the binary is on ``PATH`` via
       ``shutil.which``.

    The parent-dir check (rather than a file-exists check) is intentional: a
    freshly installed client creates its config dir on first launch before any
    config file exists. Probing the dir catches that case.

    Args:
        descriptor: the ``ClientDescriptor`` to probe.
        binary_name: optional executable name for PATH fallback (e.g. ``"codex"``).

    Returns:
        True when the client appears installed.
    """
    global_path = descriptor.mcp_config_path.resolve_global()

    if global_path is not None:
        # Config dir probe: parent must exist (or the file itself must exist).
        parent = global_path.parent
        return parent.exists() or global_path.exists()

    # No global config path → fall back to binary probe.
    if binary_name is not None:
        return shutil.which(binary_name) is not None

    return False


@observe(tier="boundary")
def detect_installed_clients(
    registry: dict[str, ClientDescriptor] | None = None,
) -> list[ClientDescriptor]:
    """Probe *registry* and return descriptors for all installed clients.

    Order is deterministic: results are sorted by descriptor name so callers
    can depend on a stable output regardless of dict insertion order.

    Args:
        registry: the client registry to probe.  Defaults to
            ``CLIENT_REGISTRY`` from ``registry.py``.

    Returns:
        List of ``ClientDescriptor`` objects (sorted by name) for clients
        that pass ``is_client_present``.
    """
    if registry is None:
        from yadgar.core.install.clients.registry import CLIENT_REGISTRY  # noqa: PLC0415

        registry = CLIENT_REGISTRY

    return sorted(
        (d for d in registry.values() if is_client_present(d)),
        key=lambda d: d.name,
    )
