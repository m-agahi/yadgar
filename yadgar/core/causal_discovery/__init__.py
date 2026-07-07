"""Formal causal discovery using the PC algorithm for Yadgar.

Implements a simplified PC algorithm (Spirtes, Glymour, Scheines 2000)
to discover causal DAGs from coding session event logs. Uses numpy and
scipy.stats for conditional independence testing — no external causal
inference libraries required.
"""

import logging

import numpy as np

from yadgar._shared.config import Settings
from yadgar._shared.knowledge_graph import KnowledgeGraph
from yadgar._shared.observability.observe import observe
from yadgar._shared.storage import StorageEngine
from yadgar.core.causal_discovery.dag_io import store_dag_edges, traverse_oriented_edges
from yadgar.core.causal_discovery.independence import conditional_independence_test
from yadgar.core.causal_discovery.meek import meek_r1, meek_r2, meek_r3
from yadgar.core.causal_discovery.pc import build_event_matrix, pc_algorithm

logger = logging.getLogger(__name__)

# Re-export for backward compatibility — tests import this directly
_traverse_oriented_edges = traverse_oriented_edges

__all__ = [
    "CausalDiscovery",
    "_traverse_oriented_edges",
]


class CausalDiscovery:
    """Discovers causal structure from observational coding event data.

    Collects structured events (file changes, errors, decisions) from
    the knowledge graph and entity store, builds a time-aligned binary
    event matrix, then runs the PC algorithm to discover which variables
    causally influence which others.
    """

    def __init__(
        self,
        storage: StorageEngine,
        knowledge_graph: KnowledgeGraph,
        settings: Settings,
    ) -> None:
        self._storage = storage
        self._kg = knowledge_graph
        self._settings = settings

    def build_event_matrix(
        self, directory: str | None = None, hours: int = 168
    ) -> tuple[np.ndarray, list[str], list[str]]:
        """Build a time-aligned binary event matrix from recent activity.

        Rows = 1-hour time windows, Columns = entity variables.
        Values = 1 if entity was active in that window, 0 otherwise.

        Returns (data_matrix, variable_names, timestamps).
        """
        return build_event_matrix(self._storage, self._settings, directory=directory, hours=hours)

    def conditional_independence_test(
        self,
        x: np.ndarray,
        y: np.ndarray,
        z: np.ndarray | None = None,
        alpha: float = 0.05,
    ) -> bool:
        """Test if X is independent of Y given Z.

        Returns True if independent (p_value > alpha), False if dependent.
        """
        return conditional_independence_test(x, y, z, alpha)

    def _meek_r1(
        self,
        i: int,
        j: int,
        n_vars: int,
        adjacency: list[list[bool]],
        directed: list[list[bool]],
    ) -> bool:
        """Meek R1 (non-collider): if X->i and i-j and X not adj j, orient i->j.

        Returns True if the edge was oriented.
        """
        return meek_r1(i, j, n_vars, adjacency, directed)

    def _meek_r2(
        self,
        i: int,
        j: int,
        n_vars: int,
        adjacency: list[list[bool]],
        directed: list[list[bool]],
    ) -> bool:
        """Meek R2 (acyclicity): if i->z->j and i-j undirected, orient i->j.

        Returns True if the edge was oriented.
        """
        return meek_r2(i, j, n_vars, adjacency, directed)

    def _meek_r3(
        self,
        i: int,
        j: int,
        n_vars: int,
        adjacency: list[list[bool]],
        directed: list[list[bool]],
    ) -> bool:
        """Meek R3 (non-adjacent): if i-z1, i-z2, z1->j, z2->j, z1 not adj z2, orient i->j.

        Returns True if the edge was oriented.
        """
        return meek_r3(i, j, n_vars, adjacency, directed)

    def pc_algorithm(
        self,
        data: np.ndarray,
        variable_names: list[str],
        alpha: float = 0.05,
        max_cond_set: int = 3,
    ) -> dict:
        """Run the PC algorithm to discover causal structure.

        Phase 1: Skeleton discovery — remove edges where conditional
        independence is detected.
        Phase 2: Edge orientation — orient v-structures and apply
        Meek's rules.

        Returns dict with nodes, directed_edges, undirected_edges,
        and separating_sets.
        """
        return pc_algorithm(data, variable_names, alpha, max_cond_set)

    @observe(tier="boundary")
    def discover_dag(
        self,
        directory: str | None = None,
        algorithm: str = "pc",
        hours: int = 168,
    ) -> dict:
        """Build event matrix and run causal discovery.

        Returns the discovered DAG with metadata, and stores directed
        edges in the causal_dag_edges table.
        """
        data, variable_names, timestamps = self.build_event_matrix(directory=directory, hours=hours)

        n_vars = len(variable_names)
        n_windows = len(timestamps)

        # Minimum data requirements
        if n_vars < 5 or n_windows < 10:
            return {
                "nodes": variable_names,
                "directed_edges": [],
                "undirected_edges": [],
                "separating_sets": {},
                "metadata": {
                    "algorithm": algorithm,
                    "variables": n_vars,
                    "time_windows": n_windows,
                    "status": "insufficient_data",
                    "reason": f"Need >= 5 variables and >= 10 time windows "
                    f"(got {n_vars} vars, {n_windows} windows)",
                },
            }

        dag = self.pc_algorithm(data, variable_names)

        stored_count = store_dag_edges(self._storage, dag, algorithm)

        dag["metadata"] = {
            "algorithm": algorithm,
            "variables": n_vars,
            "time_windows": n_windows,
            "directed_count": len(dag["directed_edges"]),
            "undirected_count": len(dag["undirected_edges"]),
            "stored_edges": stored_count,
            "status": "completed",
        }
        logger.info(
            "discover_dag: algorithm=%s, variables=%d, windows=%d, directed=%d, undirected=%d, stored=%d",
            algorithm,
            n_vars,
            n_windows,
            len(dag["directed_edges"]),
            len(dag["undirected_edges"]),
            stored_count,
        )

        return dag

    @observe(tier="boundary")
    def query_causes(self, effect_entity: str, max_depth: int = 3) -> list[dict]:
        """Find causes of an effect by traversing the DAG upstream.

        BFS from the effect node following edges in reverse direction
        (target -> source means source is a cause of target).
        """
        target = self._storage.get_entity_by_name(effect_entity)
        if not target:
            return []
        return traverse_oriented_edges(
            target["id"], effect_entity, self._storage, "upstream", max_depth
        )

    @observe(tier="boundary")
    def query_effects(self, cause_entity: str, max_depth: int = 3) -> list[dict]:
        """Find effects by traversing the DAG downstream.

        BFS from the cause node following edges in forward direction
        (source -> target means target is an effect of source).
        """
        source = self._storage.get_entity_by_name(cause_entity)
        if not source:
            return []
        return traverse_oriented_edges(
            source["id"], cause_entity, self._storage, "downstream", max_depth
        )

    @observe(tier="boundary")
    def get_causal_chain(self, entity: str) -> dict:
        """Return both causes and effects for an entity."""
        causes = self.query_causes(entity)
        effects = self.query_effects(entity)

        all_edges = self._storage.get_all_causal_edges()

        return {
            "entity": entity,
            "causes": causes,
            "effects": effects,
            "dag_edges_total": len(all_edges),
        }
