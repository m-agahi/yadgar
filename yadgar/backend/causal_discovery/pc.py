"""PC algorithm implementation for causal skeleton and v-structure orientation."""

import logging
import re
from datetime import UTC, datetime, timedelta
from itertools import combinations

import numpy as np

from yadgar._shared.config import Settings
from yadgar._shared.observability.observe import observe
from yadgar._shared.storage import StorageEngine
from yadgar.backend.causal_discovery.independence import conditional_independence_test
from yadgar.backend.causal_discovery.meek import meek_r1, meek_r2, meek_r3

logger = logging.getLogger(__name__)


@observe(tier="stage")
def _fetch_filtered_episodes(
    storage: StorageEngine,
    cutoff_iso: str,
    project_id: str | None,
) -> list[dict]:
    """Return episodes since cutoff, sorted by timestamp, optionally filtered by project_id."""
    all_episodes = storage.get_episodes_since(0)
    episodes = [e for e in all_episodes if e.get("timestamp", "") >= cutoff_iso]
    episodes.sort(key=lambda e: e.get("timestamp", ""))
    if project_id:
        episodes = [e for e in episodes if e["directory"] == project_id]
    return episodes


@observe(tier="stage")
def _build_time_buckets(cutoff: datetime, now: datetime) -> list[str]:
    """Return ISO-formatted 1-hour bucket starts from cutoff to now."""
    timestamps: list[str] = []
    bucket_start = cutoff.replace(minute=0, second=0, microsecond=0)
    while bucket_start < now:
        timestamps.append(bucket_start.isoformat())
        bucket_start += timedelta(hours=1)
    return timestamps


@observe(tier="stage")
def _scan_entity_mentions(
    episodes: list[dict],
    all_entities: list[dict],
) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """Scan each episode for entity name mentions.

    Returns (entity_names, episode_entities) where episode_entities is a list
    of (timestamp, [entity_names_found]) pairs.
    """
    entity_names: list[str] = []
    entity_name_set: set[str] = set()
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

    return entity_names, episode_entities


@observe(tier="stage")
def _fill_event_matrix(
    episode_entities: list[tuple[str, list[str]]],
    entity_names: list[str],
    timestamps: list[str],
    bucket_origin: datetime,
) -> np.ndarray:
    """Fill and return a binary (n_windows x n_vars) event matrix."""
    n_windows = len(timestamps)
    n_vars = len(entity_names)
    name_to_col = {name: i for i, name in enumerate(entity_names)}
    data = np.zeros((n_windows, n_vars), dtype=np.float64)

    for ep_ts, ep_ents in episode_entities:
        try:
            ep_time = datetime.fromisoformat(ep_ts)
            if ep_time.tzinfo is None:
                ep_time = ep_time.replace(tzinfo=UTC)
        except (ValueError, TypeError):  # fmt: skip
            continue
        bucket_idx = int((ep_time - bucket_origin).total_seconds() / 3600)
        if 0 <= bucket_idx < n_windows:
            for name in ep_ents:
                if name in name_to_col:
                    data[bucket_idx, name_to_col[name]] = 1.0

    return data


@observe(tier="stage")
def build_event_matrix(
    storage: StorageEngine,
    settings: Settings,
    project_id: str | None = None,
    hours: int = 168,
) -> tuple[np.ndarray, list[str], list[str]]:
    """Build a time-aligned binary event matrix from recent activity.

    Rows = 1-hour time windows, Columns = entity variables.
    Values = 1 if entity was active in that window, 0 otherwise.

    Returns (data_matrix, variable_names, timestamps).
    """
    now = datetime.now(UTC)
    cutoff = now - timedelta(hours=hours)
    cutoff_iso = cutoff.isoformat()

    all_entities = storage.get_all_entities(min_heat=0.0, include_archived=True)
    episodes = _fetch_filtered_episodes(storage, cutoff_iso, project_id)

    if not episodes:
        return np.zeros((0, 0)), [], []

    timestamps = _build_time_buckets(cutoff, now)

    if not timestamps:
        return np.zeros((0, 0)), [], []

    entity_names, episode_entities = _scan_entity_mentions(episodes, all_entities)

    if not entity_names:
        return np.zeros((0, 0)), [], []

    bucket_origin = cutoff.replace(minute=0, second=0, microsecond=0)
    data = _fill_event_matrix(episode_entities, entity_names, timestamps, bucket_origin)

    return data, entity_names, timestamps


@observe(tier="stage")
def pc_algorithm(
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

                    if conditional_independence_test(data[:, i], data[:, j], z, alpha):
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

                    if conditional_independence_test(data[:, i], data[:, j], z, alpha):
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
                if meek_r1(i, j, n_vars, adjacency, directed):
                    changed = True
                    continue

                # R2: Acyclicity. If i -> z -> j and i - j (undirected),
                # orient i -> j to avoid a directed cycle.
                # Fix: use directed[z][j] (path z->j), not directed[j][z].
                if meek_r2(i, j, n_vars, adjacency, directed):
                    changed = True
                    continue

                # R3: If X - Y and X - Z1 and X - Z2 and Z1 -> Y and Z2 -> Y
                # and Z1, Z2 are NOT adjacent to each other, orient X -> Y.
                # Precondition: not adjacency[z1][z2] required to avoid cycles.
                if meek_r3(i, j, n_vars, adjacency, directed):
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
                    undirected_edges.append((variable_names[i], variable_names[j], round(conf, 4)))

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
