"""CREATE-path ``project`` registry enforcement for the MCP tool surface.

Car 5 (2026-08-20 train). Before this module the registry check was a claim,
not a mechanism: a standalone backend guard under ``admin_exec`` had ZERO
production call sites — a definition, an ``__all__`` entry, and ~12 docstrings
across ``adr.py`` / ``memorize.py`` / ``recall.py`` / ``task.py`` / ``wiki.py``
/ ``_project_param.py`` asserting that "the deep registry check is
backend-side" at a write path that never called it. The only real enforcement
was ``MariaStorageEngine.assert_project_registered``, reached from
``create_task_row`` / ``create_adr_row`` — the engine-#2 LEDGER tables only.
``memory.project_id`` and ``wiki_page.project_id`` had no registry check on any
writer, so ``memorize(project="typo/repo")`` minted a phantom namespace exactly
as ADR-0202 said the registry existed to prevent. (Task 384 deleted that dead
guard outright; ``assert_project_registered`` is now the only one there is.)

WHY THE CHECK LIVES IN CORE AND TRAVELS OVER ``_forward_admin``
---------------------------------------------------------------
Three placements were available. Two are closed by facts in this repo:

* **In ``_shared``, reading ``_st._sql_storage`` directly.** Closed:
  ``init_engines(sql_storage=False)`` is the default, and only
  ``embed_service._ensure_recall_engines`` passes ``True``. The slot is
  therefore ALWAYS ``None`` in the core process (the default is called
  "load-bearing" in ``init_engines``' own docstring, because ADR-0078/ADR-0200
  forbid core touching either database). Such a predicate would raise
  ``ProjectRegistryUnavailableError`` on every core write.

* **In the drainer** (``QueueDrainer._validate_project_id``), which is where
  the queued memory/wiki writes are already gated. Closed by the event loop:
  ``QueueDrainer`` is a bare ``threading.Thread`` with no running loop, while
  ``MariaStorageEngine`` builds its ``AsyncEngine`` with the default
  ``AsyncAdaptedQueuePool``. Reaching engine #2 from there means an
  ``asyncio.run`` per write — a private loop whose pool caches connections
  bound to a loop that dies with the thread. This repo has written that hazard
  down three times (``backend/retrieval/superseded.py``: *"Do not move this
  call downstream"*; ``backend/admin_exec/invariants_cross_engine.py``;
  ``backend/embed_service/embed_service_lifecycle.py``) and it is precisely why
  the deleted guard's ``asyncio.run`` wrapper never acquired a caller: it was
  unusable from every process that would want it.

* **In core, forwarded to the backend ``/admin`` route** — this module. The
  query runs on the backend's own event loop, so neither problem exists, and
  ``_forward_admin`` is the sanctioned core→backend seam already used by
  ``runtime_config`` and the rekey migration. No new admin op is needed:
  ``list_project_rows`` exists, and the registry is small by construction
  ("one row per project the user owns" — ``ledger.list_project_rows``), so the
  whole key set is cached in-process and membership is answered without I/O.

CACHE FRESHNESS — A MISS FORCES ONE REFRESH
-------------------------------------------
A TTL cache alone would reject a project registered SINCE the cache was
filled, which turns ``project_seed`` into "wait 60 seconds". So a miss is never
final: it re-fetches once and re-checks before raising. Hits are answered from
the cache, so the steady-state cost of the guard is zero round-trips.

ENGINE #2 ABSENT — DEGRADE, LOUDLY, TO THE SHAPE GATE
-----------------------------------------------------
``ProjectRegistryUnavailableError`` is what a registry that cannot be consulted
at all raises. Propagating that here would make every
memory and wiki write impossible on any deployment without engine #2 —
including the in-process test bypass, where ``YADGAR_EMBED_URL`` is unset and
``_forward_admin`` raises. That trade is not available: refusing to write the
user's content is a worse failure than accepting an unverifiable project_id.

So the unavailable branch WARNs and falls through to the shape gate, which
still runs. The shape gate is not nothing — it is the half that closes the
asymmetry finding 2 named: ``memorize(project="global")`` used to stamp the
ADR-0227 sentinel while ``memory_update`` (task 262) rejected it, i.e. the
CORRECTION path was stricter than the CREATION path. That half never needs a
database and therefore never degrades.
"""

from __future__ import annotations

import logging
import time

from yadgar._shared.observability.observe import observe
from yadgar._shared.storage.sql.errors import UnknownProjectError
from yadgar.core.server.tools._project_param import (
    InvalidProjectOverrideError,
    project_id_value_error,
)

logger = logging.getLogger(__name__)

