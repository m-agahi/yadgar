"""Bi-temporal edge helpers — C1 v5.3.4 (Zep parity) + Adopt-3 v5.29.0.

Provides:
  - invalidate_edge(storage, edge_table, edge_id, reason) → sets valid_until = now()
  - as_of_filter(table, as_of) → SQL WHERE-fragment for point-in-time queries

Design: NEVER deletes. Superseded edges are closed by setting valid_until.
Callers decide WHEN to invalidate (domain-specific); this module provides the how.
"""

from __future__ import annotations

import logging

from yadgar.observability.observe import observe

_log = logging.getLogger(__name__)

# Edge tables that carry bi-temporal validity columns.
# v5.29.0 (Adopt-3): extended with user_profile and derived_belief.
_VALID_EDGE_TABLES = frozenset(
    {
        "causal_dag_edge",
        "relationship",
        "memory_similarity_link",
        "user_profile",  # NEW v5.29.0
        "derived_belief",  # NEW v5.29.0
    }
)


@observe(tier="stage")
def invalidate_edge(
    storage,
    edge_table: str,
    edge_id: int,
    reason: str | None = None,
) -> None:
    """Close a KG edge by setting valid_until = now().

    Args:
        storage: StorageEngine instance.
        edge_table: One of the recognised bi-temporal tables.
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


@observe(tier="hot")
def as_of_filter(table: str, as_of: str | None = None) -> str:  # noqa: ARG001
    """Return a SQL WHERE-fragment selecting rows valid at ``as_of``.

    Args:
        table: The bi-temporal table name (informational; not interpolated into SQL).
        as_of: ISO-8601 timestamp string, or None for current state.

    Returns:
        A string beginning with " AND " ready to splice after an existing WHERE clause.

        as_of=None  → currently-valid rows:
            valid_until IS NONE OR valid_until > <now>

        as_of=<ts>  → historically-valid at ts:
            valid_from <= <ts> AND (valid_until IS NONE OR valid_until > <ts>)

    Dates are compared as ISO-8601 strings (lexicographic order works for UTC timestamps).
    Rows without valid_from are excluded from historical queries to avoid false positives
    (they were inserted before the bi-temporal migration and have unknown validity start).
    """
    if as_of is None:
        # Current state: row has no close timestamp, or close timestamp in the future.
        return " AND (valid_until IS NONE OR valid_until > time::now())"
    # Historical state: row was valid at the requested timestamp.
    return (
        f" AND valid_from IS NOT NONE"
        f" AND valid_from <= '{as_of}'"
        f" AND (valid_until IS NONE OR valid_until > '{as_of}')"
    )
