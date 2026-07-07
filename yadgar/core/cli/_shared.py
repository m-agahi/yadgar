"""Shared helpers used by multiple CLI subcommands."""


def init_replay_lightweight(db_path=None):
    """Initialize only the engines needed for drain/restore (no daemons, no server).

    Phase 2b: Retriever construction removed (CLI recall island killed per §5.4 decision 4).
    CheckpointRestore.retriever is set to None — it never calls .recall() so this is safe.
    CLI replay that genuinely needs recall must route through the backend HTTP endpoint.
    """
    import logging

    # Suppress all library logging — hooks must only output data to stdout
    logging.disable(logging.CRITICAL)

    from yadgar._shared.cognitive_map import CognitiveMap
    from yadgar._shared.config import Settings
    from yadgar._shared.embeddings import EmbeddingEngine
    from yadgar._shared.knowledge_graph import KnowledgeGraph
    from yadgar._shared.metacognition import MetaCognition
    from yadgar._shared.restoration import CheckpointRestore
    from yadgar._shared.storage import StorageEngine

    settings = Settings()
    storage = StorageEngine(db_path or settings.DB_PATH)
    embeddings = EmbeddingEngine(settings.EMBEDDING_MODEL)
    kg = KnowledgeGraph(storage, settings)
    cognitive_map = CognitiveMap(storage, settings)
    metacognition = MetaCognition(storage, embeddings, kg, settings)

    replay = CheckpointRestore(
        storage=storage,
        embeddings=embeddings,
        retriever=None,  # CLI recall island killed — no local Retriever constructed
        cognitive_map=cognitive_map,
        metacognition=metacognition,
        settings=settings,
    )
    return storage, replay
