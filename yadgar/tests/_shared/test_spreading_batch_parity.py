"""v5.102.0 — parity gate for the spreading-activation per-entity N+1 fetch fix.

`_spreading_bfs_step` previously called `_spreading_apply_activation` once per
newly-activated entity, and each call issued TWO serial round-trips:

  1. `get_entity_by_id(entity_id)`         (storage/entity.py)  — id → name
  2. `find_memory_ids_by_entity_name(name)` (storage/memory.py) — FTS on content

So ~136 activated entities × 2 round-trips ≈ 5 s (cProfile: 5.38 s = socket.recv,
294 serial round-trips). v5.99 batched ONLY the adjacency fetch; it left these two
per-entity loops un-batched. The v5.102 fix batches them PER BFS DEPTH:

  - one `get_entities_by_ids` for every entity discovered in the step, and
  - one `find_memory_ids_by_entities` (multi-statement single round-trip) for every
    name in the step,

then applies activation in the exact same discovery order. Because BFS is
level-synchronous, every entity discovered in one `_spreading_bfs_step` shares the
same `activation = spread_factor ** current_depth`, which is why per-depth batching
is exact-parity.

This module is the gate. It drives the REAL `spreading_activation` against a graph
seeded with memories whose content contains entity names, and asserts the batched
path yields the byte-identical `activated` dict (ids + scores) AND identical
sorted-output order as the retained per-entity baseline
(`_spreading_bfs_step_pernode`). If it ever fails, the fix perturbed ranking and
must NOT ship.
"""

import pytest

from yadgar._shared.config import Settings
from yadgar._shared.knowledge_graph import KnowledgeGraph
from yadgar._shared.storage import StorageEngine
from yadgar.backend.retrieval.core import Retriever


@pytest.fixture
def seeded(tmp_path):
    """Entity graph + memories exercising every parity-sensitive case.

    The seed memory mentions only "Alpha", so Alpha is the sole seed entity (depth 0).

    Graph (co_occurrence edges, weight 1.0):
        Alpha -> Bravo, Alpha -> Charlie   (Bravo, Charlie at depth 1)
        Bravo -> Charlie                   (Charlie already visited -> discovery order)
        Charlie -> Delta, Charlie -> Echo  (Delta, Echo at depth 2)
        Bravo -> Delta                     (Delta first reached via Bravo -> depth 2)

    Memories — content contains entity NAMES so `find_memory_ids_by_entity_name`
    (FTS on content) attributes them per entity:
        m(bravo):         hit by Bravo only (depth-1 activation)
        m(bravo_charlie): hit by Bravo AND Charlie (two same-depth entities -> same
                          activation, tests union + insertion order into `activated`)
        m(delta):         hit by Delta (depth-2 activation)
        m(echo):          hit by Echo only (depth 2)
        m(foxtrot):       matches NO entity name (must never appear in `activated`)
        m(seed):          contains the SEED entity name Alpha (Alpha is a seed entity,
                          never re-activated; and the seed memory itself is excluded
                          via seed_memory_set regardless)

    Entity names are unique multi-char tokens so FTS substring matches are exact and
    non-overlapping.
    """
    storage = StorageEngine(str(tmp_path / "spread_parity.db"))
    settings = Settings()
    graph = KnowledgeGraph(storage, settings)

    names = ["Alpha", "Bravo", "Charlie", "Delta", "Echo"]
    eids = {n: storage.insert_entity({"name": n, "type": "file"}) for n in names}

    edges = [
        ("Alpha", "Bravo", 1.0),
        ("Alpha", "Charlie", 1.0),
        ("Bravo", "Charlie", 1.0),
        ("Charlie", "Delta", 1.0),
        ("Charlie", "Echo", 1.0),
        ("Bravo", "Delta", 1.0),  # D reachable at depth 1 (via B) AND depth 2 (via C)
    ]
    for s, t, w in edges:
        storage.insert_relationship(
            {
                "source_entity_id": eids[s],
                "target_entity_id": eids[t],
                "relationship_type": "co_occurrence",
                "weight": w,
            }
        )

    # Memories whose content contains entity names.
    mids = {
        "seed": storage.insert_memory(
            {"content": "seed memory mentioning Alpha", "directory_context": "/x"}
        ),
        "bravo": storage.insert_memory(
            {"content": "note about Bravo internals", "directory_context": "/x"}
        ),
        "bravo_charlie": storage.insert_memory(
            {"content": "Bravo and Charlie interplay", "directory_context": "/x"}
        ),
        "delta": storage.insert_memory(
            {"content": "Delta pathway analysis", "directory_context": "/x"}
        ),
        "echo": storage.insert_memory({"content": "Echo leaf details", "directory_context": "/x"}),
        "foxtrot": storage.insert_memory(
            {"content": "Foxtrot unrelated content", "directory_context": "/x"}
        ),
    }

    yield storage, graph, settings, eids, mids
    storage.close()


