"""Bi-temporal edge helpers — C1 v5.3.4 (Zep parity).

Provides:
  - invalidate_edge(storage, edge_table, edge_id, reason) → sets valid_until = now()

Design: NEVER deletes. Superseded edges are closed by setting valid_until.
Callers decide WHEN to invalidate (domain-specific); this module provides the how.
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)

# Edge tables that carry bi-temporal validity columns.
_VALID_EDGE_TABLES = frozenset({"causal_dag_edge", "relationship", "memory_similarity_link"})


def invalidate_edge(
    storage,
    edge_table: str,
    edge_id: int,
    reason: str | None = None,
) -> None:
    """Close a KG edge by setting valid_until = now().

    Args:
        storage: StorageEngine instance.
        edge_table: One of causal_dag_edge, relationship, memory_similarity_link.
        edge_id: Integer primary key of the row to invalidate.
        reason: Optional human-readable reason (logged, not stored — schema is lean).

    Raises:
        ValueError: if edge_table is not in the allowed set (injection guard).
    """
    if edge_table not in _VALID_EDGE_TABLES:
        raise ValueError(
            f"invalidate_edge: '{edge_table}' is not a recognised edge table. "
            f"Allowed: {sorted(_VALID_EDGE_TABLES)}"
        )
    now = storage._now_iso()
    storage._q(
        f"UPDATE type::record('{edge_table}', $id) SET valid_until = $ts",
        {"id": int(edge_id), "ts": now},
    )
    if reason:
        _log.info(
            "invalidate_edge: table=%s id=%s reason=%r ts=%s",
            edge_table,
            edge_id,
            reason,
            now,
        )
    else:
        _log.info("invalidate_edge: table=%s id=%s ts=%s", edge_table, edge_id, now)
