"""v5.129.x — N+1 fix for _get_top_cooccurring_entities.

The old path issued one _get_adjacent(eid, None) call per content-entity (with
name enrichment) PLUS one get_entity_by_id call per unique neighbour — O(N²) on
a dense graph.  The fix collapses both loops:

  1. ONE _get_adjacent_batch call for all content-entity ids (name-free).
  2. ONE get_entities_by_ids call for all unique partner ids.

These tests pin the batch contract:

- _get_adjacent_batch called exactly once; _get_adjacent never called.
- get_entities_by_ids called at most once; get_entity_by_id never called.
- Output (top names sorted by weight, limited to ``limit``) is byte-identical
  to what the old per-entity path would produce.
- Content-entity ids excluded from partner counts (same filter as before).
- Empty / missing entities handled gracefully.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from yadgar.backend.retrieval.graph_helpers import _GraphHelpersMixin

# ── Harness ──────────────────────────────────────────────────────────────────


class _CooccurrHarness(_GraphHelpersMixin):
    """Minimal object that exposes the three attrs the mixin reads."""

    def __init__(self, storage, graph, settings):
        self._storage = storage
        self._graph = graph
        self._settings = settings


def _make_harness(
    content_entity_ids: set[int],
    adjacency: dict[int, list[dict]],
    entity_map: dict[int, dict],
    *,
    min_entity_length: int = 3,
) -> _CooccurrHarness:
    """Return a harness wired with controlled mocks.

    ``content_entity_ids`` simulates the output of ``_find_entities_in_content``.
    ``adjacency`` is what ``_get_adjacent_batch`` returns.
    ``entity_map`` is what ``get_entities_by_ids`` returns.
    """
    storage = MagicMock()
    graph = MagicMock()
    settings = MagicMock()

    settings.GRAPH_ENTITY_MIN_LENGTH = min_entity_length

    # _find_entities_in_content is called first; patch it to return our set.
    harness = _CooccurrHarness(storage, graph, settings)
    harness._find_entities_in_content = MagicMock(return_value=content_entity_ids)

    graph._get_adjacent_batch.return_value = adjacency
    storage.get_entities_by_ids.return_value = entity_map

    return harness


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestGetTopCooccurringEntitiesBatchContract:
    """Pins the N+1 → batch contract for _get_top_cooccurring_entities."""

    def test_batch_adjacency_called_once_not_per_entity(self):
        """_get_adjacent_batch called once for all content entities; _get_adjacent never."""
        harness = _make_harness(
            content_entity_ids={10, 20, 30},
            adjacency={10: [], 20: [], 30: []},
            entity_map={},
        )
        result = harness._get_top_cooccurring_entities("some content", limit=5)

        harness._graph._get_adjacent_batch.assert_called_once()
        harness._graph._get_adjacent.assert_not_called()
        assert result == []

    def test_entity_names_fetched_in_bulk_not_per_neighbor(self):
        """get_entities_by_ids called at most once; get_entity_by_id never called."""
        harness = _make_harness(
            content_entity_ids={1},
            adjacency={
                1: [
                    {"entity_id": 100, "weight": 2.0},
                    {"entity_id": 200, "weight": 1.0},
                ]
            },
            entity_map={
                100: {"id": 100, "name": "Partner1"},
                200: {"id": 200, "name": "Partner2"},
            },
        )
        harness._get_top_cooccurring_entities("content", limit=5)

        # Exactly one bulk fetch, never per-entity fallback.
        harness._storage.get_entities_by_ids.assert_called_once()
        harness._storage.get_entity_by_id.assert_not_called()

    def test_output_semantics_match_old_per_entity_path(self):
        """Top co-occurring names are sorted by weight sum, limited correctly."""
        harness = _make_harness(
            content_entity_ids={1, 2},
            adjacency={
                1: [
                    {"entity_id": 100, "weight": 3.0},  # Partner1 via entity 1
                    {"entity_id": 200, "weight": 1.0},  # Partner2 via entity 1
                ],
                2: [
                    {"entity_id": 100, "weight": 2.0},  # Partner1 again via entity 2
                    {"entity_id": 300, "weight": 5.0},  # Partner3 via entity 2
                ],
            },
            entity_map={
                100: {"id": 100, "name": "Partner1"},
                200: {"id": 200, "name": "Partner2"},
                300: {"id": 300, "name": "Partner3"},
            },
        )
        result = harness._get_top_cooccurring_entities("content", limit=2)

        # Partner1: 3.0+2.0=5.0; Partner3: 5.0; Partner2: 1.0.
        # Tie between Partner1 and Partner3 → both at 5.0, but only top-2.
        assert len(result) == 2
        assert set(result) == {"Partner1", "Partner3"}

    def test_content_entity_ids_excluded_from_partners(self):
        """Entities already in content_entities are not returned as co-occurrence partners."""
        harness = _make_harness(
            content_entity_ids={1},
            adjacency={
                1: [
                    {"entity_id": 1, "weight": 9.9},  # self — excluded
                    {"entity_id": 99, "weight": 1.0},  # new partner
                ]
            },
            entity_map={99: {"id": 99, "name": "ExternalPartner"}},
        )
        result = harness._get_top_cooccurring_entities("content", limit=5)

        assert result == ["ExternalPartner"]

    def test_no_content_entities_returns_empty(self):
        """When _find_entities_in_content returns empty set, result is []."""
        harness = _make_harness(
            content_entity_ids=set(),
            adjacency={},
            entity_map={},
        )
        result = harness._get_top_cooccurring_entities("content", limit=5)

        assert result == []
        harness._graph._get_adjacent_batch.assert_not_called()

    def test_missing_entity_rows_skipped_gracefully(self):
        """Partners whose entity row is missing from get_entities_by_ids are ignored."""
        harness = _make_harness(
            content_entity_ids={1},
            adjacency={1: [{"entity_id": 999, "weight": 5.0}]},
            entity_map={},  # 999 absent — simulate deleted/missing entity
        )
        result = harness._get_top_cooccurring_entities("content", limit=5)

        assert result == []

    def test_limit_respected(self):
        """Only the top ``limit`` partners by weight are returned."""
        adjacency = {1: [{"entity_id": 100 + i, "weight": float(10 - i)} for i in range(10)]}
        entity_map = {100 + i: {"id": 100 + i, "name": f"P{i}"} for i in range(10)}
        harness = _make_harness(
            content_entity_ids={1},
            adjacency=adjacency,
            entity_map=entity_map,
        )
        result = harness._get_top_cooccurring_entities("content", limit=3)

        assert len(result) == 3
        # P0 has weight 10.0 → highest → must be first.
        assert result[0] == "P0"