#: How long a successfully fetched key set is trusted without re-fetching.
#: Only ever shortens the window in which a DELETED project keeps passing —
#: an ADDED one is picked up immediately by the forced refresh on a miss.
_REGISTRY_TTL_S = 60.0

#: How long an UNAVAILABLE registry is remembered as unavailable. Without it a
#: core process with no backend would pay a failing httpx connect on every
#: single write.
_UNAVAILABLE_TTL_S = 30.0

_cached_keys: frozenset[str] | None = None
_cached_at: float = 0.0
_unavailable_until: float = 0.0


@observe(exempt="pure module-global cache reset; no I/O, no storage access")
def invalidate_project_registry_cache() -> None:
    """Drop the cached key set (and any remembered unavailability)."""
    global _cached_keys, _cached_at, _unavailable_until
    _cached_keys = None
    _cached_at = 0.0
    _unavailable_until = 0.0


@observe(tier="boundary", metric="tools.project_registry.fetch")
def _fetch_registry_keys() -> frozenset[str] | None:
    """Fetch every registered ``project.key``, or ``None`` if unconsultable.

    ``None`` is the "could not check" answer and is deliberately distinct from
    an EMPTY frozenset, which means "checked, and the registry has no rows" —
    the two call for different responses (repair the deployment vs seed the
    registry) and collapsing them is the mistake
    ``ProjectRegistryUnavailableError`` exists to prevent.
    """
    from yadgar.core.forward import _forward_admin  # noqa: PLC0415

    try:
        result = _forward_admin("list_project_rows", {}, timeout_s=10.0)
    except Exception as exc:  # noqa: BLE001 — an unreachable backend must not block a write
        logger.warning("project registry unconsultable: %s", exc)
        return None
    if not isinstance(result, dict) or result.get("ok") is False:
        logger.warning(
            "project registry unconsultable: %s",
            (result or {}).get("error") if isinstance(result, dict) else result,
        )
        return None
    rows = result.get("rows")
    if not isinstance(rows, list):
        logger.warning("project registry returned no rows key: %r", result)
        return None
    return frozenset(str(row["key"]) for row in rows if isinstance(row, dict) and row.get("key"))


@observe(tier="stage", metric="tools.project_registry.known_ids")
def known_project_ids(*, refresh: bool = False) -> frozenset[str] | None:
    """Return the registered key set, or ``None`` when it cannot be consulted.

    Args:
        refresh: skip the TTL and the remembered-unavailable window. Used by
            the forced re-check on a miss, so a project registered a moment ago
            is never rejected by a stale cache.
    """
    global _cached_keys, _cached_at, _unavailable_until

    now = time.monotonic()
    if not refresh:
        if _cached_keys is not None and (now - _cached_at) < _REGISTRY_TTL_S:
            return _cached_keys
        if now < _unavailable_until:
            return None

    keys = _fetch_registry_keys()
    if keys is None:
        _unavailable_until = now + _UNAVAILABLE_TTL_S
        return None
    _cached_keys = keys
    _cached_at = now
    _unavailable_until = 0.0
    return keys


@observe(tier="stage", metric="tools.project_registry.assert_create")
def assert_project_registered_for_create(project_id: object, *, tool: str) -> None:
    """Gate a CREATE write on *project_id*. Returns cleanly, or raises.

    Two gates, and they fail differently on purpose:

    1. **Shape** — the ADR-0227 manufactured identities and the empty/non-string
       cases, via the same ``project_id_value_error`` the ``memory_update``
       restamp path uses (ledger task 262), so creation and correction read the
       one authority and cannot drift apart. Never touches the network.
    2. **Registry membership** — the real check. Degrades to a WARNING when the
       registry cannot be consulted (see the module docstring); a caller whose
       deployment has no engine #2 keeps its writes.

    Raises:
        InvalidProjectOverrideError: *project_id* is not a usable identity at
            the shape level. Raised for the SAME class the tool surface already
            maps to its error envelope, so no call site grows a new arm for it.
        UnknownProjectError: the registry was consulted and does not carry
            *project_id*. Same class ``MariaStorageEngine.assert_project_registered``
            raises, so ``except UnknownProjectError`` binds one class everywhere.
    """
    shape_error = project_id_value_error(project_id)
    if shape_error is not None:
        raise InvalidProjectOverrideError(f"{tool}: {shape_error}")

    keys = known_project_ids()
    if keys is None or project_id in keys:
        return
    # A miss is never final on a possibly-stale cache: re-fetch once, then decide.
    keys = known_project_ids(refresh=True)
    if keys is None or project_id in keys:
        return
    raise UnknownProjectError(str(project_id))


__all__ = [
    "assert_project_registered_for_create",
    "invalidate_project_registry_cache",
    "known_project_ids",
]
