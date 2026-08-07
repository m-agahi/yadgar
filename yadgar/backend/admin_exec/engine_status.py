"""Backend admin-op body for ``sql_engine_status`` — "is engine #2 there at all?".

WHY THE HOST HAS TO ASK
-----------------------
The nightly cycle runs HOST-SIDE and every route from there to engine #2 is a
trap, for exactly the reasons ``admin_exec/backup_sql.py`` documents at length:
``client.cnf`` carries a CONTAINER-ABSOLUTE socket path, ``MariaStorageEngine``
construction is CONNECTIONLESS (so a host-built handle looks fine and can never
connect), and mariadbd runs ``--skip-networking``. A host-side inference —
"is there a ``mariadb/`` directory under the data root?" — would answer a
question about the FILESYSTEM while the caller needs an answer about the
PROCESS. The backend is the only party that knows whether ``_init_sql_storage``
actually composed an engine, so it is the party that gets asked.

WHY IT IS A PURE SLOT READ AND NOT A LIVENESS PROBE
---------------------------------------------------
This op reports one bit: did the composition root populate ``_st._sql_storage``.
It does NOT connect, ping, or run ``SELECT 1``. That restraint is the whole
design, not a shortcut.

The caller (``core/backup/quiesce.py``) turns ABSENT into "skip tonight's
cross-engine backup". So a probe that reported ABSENT for an engine that was
merely mid-restart — or whose socket was briefly busy — would silently disable
the backup on a host that HAS one, and every log line would still read green.
That is the vacuous-pass class the engine-#2 train exists to close, so the two
states are kept structurally apart instead:

* engine ABSENT           -> ``present: False`` here -> caller SKIPS.
* engine PRESENT, BROKEN  -> ``present: True`` here  -> caller proceeds and
  ``mariadb_dump`` HARD-FAILS on the real fault, loudly, with the real error.

A broken engine failing loudly is the correct outcome; a broken engine being
mistaken for an absent one is the outcome that loses backups.

ABSENCE IS NORMAL, NOT AN ERROR
-------------------------------
``_get_sql_storage`` deliberately does not assert (see its docstring): it is
None on CORE always (ADR-0078/ADR-0200 keep core off every database) and None on
the BACKEND whenever MariaDB did not come up — which ``entrypoint-backend.sh``
treats as a WARNING that leaves the container healthy. This op inherits that
stance: ``present: False`` is a successful answer, never a failure.
"""

from __future__ import annotations

import logging
from typing import Any

from yadgar._shared.observability.observe import observe

logger = logging.getLogger(__name__)

# The engine this op reports on. Named in the response rather than implied so a
# future second optional engine cannot silently reuse this answer.
ENGINE_NAME = "mariadb"


def _get_sql_engine() -> Any:
    """The composed ``MariaStorageEngine``, or None when engine #2 is absent.

    A FUNCTION rather than a direct call at the use site, mirroring
    ``invariants_cross_engine._get_sql_engine``: it is the one seam the tests
    patch, and the lazy import keeps this module off the ``sql`` extra (nothing
    here touches ``asyncmy``/``sqlalchemy`` — it reads a slot).
    """
    from yadgar._shared.runtime.lifecycle import _get_sql_storage  # noqa: PLC0415

    return _get_sql_storage()


@observe(tier="boundary", metric="backend.admin.sql_engine_status")
def sql_engine_status(payload: dict) -> dict:
    """Report whether engine #2 was composed in THIS process. Never raises.

    payload:
        Ignored entirely. The slot is the only input; accepting a key that could
        influence the answer would let a caller talk itself into a skip.

    Returns:
        ``{"ok": True, "present": bool, "engine": "mariadb"}``

    ``present`` is a bare ``is not None`` test against the composition slot. It
    is deliberately NOT evidence that the engine is reachable — see the module
    docstring for why proving reachability here would make the answer worse
    rather than better.
    """
    present = _get_sql_engine() is not None
    logger.debug(
        "engine #2 status probed",
        extra={
            "component": "admin.sql_engine_status",
            "action": "sql_engine_status",
            "outcome": "ok",
            "present": present,
        },
    )
    return {"ok": True, "present": present, "engine": ENGINE_NAME}
