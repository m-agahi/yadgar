"""Car C8 (0047 §5 C8) — the superseded-ADR exclusion set, loaded from SQL.

WHY THIS MODULE EXISTS AT ALL
-----------------------------
ADR-0206 puts ADR status in ONE place and rejects a second writer: the
``adr.status`` column in engine #2 (migration ``002_ledger_tables``, indexed by
``ix_adr_status``). **SurrealDB carries NOTHING about ADR status.** The
``adr-status:*`` wiki tag that still sits on pages today is precisely the second
writer ADR-0206 rejects, and the spine retires it — so reading status from
SurrealDB would be fast, obvious, and wrong. Nothing in this module touches it;
``tests/_shared/test_c8_superseded_adr_exclusion.py`` pins that with an AST
guard rather than trusting the comment.

WHERE THE LOOKUP IS ALLOWED TO LIVE — AND WHY IT IS NOT NEGOTIABLE
------------------------------------------------------------------
The seams are::

    embed_service_routes.recall_route        (async)
      → await asyncio.to_thread(_run_pipeline)
        → recall_pipeline._fanout_recall     (SYNC, in a worker thread)
          → sql/mariadb.py                   (asyncmy — ASYNC-ONLY)

Placing the status lookup anywhere downstream of the ``to_thread`` boundary
means driving an async-only driver from a sync worker thread, i.e. an
``asyncio.run`` per recall, i.e. an ``AsyncAdaptedQueuePool`` caching
connections bound to an event loop that dies with the thread. This repo has
written that hazard down twice already (``backend/admin_exec/invariants.py``
docstring, ``backend/embed_service/embed_service_lifecycle.py``). C8 closes it
**by placement**, not by handling: the load happens ONCE, in the async route,
upstream of the boundary, and travels down as plain data.

**Do not move this call downstream.**

WHY IT IS NOT A PER-CANDIDATE LOOKUP
------------------------------------
An earlier design filtered superseded ADRs out at pool assembly, keyed by the
slugs the providers had already returned. That is the exact defect C7 exists to
delete — C7's thesis is *"the limit is spent before filtering"*. Loading the
whole (tiny) set up front and pushing it into the stage-1 ``WHERE`` means a
superseded ADR never consumes a ``pool_limit`` slot, because it is never
fetched.

NO CACHE, DELIBERATELY
----------------------
The set is order-tens per project (14 superseded ADRs in yadgar), read through
an indexed, project-scoped query. A ledger-version cache is the obvious next
step and is deliberately NOT taken here: a cache introduces a staleness mode
that the C8 invariant would then have to detect, i.e. shipping the bug and its
detector in the same car. Follow-up, not this car.

FAILURE IS LOUD
---------------
Every failure path returns an EMPTY set — there is no way to fail closed
without breaking recall entirely — and an empty set is INVISIBLE at the call
site: superseded ADRs simply rank normally again and no result looks wrong. So
every such path logs at WARNING, and
``invariants_cross_engine.check_superseded_adr_exclusion`` cross-checks the
loader's output against its own independent SQL read on the nightly cycle.
"""

from __future__ import annotations

import logging
from typing import Any

from yadgar._shared.observability.observe import observe

logger = logging.getLogger(__name__)

SUPERSEDED_STATUS = "superseded"

# Car C8 free rider. C7 already built the opt-in arm ("excluded by default,
# returned when a caller explicitly asks"), which is ADR-0206's own sanctioned
# exclusion-with-opt-in escape hatch. Riding it costs nothing and lets ADR-0228
# NARROW ADR-0206 rather than overturn its partial-supersession counterexample:
# a superseded ADR whose surviving half still binds stays reachable by key
# (``adr_get``) AND by an explicit recall opt-in.
#
# The token is a plain word on purpose. Naming it ``adr-status:superseded``
# would make caller-supplied request data read like the forbidden SurrealDB tag
# to anyone reviewing this file.
SUPERSEDED_OPT_IN_TAG = "superseded"


@observe(tier="stage", metric="backend.retrieval.superseded.load")
async def load_superseded_slugs(engine: Any, *, project_id: str) -> tuple[str, ...]:
    """Return the wiki slugs of *project_id*'s superseded ADRs, from SQL.

    Reads ``adr`` rows with ``status='superseded'`` through the existing
    project-scoped ledger accessor and returns their ``body_slug`` values.

    ``body_slug`` is ``nullable=True`` in migration 002, so a superseded row
    that was never stamped has nothing to exclude BY. Those rows are skipped
    here (there is no slug to put in the predicate) and reported as a violation
    by the cross-engine invariant, which is the layer that can say "this row
    should be excludable and is not" without breaking the read path.

    Args:
        engine: The composed ``MariaStorageEngine``, or ``None`` when engine #2
            is absent (core-only install, or MariaDB did not come up).
        project_id: The caller's resolved project id.

    Returns:
        A sorted tuple of slugs. EMPTY on every failure path — see the module
        docstring on why each such path is logged at WARNING.
    """
    if engine is None:
        logger.warning(
            "superseded-ADR exclusion INACTIVE for %s: engine #2 is not composed — "
            "superseded ADRs will rank normally in recall",
            project_id,
        )
        return ()
    if not project_id:
        return ()

    try:
        rows = await engine.list_adr_rows(project_id=project_id, status=SUPERSEDED_STATUS)
    except Exception as exc:  # noqa: BLE001 — a read failure must not break recall
        logger.warning(
            "superseded-ADR exclusion INACTIVE for %s: ledger read failed (%s) — "
            "superseded ADRs will rank normally in recall",
            project_id,
            exc,
        )
        return ()

    slugs = sorted({str(row["body_slug"]) for row in rows if row.get("body_slug")})
    unstamped = sum(1 for row in rows if not row.get("body_slug"))
    if unstamped:
        logger.warning(
            "%d superseded ADR row(s) in %s carry no body_slug and cannot be "
            "excluded from recall; check_invariants reports the ids",
            unstamped,
            project_id,
        )
    return tuple(slugs)


__all__ = ["SUPERSEDED_OPT_IN_TAG", "SUPERSEDED_STATUS", "load_superseded_slugs"]
