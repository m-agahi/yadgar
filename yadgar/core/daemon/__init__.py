"""yadgar.core.daemon — daemon lifecycle package.

T2 Car D3 (layer-boundary train): the flat daemon family packaged per the
no-lone-files law (ADR-0084). ``yadgar.core.daemon`` IS the old ``daemon.py``
dotted path — imports keep working through this PEP-562 re-export
``__init__`` (Car 0 #167 precedent).

  daemon.py    — YadgarDaemon container orchestration + ContainerProfile
  daemons.py   — background daemon-thread startup (auto-update checks, …)
  sd_notify.py — systemd sd_notify READY/RELOADING/STOPPING plumbing
  drain.py     — in-flight request draining + embed-cache snapshot on stop

Back-compat shims remain at the old ``yadgar.core.daemon.daemons`` /
``.sd_notify`` / ``.drain`` paths.
"""

from typing import Final

_EXPORTS: Final = {
    "ContainerProfile": "yadgar.core.daemon.daemon",
    "DEFAULT_BACKEND_EMBED_PORT": "yadgar.core.daemon.daemon",
    "DEFAULT_DEV_PORT": "yadgar.core.daemon.daemon",
    "DEFAULT_PORT": "yadgar.core.daemon.daemon",
    "DOCKERHUB_BACKEND_IMAGE": "yadgar.core.daemon.daemon",
    "DOCKERHUB_IMAGE": "yadgar.core.daemon.daemon",
    "Path": "yadgar.core.daemon.daemon",
    "YadgarDaemon": "yadgar.core.daemon.daemon",
    "_BACKEND_CONTAINER": "yadgar.core.daemon.daemon",
    "_BACKEND_VOLUME": "yadgar.core.daemon.daemon",
    "_HEALTH_TIMEOUT": "yadgar.core.daemon.daemon",
    "_NETWORK_NAME": "yadgar.core.daemon.daemon",
    "_RUNTIME": "yadgar.core.daemon.daemon",
    "_backend_version": "yadgar.core.daemon.daemon",
    "_container_memory_mb": "yadgar.core.daemon.daemon",
    "_default_image": "yadgar.core.daemon.daemon",
    "_dev_profile": "yadgar.core.daemon.daemon",
    "_ensure_network": "yadgar.core.daemon.daemon",
    "_get_runtime": "yadgar.core.daemon.daemon",
    "_host_memory_bytes": "yadgar.core.daemon.daemon",
    "_prod_profile": "yadgar.core.daemon.daemon",
    "_safe_urlopen": "yadgar.core.daemon.daemon",
    "_source_root": "yadgar.core.daemon.daemon",
    "dataclass": "yadgar.core.daemon.daemon",
    "json": "yadgar.core.daemon.daemon",
    "observe": "yadgar.core.daemon.daemon",
    "os": "yadgar.core.daemon.daemon",
    "platform": "yadgar.core.daemon.daemon",
    "subprocess": "yadgar.core.daemon.daemon",
    "sys": "yadgar.core.daemon.daemon",
    "time": "yadgar.core.daemon.daemon",
    "urllib": "yadgar.core.daemon.daemon",
}


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 re-export)

    return getattr(importlib.import_module(target), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_EXPORTS)
