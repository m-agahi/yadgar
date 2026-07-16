"""Causal DAG edge table CRUD.

_CausalMixin provides:
  - insert_causal_edge
  - get_causal_edges_for_entity
  - get_all_causal_edges
  - clear_causal_dag_edges

Used by v5.1 C1 (consolidation/causal.py) which calls storage.insert_causal_edge.
StorageEngine MRO must include _CausalMixin for that call to resolve.
"""

import logging

from yadgar._shared.observability.observe import observe

_log = logging.getLogger(__name__)


class _CausalMixin:
    """Causal DAG edges — mixed into StorageEngine."""

    # ------------------------------------------------------------------ Causal DAG Edges

    @observe(tier="stage")
    def insert_causal_edge(self, edge: dict) -> int:
        now = self._now_iso()
        eid = self._next_id("causal_dag_edge")
        # C3: optional source_memory_id for citation tracing (Zep parity).
        # Only include the field in the SET clause when a value is provided —
        # SurrealDB option<int> coercion rejects explicit NULL values.
        source_memory_id = edge.get("source_memory_id")
        # C1: bi-temporal validity. valid_from defaults to now(); caller may override.
        valid_from = edge.get("valid_from", now)
        params: dict = {
            "id": eid,
            "src": edge["source_entity_id"],
            "tgt": edge["target_entity_id"],
            "algo": edge.get("algorithm", "pc"),
            "conf": edge.get("confidence", 1.0),
            "discovered_at": edge.get("discovered_at", now),
            "is_validated": bool(edge.get("is_validated", False)),
            "vf": valid_from,
        }
        sql = (
            "CREATE type::record('causal_dag_edge', $id) SET "
            "source_entity_id = $src, target_entity_id = $tgt, "
            "algorithm = $algo, confidence = $conf, "
            "discovered_at = $discovered_at, is_validated = $is_validated, "
            "valid_from = $vf"
        )
        if source_memory_id is not None:
            sql += ", source_memory_id = $smid"
            params["smid"] = source_memory_id
        self._q(sql, params)
        return eid

    def get_causal_edges_for_entity(self, entity_id: int) -> list[dict]:
        rows = self._q(
            "SELECT * FROM causal_dag_edge "
            "WHERE source_entity_id = $eid OR target_entity_id = $eid",
            {"eid": entity_id},
        )
        return self._rows_to_dicts(rows)

    @observe(tier="stage")
    def get_all_causal_edges(
        self, include_invalidated: bool = False, as_of: str | None = None, limit: int = 0
    ) -> list[dict]:
        """Return causal DAG edges.

        include_invalidated (C1): when False (default), excludes rows whose
        valid_until is set (i.e. closed/superseded). Ignored when as_of is set.

        as_of (v5.29.0): ISO-8601 timestamp. When provided, returns edges valid
        at that point in time via as_of_filter. Default None = current state.

        limit (viz-render-perf, Car A): 0/-1 = unlimited. Applied AFTER the existing
        ``ORDER BY confidence DESC`` so a capped subset is the strongest edges.
        Default unlimited keeps non-viz callers + the uncapped precompute unchanged.
        """
        _lim = " LIMIT $lim" if limit and limit > 0 else ""
        _params = {"lim": limit} if _lim else {}
        if as_of is not None:
            from yadgar._shared.storage.bitemporal import as_of_filter

            clause = as_of_filter("causal_dag_edge", as_of=as_of)
            sql = f"SELECT * FROM causal_dag_edge WHERE 1=1{clause} ORDER BY confidence DESC{_lim}"
        elif include_invalidated:
            sql = f"SELECT * FROM causal_dag_edge ORDER BY confidence DESC{_lim}"
        else:
            sql = (
                "SELECT * FROM causal_dag_edge WHERE valid_until IS NONE "
                f"ORDER BY confidence DESC{_lim}"
            )
        rows = self._q(sql, _params) if _params else self._q(sql)
        return self._rows_to_dicts(rows)

    @observe(tier="stage")
    def clear_causal_dag_edges(self, algorithm: str | None = None) -> int:
        """Delete causal DAG edges, optionally filtered by algorithm.

        Called by discover_dag before re-inserting so the table is
        truncate-and-rebuild rather than append-only.

        Returns the number of rows deleted.
        """
        if algorithm is not None:
            count_rows = self._q(
                "SELECT count() AS c FROM causal_dag_edge WHERE algorithm = $algo GROUP ALL",
                {"algo": algorithm},
            )
            n = int(count_rows[0]["c"]) if count_rows and count_rows[0].get("c") else 0
            self._q(
                "DELETE FROM causal_dag_edge WHERE algorithm = $algo",
                {"algo": algorithm},
            )
        else:
            count_rows = self._q("SELECT count() AS c FROM causal_dag_edge GROUP ALL")
            n = int(count_rows[0]["c"]) if count_rows and count_rows[0].get("c") else 0
            self._q("DELETE FROM causal_dag_edge")
        return n
