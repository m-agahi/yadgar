"""Backend admin-op body for the ``drain_now`` op (task #29 — cold-drain fix).

RCA (docs/plans/wiki-cold-drain-rca): ``wiki_add(wait=True)`` / ``memorize(wait=True)``
nudge a ``drain_now()`` that only exists in the BACKEND process after the ADR-0078
core/backend split. In the CORE MCP process ``_st._queue_drainer is None``, so the
in-core nudge is a silent no-op and the caller passively waits on the backend's
30s-interval drainer — but the 15s wait budget expires first → ``wait_timeout``.

This op exposes the LIVE backend drainer's ``drain_now()`` over the generic
POST /admin seam so the core wait-path can nudge it cross-process (synchronous,
durable). Idempotent: ``drain_now`` is ``_drain_lock``-serialized inside the
drainer, so a mid-interval nudge just waits for the lock — never double-drains.
"""

from __future__ import annotations

import logging

from yadgar._shared.observability.observe import observe

logger = logging.getLogger(__name__)


@observe(tier="boundary", metric="backend.admin.drain_now")
def drain_now(payload: dict) -> dict:
    """Force an immediate synchronous drain pass on the live backend drainer.

    payload: ignored (no args — the drainer knows its own queue).

    Returns:
        {"drained": bool, "items_processed": int}
        drained=False + items_processed=0 when no live drainer is wired
        (op called before the drainer started, or a drain error) — best-effort:
        the core wait-path falls through to the passive poll on a soft result.
    """
    # Read the LIVE drainer off shared runtime state (set at
    # embed_service_lifecycle.py:139; survives importlib.reload(embed_service)).
    import yadgar._shared.runtime.state as _st  # noqa: PLC0415

    drainer = _st._queue_drainer
    if drainer is None:
        logger.warning("drain_now admin op: no live backend drainer wired (drained=False)")
        return {"drained": False, "items_processed": 0}

    try:
        processed = drainer.drain_now()
    except Exception as exc:  # noqa: BLE001 — best-effort nudge; never 500 the caller
        logger.warning("drain_now admin op: drain failed (non-fatal): %s", exc)
        return {"drained": False, "items_processed": 0}

    return {"drained": True, "items_processed": int(processed)}
