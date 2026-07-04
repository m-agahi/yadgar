"""Single-writer facade for heat mutations (T4 — BC-C-SW1).

HeatWriter is the ONLY path that may call storage.batch_writes for heat-value
changes during a consolidation cycle.  All decay phases collect heat-change
intents (memory_id/entity_id → SQL + params) and hand them to
apply_heat_intents(), which merges the full set and issues a single
batch_writes call.

Invariant (BC-C-SW1):
    Exactly ONE storage.batch_writes call per consolidation cycle for heat.
    No other code path may call batch_writes with heat payloads during a cycle.
"""

from __future__ import annotations

import logging

from yadgar.observability.observe import observe

logger = logging.getLogger("yadgar.storage.heat_writer")


class HeatWriter:
    """Facade that collects heat intents and flushes them in a single batch.

    Args:
        storage: Any object with a ``batch_writes(statements)`` method,
                 typically a ``StorageEngine`` instance.
    """

    def __init__(self, storage) -> None:
        self._storage = storage

    @observe(tier="stage")
    def apply_heat_intents(self, intents: list[tuple[str, dict | None]]) -> None:
        """Flush all collected heat intents in a single batch_writes call.

        Args:
            intents: List of (sql, params) tuples — the full set of heat
                     mutations for one consolidation cycle (memories + entities
                     combined).  Empty list is a no-op (no storage call made).
        """
        if not intents:
            return
        logger.debug("HeatWriter: applying %d heat intent(s) in one batch", len(intents))
        self._storage.batch_writes(intents)
