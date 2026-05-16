"""Formal causal discovery using the PC algorithm for Yadgar.

Implements a simplified PC algorithm (Spirtes, Glymour, Scheines 2000)
to discover causal DAGs from coding session event logs. Uses numpy and
scipy.stats for conditional independence testing — no external causal
inference libraries required.
"""

import logging
import math
import re
from collections import deque
from datetime import UTC, datetime, timedelta
from itertools import combinations

import numpy as np
from scipy import stats

from yadgar.config import Settings
from yadgar.knowledge_graph import KnowledgeGraph
from yadgar.storage import StorageEngine

logger = logging.getLogger(__name__)


def _traverse_oriented_edges(
    start_id: int,
    start_name: str,
    storage: StorageEngine,
    direction: str,
    max_depth: int,
) -> list[dict]:
    """BFS traversal over oriented causal edges.

    direction='upstream'  → follow edges where current entity is TARGET
                           (returns causes of start entity).
    direction='downstream' → follow edges where current entity is SOURCE
                            (returns effects of start entity).

    Returns a list of dicts with keys: entity, confidence, depth, path.
    Sorted by (depth ASC, confidence DESC).
    """
    visited: set[int] = {start_id}
    results: list[dict] = []
    queue: deque[tuple[int, str, int, list[str]]] = deque()
    queue.append((start_id, start_name, 0, [start_name]))

    while queue:
        current_id, current_name, depth, path = queue.popleft()
        if depth >= max_depth:
            continue

        edges = storage.get_causal_edges_for_entity(current_id)
        if direction == "upstream":
            edges = [e for e in edges if e["target_entity_id"] == current_id]
            neighbor_key = "source_entity_id"
        else:
            edges = [e for e in edges if e["source_entity_id"] == current_id]
            neighbor_key = "target_entity_id"

        for edge in edges:
            neighbor_id = edge[neighbor_key]
            if neighbor_id not in visited:
                visited.add(neighbor_id)
                neighbor_entity = storage.get_entity_by_id(neighbor_id)
                neighbor_name = neighbor_entity["name"] if neighbor_entity else str(neighbor_id)
                new_path = path + [neighbor_name]
                results.append(
                    {
                        "entity": neighbor_name,
                        "confidence": edge["confidence"],
                        "depth": depth + 1,
                        "path": new_path,
                    }
                )
                queue.append((neighbor_id, neighbor_name, depth + 1, new_path))

    results.sort(key=lambda r: (r["depth"], -r["confidence"]))
    return results


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
        now = datetime.now(UTC)
        cutoff = now - timedelta(hours=hours)
        cutoff_iso = cutoff.isoformat()

        # Collect entities active since cutoff
        all_entities = self._storage.get_all_entities(min_heat=0.0, include_archived=True)

        # Collect episodes within the time range
        all_episodes = self._storage.get_episodes_since(0)
        episodes = [e for e in all_episodes if e.get("timestamp", "") >= cutoff_iso]
        episodes.sort(key=lambda e: e.get("timestamp", ""))

        if directory:
            episodes = [e for e in episodes if e["directory"] == directory]

        if not episodes:
            return np.zeros((0, 0)), [], []

        # Build time buckets (1-hour windows)
        timestamps: list[str] = []
        bucket_start = cutoff.replace(minute=0, second=0, microsecond=0)
        while bucket_start < now:
            timestamps.append(bucket_start.isoformat())
            bucket_start += timedelta(hours=1)

        if not timestamps:
            return np.zeros((0, 0)), [], []

        # Map entity names to column indices
        entity_names: list[str] = []
        entity_name_set: set[str] = set()

        # Collect entity mentions per episode
        episode_entities: list[tuple[str, list[str]]] = []
        for ep in episodes:
            content = ep["raw_content"]
            ep_entities: list[str] = []
            for ent in all_entities:
                name = ent["name"]
                if re.search(r"\b" + re.escape(name) + r"\b", content):
                    ep_entities.append(name)
                    if name not in entity_name_set:
                        entity_name_set.add(name)
                        entity_names.append(name)
            episode_entities.append((ep["timestamp"], ep_entities))

        if not entity_names:
            return np.zeros((0, 0)), [], []

        # Build the matrix
        n_windows = len(timestamps)
        n_vars = len(entity_names)
        name_to_col = {name: i for i, name in enumerate(entity_names)}
        data = np.zeros((n_windows, n_vars), dtype=np.float64)

        for ep_ts, ep_ents in episode_entities:
            # Find which time bucket this episode falls into
            try:
                ep_time = datetime.fromisoformat(ep_ts)
                if ep_time.tzinfo is None:
                    ep_time = ep_time.replace(tzinfo=UTC)
            except (ValueError, TypeError) as _e:
                continue
            bucket_idx = int(
                (ep_time - cutoff.replace(minute=0, second=0, microsecond=0)).total_seconds() / 3600
            )
            if 0 <= bucket_idx < n_windows:
                for name in ep_ents:
                    if name in name_to_col:
                        data[bucket_idx, name_to_col[name]] = 1.0

        return data, entity_names, timestamps

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
        n = len(x)
        if n < 4:
            return True  # Not enough data to determine dependence

        if z is None:
            # Unconditional: Pearson correlation test
            if np.std(x) < 1e-10 or np.std(y) < 1e-10:
                return True  # constant variable -> independence ill-defined
            r = np.corrcoef(x, y)[0, 1]
            if np.isnan(r):
                return True  # constant variable -> treat as independent
            denom = 1.0 - r * r
            if denom <= 0:
                return False  # perfect correlation -> dependent
            t_stat = r * math.sqrt((n - 2) / denom)
            p_value = 2.0 * stats.t.sf(abs(t_stat), df=n - 2)
        else:
            # Partial correlation: regress X on Z and Y on Z, correlate residuals
            if z.ndim == 1:
                z = z.reshape(-1, 1)

            # Add intercept column
            ones = np.ones((n, 1))
            Z = np.hstack([ones, z])

            # Compute residuals via least squares
            try:
                res_x = x - Z @ np.linalg.lstsq(Z, x, rcond=None)[0]
                res_y = y - Z @ np.linalg.lstsq(Z, y, rcond=None)[0]
            except np.linalg.LinAlgError:
                return True  # Singular matrix -> treat as independent

            # Check for zero-variance residuals
            if np.std(res_x) < 1e-10 or np.std(res_y) < 1e-10:
                return True

            r = np.corrcoef(res_x, res_y)[0, 1]
            if np.isnan(r):
                return True

            dof = n - 2 - z.shape[1]
            if dof < 1:
                return True  # Not enough degrees of freedom

            denom = 1.0 - r * r
            if denom <= 0:
                return False

            t_stat = r * math.sqrt(dof / denom)
            p_value = 2.0 * stats.t.sf(abs(t_stat), df=dof)

        return bool(p_value > alpha)

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
        for x in range(n_vars):
            if directed[x][i] and not adjacency[x][j]:
                directed[i][j] = True
                return True
        return False

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
        for z in range(n_vars):
            if directed[i][z] and directed[z][j]:
                directed[i][j] = True
                return True
        return False

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
        z_to_y = [
            z for z in range(n_vars) if z != i and z != j and adjacency[i][z] and directed[z][j]
        ]
        valid_pairs = any(
            not adjacency[z1][z2] for idx1, z1 in enumerate(z_to_y) for z2 in z_to_y[idx1 + 1 :]
        )
        if len(z_to_y) >= 2 and valid_pairs:
            directed[i][j] = True
            return True
        return False

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
        n_vars = data.shape[1]
        if n_vars < 2:
            return {
                "nodes": variable_names,
                "directed_edges": [],
                "undirected_edges": [],
                "separating_sets": {},
            }

        # Phase 1: Skeleton discovery
        # Start with complete undirected graph
        adjacency = [[True] * n_vars for _ in range(n_vars)]
        for i in range(n_vars):
            adjacency[i][i] = False

        sep_sets: dict[tuple[int, int], set[int]] = {}

        for k in range(max_cond_set + 1):
            edges_to_remove = []
            for i in range(n_vars):
                for j in range(i + 1, n_vars):
                    if not adjacency[i][j]:
                        continue

                    # Get neighbors of i (excluding j)
                    neighbors_i = [n for n in range(n_vars) if adjacency[i][n] and n != j]

                    if len(neighbors_i) < k:
                        continue

                    # Test all subsets of size k from neighbors of i
                    found_independent = False
                    for subset in combinations(neighbors_i, k):
                        if k == 0:
                            z = None
                        else:
                            z = data[:, list(subset)]

                        if self.conditional_independence_test(data[:, i], data[:, j], z, alpha):
                            edges_to_remove.append((i, j))
                            sep_sets[(i, j)] = set(subset)
                            sep_sets[(j, i)] = set(subset)
                            found_independent = True
                            break

                    if found_independent:
                        continue

                    # Also check from j's perspective
                    neighbors_j = [n for n in range(n_vars) if adjacency[j][n] and n != i]

                    if len(neighbors_j) < k:
                        continue

                    for subset in combinations(neighbors_j, k):
                        if k == 0:
                            z = None
                        else:
                            z = data[:, list(subset)]

                        if self.conditional_independence_test(data[:, i], data[:, j], z, alpha):
                            edges_to_remove.append((i, j))
                            sep_sets[(i, j)] = set(subset)
                            sep_sets[(j, i)] = set(subset)
                            found_independent = True
                            break

            for i, j in edges_to_remove:
                adjacency[i][j] = False
                adjacency[j][i] = False

        # Phase 2: Edge orientation
        # directed[i][j] = True means i -> j
        directed = [[False] * n_vars for _ in range(n_vars)]

        # Orient v-structures: X - Z - Y where X not adj Y, Z not in sep(X,Y)
        for z in range(n_vars):
            neighbors_z = [n for n in range(n_vars) if adjacency[z][n]]
            for xi, yi in combinations(neighbors_z, 2):
                x, y = (xi, yi) if xi < yi else (yi, xi)
                if adjacency[x][y]:
                    continue  # x and y are adjacent, skip

                sep = sep_sets.get((x, y), set())
                if z not in sep:
                    # Orient as x -> z <- y (v-structure)
                    directed[x][z] = True
                    directed[y][z] = True
                    # Remove reverse directions
                    directed[z][x] = False
                    directed[z][y] = False

        # Apply Meek's orientation rules iteratively
        changed = True
        while changed:
            changed = False

            for i in range(n_vars):
                for j in range(n_vars):
                    if i == j or not adjacency[i][j]:
                        continue
                    if directed[i][j] or directed[j][i]:
                        continue  # already oriented

                    # R1: If X -> Y and Y - Z and not X - Z, orient Y -> Z
                    if self._meek_r1(i, j, n_vars, adjacency, directed):
                        changed = True
                        continue

                    # R2: Acyclicity. If i -> z -> j and i - j (undirected),
                    # orient i -> j to avoid a directed cycle.
                    # Fix: use directed[z][j] (path z->j), not directed[j][z].
                    if self._meek_r2(i, j, n_vars, adjacency, directed):
                        changed = True
                        continue

                    # R3: If X - Y and X - Z1 and X - Z2 and Z1 -> Y and Z2 -> Y
                    # and Z1, Z2 are NOT adjacent to each other, orient X -> Y.
                    # Precondition: not adjacency[z1][z2] required to avoid cycles.
                    if self._meek_r3(i, j, n_vars, adjacency, directed):
                        changed = True

        # Build result
        directed_edges: list[tuple[str, str, float]] = []
        undirected_edges: list[tuple[str, str, float]] = []
        seen_undirected: set[tuple[int, int]] = set()

        for i in range(n_vars):
            for j in range(n_vars):
                if i == j or not adjacency[i][j]:
                    continue
                if directed[i][j]:
                    # Compute edge confidence from correlation strength
                    with np.errstate(divide="ignore", invalid="ignore"):
                        r = abs(np.corrcoef(data[:, i], data[:, j])[0, 1])
                    conf = float(r) if not np.isnan(r) else 0.5
                    directed_edges.append((variable_names[i], variable_names[j], round(conf, 4)))
                elif not directed[j][i]:
                    edge_key = (min(i, j), max(i, j))
                    if edge_key not in seen_undirected:
                        seen_undirected.add(edge_key)
                        with np.errstate(divide="ignore", invalid="ignore"):
                            r = abs(np.corrcoef(data[:, i], data[:, j])[0, 1])
                        conf = float(r) if not np.isnan(r) else 0.5
                        undirected_edges.append(
                            (variable_names[i], variable_names[j], round(conf, 4))
                        )

        # Convert separating sets to serializable form
        serializable_sep_sets: dict[str, list[str]] = {}
        for (i, j), s in sep_sets.items():
            if i < j:
                key = f"{variable_names[i]}|{variable_names[j]}"
                serializable_sep_sets[key] = [variable_names[k] for k in s]

        return {
            "nodes": variable_names,
            "directed_edges": directed_edges,
            "undirected_edges": undirected_edges,
            "separating_sets": serializable_sep_sets,
        }

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

        # Truncate-and-rebuild: delete old edges for this algorithm before
        # re-inserting so the table doesn't grow unboundedly across runs.
        self._storage.clear_causal_dag_edges(algorithm=algorithm)

        # Store directed edges in causal_dag_edges table
        now_iso = datetime.now(UTC).isoformat()
        stored_count = 0
        for source_name, target_name, confidence in dag["directed_edges"]:
            source_entity = self._storage.get_entity_by_name(source_name)
            target_entity = self._storage.get_entity_by_name(target_name)
            if source_entity and target_entity:
                self._storage.insert_causal_edge(
                    {
                        "source_entity_id": source_entity["id"],
                        "target_entity_id": target_entity["id"],
                        "algorithm": algorithm,
                        "confidence": confidence,
                        "discovered_at": now_iso,
                    }
                )
                stored_count += 1

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

    def query_causes(self, effect_entity: str, max_depth: int = 3) -> list[dict]:
        """Find causes of an effect by traversing the DAG upstream.

        BFS from the effect node following edges in reverse direction
        (target -> source means source is a cause of target).
        """
        target = self._storage.get_entity_by_name(effect_entity)
        if not target:
            return []
        return _traverse_oriented_edges(
            target["id"], effect_entity, self._storage, "upstream", max_depth
        )

    def query_effects(self, cause_entity: str, max_depth: int = 3) -> list[dict]:
        """Find effects by traversing the DAG downstream.

        BFS from the cause node following edges in forward direction
        (source -> target means target is an effect of source).
        """
        source = self._storage.get_entity_by_name(cause_entity)
        if not source:
            return []
        return _traverse_oriented_edges(
            source["id"], cause_entity, self._storage, "downstream", max_depth
        )

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