def _run(retriever, seed_memories, settings, *, pernode: bool):
    """Drive spreading_activation, forcing the per-node baseline when pernode=True."""
    if pernode:
        # Monkeypatch the batched step to the retained per-node baseline for this run.
        orig = retriever._spreading_bfs_step
        retriever._spreading_bfs_step = retriever._spreading_bfs_step_pernode
        try:
            return retriever.spreading_activation(
                seed_memories,
                settings.GRAPH_SPREADING_DECAY,
                settings.GRAPH_SPREADING_MAX_DEPTH,
            )
        finally:
            retriever._spreading_bfs_step = orig
    return retriever.spreading_activation(
        seed_memories,
        settings.GRAPH_SPREADING_DECAY,
        settings.GRAPH_SPREADING_MAX_DEPTH,
    )


def test_spreading_apply_parity(seeded):
    """Batched per-depth apply == per-entity apply: identical activated ids + scores + order."""
    storage, graph, settings, _eids, mids = seeded
    retriever = Retriever(storage, None, graph, settings)

    seed_memories = [mids["seed"]]

    result_pernode = _run(retriever, seed_memories, settings, pernode=True)
    result_batched = _run(retriever, seed_memories, settings, pernode=False)

    # 1. Identical sorted output (ids + scores + ORDER, including tie-break order).
    assert result_pernode == result_batched, (
        f"batched spreading diverged from per-node baseline:\n"
        f"  pernode={result_pernode}\n  batched={result_batched}"
    )

    # 2. Identical activated dict (order-insensitive value check as a second gate).
    assert dict(result_pernode) == dict(result_batched)

    # 3. Sanity: the fixture must actually activate memories (else the test is vacuous).
    activated_ids = {mid for mid, _ in result_batched}
    assert activated_ids, "fixture activated nothing — parity assertion would be vacuous"

    # 4. Seed memory is excluded.
    assert mids["seed"] not in activated_ids
    # 5. Unrelated memory never activated.
    assert mids["foxtrot"] not in activated_ids
    # 6. Seed entity is Alpha (only name in the seed memory). Bravo/Charlie are at
    #    depth 1 (activation spread_factor**1); Delta/Echo at depth 2 (spread_factor**2).
    score_map = dict(result_batched)
    assert score_map[mids["bravo"]] == settings.GRAPH_SPREADING_DECAY**1, (
        "Bravo (depth 1) must carry spread_factor**1"
    )
    assert score_map[mids["delta"]] == settings.GRAPH_SPREADING_DECAY**2, (
        "Delta (depth 2) must carry spread_factor**2"
    )
    # 7. Memory hit by two same-depth entities (Bravo, Charlie) — same activation via
    #    `max`, present exactly once, at the depth-1 score.
    assert mids["bravo_charlie"] in activated_ids
    assert score_map[mids["bravo_charlie"]] == settings.GRAPH_SPREADING_DECAY**1
