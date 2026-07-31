"""yadgar.core.daemon — daemon lifecycle package.

T2 Car D3 (layer-boundary train): the flat daemon family packaged per the
no-lone-files law (ADR-0084). ``yadgar.core.daemon`` IS the old ``daemon.py``
dotted path — imports keep working through this PEP-562 re-export
``__init__`` (Car 0 #167 precedent).

  daemon.py    — YadgarDaemon container orchestration
  runtime.py   — container-runtime + host-env probing (Car C3 split)
  profiles.py  — ContainerProfile + prod/dev profiles + network (Car C3 split)
  systemd.py   — systemd user-unit rendering (Car C3 split)
  db_migrate.py— one-time copy of the backend DB out of the legacy named volume
                 onto the host bind mount (Bug 11 / task 0100)
  daemons.py   — background daemon-thread startup (auto-update checks, …)
  sd_notify.py — systemd sd_notify READY/RELOADING/STOPPING plumbing
  drain.py     — in-flight request draining + embed-cache snapshot on stop

Back-compat shims remain at the old ``yadgar.core.daemon.daemons`` /
``.sd_notify`` / ``.drain`` paths.
"""

from typing import Final

# Car C3 (module-standardization train) split daemon.py into cohesive siblings.
# The public API is unchanged; each symbol below points at its real home module
# so external importers (via ``from yadgar.core.daemon import X``) are unaffected.
_EXPORTS: Final = {
    # ── orchestrator (daemon.py) ──
    "YadgarDaemon": "yadgar.core.daemon.daemon",
    # ── profiles.py ──
    "ContainerProfile": "yadgar.core.daemon.profiles",
    "_dev_profile": "yadgar.core.daemon.profiles",
    "_ensure_network": "yadgar.core.daemon.profiles",
    "_prod_profile": "yadgar.core.daemon.profiles",
    # ── runtime.py ──
    "DEFAULT_BACKEND_EMBED_PORT": "yadgar.core.daemon.runtime",
    "DEFAULT_DEV_PORT": "yadgar.core.daemon.runtime",
    "DEFAULT_PORT": "yadgar.core.daemon.runtime",
    "DOCKERHUB_BACKEND_IMAGE": "yadgar.core.daemon.runtime",
    "DOCKERHUB_IMAGE": "yadgar.core.daemon.runtime",
    "_BACKEND_CONTAINER": "yadgar.core.daemon.runtime",
    "_BACKEND_VOLUME": "yadgar.core.daemon.runtime",
    "_HEALTH_TIMEOUT": "yadgar.core.daemon.runtime",
    "_NETWORK_NAME": "yadgar.core.daemon.runtime",
    "_RUNTIME": "yadgar.core.daemon.runtime",
    "_backend_version": "yadgar.core.daemon.runtime",
    "_container_memory_mb": "yadgar.core.daemon.runtime",
    "_default_image": "yadgar.core.daemon.runtime",
    "_get_runtime": "yadgar.core.daemon.runtime",
    "_host_memory_bytes": "yadgar.core.daemon.runtime",
    "_safe_urlopen": "yadgar.core.daemon.runtime",
    "_source_root": "yadgar.core.daemon.runtime",
    # ── incidental module globals preserved for back-compat surface ──
    "Path": "yadgar.core.daemon.daemon",
    "json": "yadgar.core.daemon.daemon",
    "observe": "yadgar.core.daemon.daemon",
    "os": "yadgar.core.daemon.daemon",
    "subprocess": "yadgar.core.daemon.daemon",
    "sys": "yadgar.core.daemon.daemon",
    "time": "yadgar.core.daemon.daemon",
    "urllib": "yadgar.core.daemon.daemon",
    "dataclass": "yadgar.core.daemon.profiles",
    "platform": "yadgar.core.daemon.runtime",
}


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 re-export)

    return getattr(importlib.import_module(target), name)


def __dir__() -> list[str]:
    return list(globals()) + list(_EXPORTS)
