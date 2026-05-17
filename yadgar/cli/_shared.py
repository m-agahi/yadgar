"""Shared helpers used by multiple CLI subcommands."""


def init_replay_lightweight(db_path=None):
    """Initialize only the engines needed for drain/restore (no daemons, no server)."""
    import logging

    # Suppress all library logging — hooks must only output data to stdout
    logging.disable(logging.CRITICAL)

    from yadgar.cognitive_map import CognitiveMap
    from yadgar.config import Settings
    from yadgar.embeddings import EmbeddingEngine
    from yadgar.knowledge_graph import KnowledgeGraph
    from yadgar.metacognition import MetaCognition
    from yadgar.restoration import CheckpointRestore
    from yadgar.retrieval import Retriever
    from yadgar.storage import StorageEngine

    settings = Settings()
    storage = StorageEngine(db_path or settings.DB_PATH)
    embeddings = EmbeddingEngine(settings.EMBEDDING_MODEL)
    kg = KnowledgeGraph(storage, settings)
    cognitive_map = CognitiveMap(storage, settings)
    retriever = Retriever(storage, embeddings, kg, settings)
    metacognition = MetaCognition(storage, embeddings, kg, settings)

    replay = CheckpointRestore(
        storage=storage,
        embeddings=embeddings,
        retriever=retriever,
        cognitive_map=cognitive_map,
        metacognition=metacognition,
        settings=settings,
    )
    return storage, replay
