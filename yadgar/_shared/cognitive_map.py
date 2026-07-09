"""Successor Representation cognitive maps — retrieval as navigation through concept space.

Based on:
- Stachenfeld et al. (Nature Neuroscience 20:1643, 2017): SR in hippocampus
- Whittington et al. "Tolman-Eichenbaum Machine" (Cell 183:1249, 2020)
- Yan "External Hippocampus" (arXiv:2512.18190, 2025)

Key math:
  T[i,j] = P(access j right after i)
  M = (I - γ·T)^{-1}  — Successor Representation matrix
  M[i,j] = expected discounted future visits to j starting from i
  Eigenvectors of M ≈ "grid cell coordinates" in concept space
"""

import logging

import numpy as np

from yadgar._shared.config import Settings
from yadgar._shared.observability.observe import observe
from yadgar._shared.storage import StorageEngine

logger = logging.getLogger(__name__)

# Minimum transitions before SR is considered useful
_MIN_TRANSITIONS = 20


class CognitiveMap:
    """Navigate memory space via Successor Representation coordinates."""

    def __init__(self, storage: StorageEngine, settings: Settings) -> None:
        self._storage = storage
        self._discount = settings.SR_DISCOUNT  # γ
        self._lr = settings.SR_UPDATE_RATE  # TD learning rate
        self._sr_matrix: np.ndarray | None = None
        self._memory_index: dict[int, int] = {}  # memory_id → row index
        self._index_memory: dict[int, int] = {}  # row index → memory_id
        self._dirty = True

    # -- Recording --

    @observe(tier="boundary", metric="cognitive_map.record_transition")
    def record_transition(
        self, from_memory_id: int, to_memory_id: int, session_id: str = ""
    ) -> None:
        """Record that memory 'to' was accessed right after memory 'from'."""
        existing = self._storage.get_transition(from_memory_id, to_memory_id)
        if existing:
            self._storage.increment_transition(from_memory_id, to_memory_id)
        else:
            self._storage.insert_transition(
                {
                    "from_memory_id": from_memory_id,
                    "to_memory_id": to_memory_id,
                    "count": 1,
                    "session_id": session_id,
                }
            )
        self._dirty = True

    # -- Transition matrix --

    @observe(tier="stage", metric="cognitive_map.build_transition_matrix")
    def build_transition_matrix(self) -> np.ndarray:
        """Build row-normalized transition matrix T from stored transitions.

        T[i,j] = count(i→j) / sum(count(i→*))
        """
        raw_transitions = self._storage.get_all_transitions()
        if not raw_transitions:
            self._memory_index = {}
            self._index_memory = {}
            return np.zeros((0, 0), dtype=np.float64)

        # Filter out rows with None IDs (can arrive from SurrealDB NONE or
        # historical corrupt rows).  Coercing to 0 would map bad transitions
        # onto a phantom memory — skip instead.
        transitions = [
            t
            for t in raw_transitions
            if isinstance(t.get("from_memory_id"), int) and isinstance(t.get("to_memory_id"), int)
        ]

        if not transitions:
            self._memory_index = {}
            self._index_memory = {}
            return np.zeros((0, 0), dtype=np.float64)

        # Collect unique memory IDs
        ids: set[int] = set()
        for t in transitions:
            ids.add(t["from_memory_id"])
            ids.add(t["to_memory_id"])

        sorted_ids = sorted(ids)
        self._memory_index = {mid: idx for idx, mid in enumerate(sorted_ids)}
        self._index_memory = {idx: mid for mid, idx in self._memory_index.items()}

        n = len(sorted_ids)
        T = np.zeros((n, n), dtype=np.float64)

        for t in transitions:
            i = self._memory_index[t["from_memory_id"]]
            j = self._memory_index[t["to_memory_id"]]
            T[i, j] = t.get("count") or 0

        # Row-normalize
        row_sums = T.sum(axis=1, keepdims=True)
        row_sums[row_sums == 0] = 1.0  # avoid division by zero
        T = T / row_sums

        return T

    # -- SR matrix --

    @observe(tier="stage", metric="cognitive_map.compute_sr_matrix")
    def compute_sr_matrix(self) -> np.ndarray:
        """Compute M = (I - γ·T)^{-1}, the Successor Representation matrix."""
        T = self.build_transition_matrix()
        n = T.shape[0]

        if n == 0:
            self._sr_matrix = np.zeros((0, 0), dtype=np.float64)
            self._dirty = False
            return self._sr_matrix

        if n > 5000:
            # Iterative: M ≈ I + γT + γ²T² + ... (truncate at 20 terms)
            M = np.eye(n, dtype=np.float64)
            power = np.eye(n, dtype=np.float64)
            gamma_k = 1.0
            for _ in range(20):
                gamma_k *= self._discount
                power = power @ T
                M += gamma_k * power
        else:
            # Direct inversion with epsilon for numerical stability
            eps = 1e-10
            A = np.eye(n, dtype=np.float64) - self._discount * T
            A += eps * np.eye(n, dtype=np.float64)
            M = np.linalg.inv(A)

        self._sr_matrix = M
        self._dirty = False
        return M

    # -- Coordinate extraction --

    @observe(tier="stage", metric="cognitive_map.extract_coordinates")
    def extract_coordinates(self, n_dims: int = 2) -> dict[int, tuple]:
        """Extract low-dimensional coordinates from SR matrix eigenvectors.

        Returns {memory_id: (x, y, ...)} mapping.
        """
        if self._sr_matrix is None or self._dirty:
            self.compute_sr_matrix()

        M = self._sr_matrix
        # §13: SR matrix must be set after compute_sr_matrix — None means programming error
        if M is None:
            raise RuntimeError("CognitiveMap: SR matrix not initialized after compute_sr_matrix")
        if M.size == 0:
            return {}

        n = M.shape[0]
        n_dims = min(n_dims, n)

        # Symmetrize for real eigenvalues
        M_sym = (M + M.T) / 2.0

        eigenvalues, eigenvectors = np.linalg.eigh(M_sym)

        # Sort by eigenvalue magnitude descending
        order = np.argsort(-np.abs(eigenvalues))
        # Take top n_dims eigenvectors (columns of eigenvectors)
        top_vecs = eigenvectors[:, order[:n_dims]]  # shape (n, n_dims)

        coords: dict[int, tuple] = {}
        for idx in range(n):
            mid = self._index_memory[idx]
            coords[mid] = tuple(float(top_vecs[idx, d]) for d in range(n_dims))

        return coords

    # -- Navigation --

    @observe(tier="stage", metric="cognitive_map.navigate_to")
    def navigate_to(
        self,
        query_embedding: bytes,
        embeddings_engine,
        top_k: int = 10,
    ) -> list[tuple[int, float]]:
        """Project query into SR space and find nearest memories.

        Returns (memory_id, proximity_score) sorted by proximity descending.
        """
        coords = self.extract_coordinates(n_dims=2)
        if not coords:
            return []

        # Find top-5 most similar memories by embedding to seed SR position
        all_memory_ids = list(coords.keys())
        vec_hits = self._storage.search_vectors(
            query_embedding, top_k=min(5, len(all_memory_ids)), min_heat=0.0
        )

        if not vec_hits:
            return []

        # Average SR coordinates of embedding-similar memories
        seed_coords = []
        for mid, _dist in vec_hits:
            if mid in coords:
                seed_coords.append(np.array(coords[mid]))

        if not seed_coords:
            return []

        query_pos = np.mean(seed_coords, axis=0)

        # Find top_k nearest in SR space
        distances: list[tuple[int, float]] = []
        for mid, c in coords.items():
            dist = float(np.linalg.norm(np.array(c) - query_pos))
            proximity = 1.0 / (1.0 + dist)
            distances.append((mid, proximity))

        distances.sort(key=lambda x: x[1], reverse=True)
        return distances[:top_k]

    # -- Incremental TD update --

    @observe(tier="stage", metric="cognitive_map.incremental_update")
    def incremental_update(self, from_id: int, to_id: int) -> None:
        """TD-learning update: M[from] += lr * (e_to + γ·M[to] - M[from]).

        Only updates if both IDs are in the current index.
        """
        if self._sr_matrix is None or self._sr_matrix.size == 0:
            return
        if from_id not in self._memory_index or to_id not in self._memory_index:
            return

        i = self._memory_index[from_id]
        j = self._memory_index[to_id]
        n = self._sr_matrix.shape[0]

        e_to = np.zeros(n, dtype=np.float64)
        e_to[j] = 1.0

        delta = e_to + self._discount * self._sr_matrix[j] - self._sr_matrix[i]
        self._sr_matrix[i] += self._lr * delta

    @observe(tier="stage", metric="cognitive_map.has_sufficient_data")
    def has_sufficient_data(self) -> bool:
        """Check if enough transitions exist for meaningful SR computation."""
        transitions = self._storage.get_all_transitions()
        total_count = sum(t.get("count") or 0 for t in transitions) if transitions else 0
        return total_count >= _MIN_TRANSITIONS
