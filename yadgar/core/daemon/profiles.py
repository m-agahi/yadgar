"""Container-profile definitions + network helper for the Yadgar daemon.

Split out of ``daemon.py`` (Car C3, module-standardization train). A
``ContainerProfile`` bundles the prod/dev container settings the orchestrator
templates ``docker run`` from; ``_prod_profile`` / ``_dev_profile`` build them
from env overrides, and ``_ensure_network`` creates the shared bridge network.
"""

import os
import subprocess
from dataclasses import dataclass

from yadgar._shared.observability.observe import observe
from yadgar.core.daemon.runtime import (
    _NETWORK_NAME,
    DEFAULT_DEV_PORT,
    DEFAULT_PORT,
    DOCKERHUB_IMAGE,
)


@dataclass
class ContainerProfile:
    container_name: str
    image_name: str
    volume_name: str
    port: int  # host port — maps to container port 8765
    cpus: float
    restart_policy: str
    is_dev: bool


@observe(tier="hot")
def _prod_profile(port: int = DEFAULT_PORT) -> ContainerProfile:
    return ContainerProfile(
        container_name=os.environ.get("YADGAR_CONTAINER", "yadgar"),
        image_name=os.environ.get("YADGAR_IMAGE", DOCKERHUB_IMAGE),
        volume_name=os.environ.get("YADGAR_VOLUME", "yadgar-data"),
        port=port,
        cpus=1.0,
        restart_policy="on-failure:3",
        is_dev=False,
    )


@observe(tier="hot")
def _dev_profile(port: int = DEFAULT_DEV_PORT) -> ContainerProfile:
    return ContainerProfile(
        container_name=os.environ.get("YADGAR_DEV_CONTAINER", "yadgar-dev"),
        image_name=os.environ.get("YADGAR_DEV_IMAGE", "yadgar-dev"),
        volume_name=os.environ.get("YADGAR_DEV_VOLUME", "yadgar-dev-data"),
        port=port,
        cpus=2.0,
        restart_policy="no",
        is_dev=True,
    )


# ── Network helper ────────────────────────────────────────────────────────────


@observe(tier="stage")
def _ensure_network() -> None:
    """Create the yadgar Docker network if it doesn't exist."""
    result = subprocess.run(
        ["docker", "network", "inspect", _NETWORK_NAME],
        capture_output=True,
    )
    if result.returncode != 0:
        subprocess.run(
            ["docker", "network", "create", "--driver", "bridge", _NETWORK_NAME],
            check=True,
            capture_output=True,
        )
