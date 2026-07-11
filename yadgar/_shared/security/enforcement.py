"""Enforcement helpers (_shared-clean).

Moved here in R3 Car 1 so core (wiki tool) and backend (queue drainer DLQ)
both import from _shared, not across the core<->backend boundary.
"""

from __future__ import annotations

import os

from yadgar._shared.observability.observe import observe

_FALSY = frozenset({"false", "0", "no", "off"})


@observe(tier="hot", metric="drainer.dlq.enforcement_on")
def _enforcement_on(env_var: str) -> bool:
    """Return True (enforcement ON) unless env var is explicitly falsy.

    Fail-safe: unknown/garbage values default to True (ON).
    """
    val = os.environ.get(env_var, "").strip().lower()
    return val not in _FALSY


def _inc_relaxed(enforcement: str) -> None:
    """Increment yadgar_writes_with_enforcement_relaxed counter. Never raises."""
    try:
        from yadgar._shared.observability.metrics import (
            yadgar_writes_with_enforcement_relaxed,  # noqa: PLC0415
        )

        yadgar_writes_with_enforcement_relaxed.labels(enforcement=enforcement).inc()
    except Exception:
        pass
