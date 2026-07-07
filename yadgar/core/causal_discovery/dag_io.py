"""DAG persistence, traversal helpers, and query methods."""

import logging
from collections import deque
from datetime import UTC, datetime

from yadgar._shared.observability.observe import observe
from yadgar._shared.storage import StorageEngine

logger = logging.getLogger(__name__)


@observe(tier="stage")
def traverse_oriented_edges(
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


@observe(tier="stage")
def store_dag_edges(
    storage: StorageEngine,
    dag: dict,
    algorithm: str,
) -> int:
    """Truncate-and-rebuild causal DAG edges for the given algorithm.

    Returns number of stored edges.
    """
    storage.clear_causal_dag_edges(algorithm=algorithm)

    now_iso = datetime.now(UTC).isoformat()
    stored_count = 0
    for source_name, target_name, confidence in dag["directed_edges"]:
        source_entity = storage.get_entity_by_name(source_name)
        target_entity = storage.get_entity_by_name(target_name)
        if source_entity and target_entity:
            storage.insert_causal_edge(
                {
                    "source_entity_id": source_entity["id"],
                    "target_entity_id": target_entity["id"],
                    "algorithm": algorithm,
                    "confidence": confidence,
                    "discovered_at": now_iso,
                }
            )
            stored_count += 1
    return stored_count
