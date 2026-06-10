"""Tests for yadgar/cli/_shared.py — shared engine-init helper.

Wave 5 coverage: yadgar/cli/_shared.py (20 stmts, 0% pre-wave).
Strategy: all heavy imports (CognitiveMap, EmbeddingEngine, KnowledgeGraph,
MetaCognition, CheckpointRestore, Retriever, StorageEngine, Settings) are
lazy inside init_replay_lightweight — patch at the yadgar.* module level.
_shared.py also calls logging.disable so patch logging too.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from yadgar.cli._shared import init_replay_lightweight

# ---------------------------------------------------------------------------
# Shared mock setup
# ---------------------------------------------------------------------------

_PATCH_TARGETS = {
    "yadgar.config.Settings": None,
    "yadgar.storage.StorageEngine": None,
    "yadgar.embeddings.EmbeddingEngine": None,
    "yadgar.knowledge_graph.KnowledgeGraph": None,
    "yadgar.cognitive_map.CognitiveMap": None,
    "yadgar.retrieval.Retriever": None,
    "yadgar.metacognition.MetaCognition": None,
    "yadgar.restoration.CheckpointRestore": None,
}


def _build_mocks():
    """Return a dict of mock instances for each patched class."""
    settings = MagicMock()
    settings.DB_PATH = "/tmp/test.db"
    settings.EMBEDDING_MODEL = "test-model"

    storage = MagicMock()
    embeddings = MagicMock()
    kg = MagicMock()
    cmap = MagicMock()
    retriever = MagicMock()
    metacog = MagicMock()
    replay = MagicMock()

    return settings, storage, embeddings, kg, cmap, retriever, metacog, replay


def _patch_all(settings, storage, embeddings, kg, cmap, retriever, metacog, replay):
    """Return a list of patch context managers."""
    return [
        patch("yadgar.config.Settings", return_value=settings),
        patch("yadgar.storage.StorageEngine", return_value=storage),
        patch("yadgar.embeddings.EmbeddingEngine", return_value=embeddings),
        patch("yadgar.knowledge_graph.KnowledgeGraph", return_value=kg),
        patch("yadgar.cognitive_map.CognitiveMap", return_value=cmap),
        patch("yadgar.retrieval.Retriever", return_value=retriever),
        patch("yadgar.metacognition.MetaCognition", return_value=metacog),
        patch("yadgar.restoration.CheckpointRestore", return_value=replay),
    ]


# ---------------------------------------------------------------------------
# init_replay_lightweight — return value
# ---------------------------------------------------------------------------


class TestInitReplayLightweightReturnValue:
    def test_returns_tuple_of_two(self):
        mocks = _build_mocks()
        with (
            patch("yadgar.config.Settings", return_value=mocks[0]),
            patch("yadgar.storage.StorageEngine", return_value=mocks[1]),
            patch("yadgar.embeddings.EmbeddingEngine", return_value=mocks[2]),
            patch("yadgar.knowledge_graph.KnowledgeGraph", return_value=mocks[3]),
            patch("yadgar.cognitive_map.CognitiveMap", return_value=mocks[4]),
            patch("yadgar.retrieval.Retriever", return_value=mocks[5]),
            patch("yadgar.metacognition.MetaCognition", return_value=mocks[6]),
            patch("yadgar.restoration.CheckpointRestore", return_value=mocks[7]),
        ):
            result = init_replay_lightweight()
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_first_element_is_storage(self):
        mocks = _build_mocks()
        settings, storage, embeddings, kg, cmap, retriever, metacog, replay = mocks
        with (
            patch("yadgar.config.Settings", return_value=settings),
            patch("yadgar.storage.StorageEngine", return_value=storage),
            patch("yadgar.embeddings.EmbeddingEngine", return_value=embeddings),
            patch("yadgar.knowledge_graph.KnowledgeGraph", return_value=kg),
            patch("yadgar.cognitive_map.CognitiveMap", return_value=cmap),
            patch("yadgar.retrieval.Retriever", return_value=retriever),
            patch("yadgar.metacognition.MetaCognition", return_value=metacog),
            patch("yadgar.restoration.CheckpointRestore", return_value=replay),
        ):
            got_storage, _ = init_replay_lightweight()
        assert got_storage is storage

    def test_second_element_is_replay(self):
        mocks = _build_mocks()
        settings, storage, embeddings, kg, cmap, retriever, metacog, replay = mocks
        with (
            patch("yadgar.config.Settings", return_value=settings),
            patch("yadgar.storage.StorageEngine", return_value=storage),
            patch("yadgar.embeddings.EmbeddingEngine", return_value=embeddings),
            patch("yadgar.knowledge_graph.KnowledgeGraph", return_value=kg),
            patch("yadgar.cognitive_map.CognitiveMap", return_value=cmap),
            patch("yadgar.retrieval.Retriever", return_value=retriever),
            patch("yadgar.metacognition.MetaCognition", return_value=metacog),
            patch("yadgar.restoration.CheckpointRestore", return_value=replay),
        ):
            _, got_replay = init_replay_lightweight()
        assert got_replay is replay


# ---------------------------------------------------------------------------
# init_replay_lightweight — db_path handling
# ---------------------------------------------------------------------------


class TestInitReplayLightweightDbPath:
    def test_uses_settings_db_path_when_none(self):
        mocks = _build_mocks()
        settings, storage, embeddings, kg, cmap, retriever, metacog, replay = mocks
        settings.DB_PATH = "/default/path.db"

        with (
            patch("yadgar.config.Settings", return_value=settings),
            patch("yadgar.storage.StorageEngine", return_value=storage) as mock_storage_cls,
            patch("yadgar.embeddings.EmbeddingEngine", return_value=embeddings),
            patch("yadgar.knowledge_graph.KnowledgeGraph", return_value=kg),
            patch("yadgar.cognitive_map.CognitiveMap", return_value=cmap),
            patch("yadgar.retrieval.Retriever", return_value=retriever),
            patch("yadgar.metacognition.MetaCognition", return_value=metacog),
            patch("yadgar.restoration.CheckpointRestore", return_value=replay),
        ):
            init_replay_lightweight()
        mock_storage_cls.assert_called_once_with("/default/path.db")

    def test_uses_explicit_db_path_when_given(self):
        mocks = _build_mocks()
        settings, storage, embeddings, kg, cmap, retriever, metacog, replay = mocks

        with (
            patch("yadgar.config.Settings", return_value=settings),
            patch("yadgar.storage.StorageEngine", return_value=storage) as mock_storage_cls,
            patch("yadgar.embeddings.EmbeddingEngine", return_value=embeddings),
            patch("yadgar.knowledge_graph.KnowledgeGraph", return_value=kg),
            patch("yadgar.cognitive_map.CognitiveMap", return_value=cmap),
            patch("yadgar.retrieval.Retriever", return_value=retriever),
            patch("yadgar.metacognition.MetaCognition", return_value=metacog),
            patch("yadgar.restoration.CheckpointRestore", return_value=replay),
        ):
            init_replay_lightweight(db_path="/custom/path.db")
        mock_storage_cls.assert_called_once_with("/custom/path.db")


# ---------------------------------------------------------------------------
# init_replay_lightweight — construction chain
# ---------------------------------------------------------------------------


class TestInitReplayLightweightConstructionChain:
    def _run(self):
        mocks = _build_mocks()
        settings, storage, embeddings, kg, cmap, retriever, metacog, replay = mocks
        mock_settings_cls = MagicMock(return_value=settings)
        mock_storage_cls = MagicMock(return_value=storage)
        mock_embed_cls = MagicMock(return_value=embeddings)
        mock_kg_cls = MagicMock(return_value=kg)
        mock_cmap_cls = MagicMock(return_value=cmap)
        mock_retriever_cls = MagicMock(return_value=retriever)
        mock_metacog_cls = MagicMock(return_value=metacog)
        mock_replay_cls = MagicMock(return_value=replay)

        with (
            patch("yadgar.config.Settings", mock_settings_cls),
            patch("yadgar.storage.StorageEngine", mock_storage_cls),
            patch("yadgar.embeddings.EmbeddingEngine", mock_embed_cls),
            patch("yadgar.knowledge_graph.KnowledgeGraph", mock_kg_cls),
            patch("yadgar.cognitive_map.CognitiveMap", mock_cmap_cls),
            patch("yadgar.retrieval.Retriever", mock_retriever_cls),
            patch("yadgar.metacognition.MetaCognition", mock_metacog_cls),
            patch("yadgar.restoration.CheckpointRestore", mock_replay_cls),
        ):
            init_replay_lightweight()

        return (
            mock_settings_cls,
            mock_storage_cls,
            mock_embed_cls,
            mock_kg_cls,
            mock_cmap_cls,
            mock_retriever_cls,
            mock_metacog_cls,
            mock_replay_cls,
            settings,
            storage,
            embeddings,
            kg,
            cmap,
            retriever,
            metacog,
        )

    def test_settings_instantiated(self):
        clses = self._run()
        clses[0].assert_called_once()

    def test_embedding_engine_called_with_model(self):
        clses = self._run()
        mock_embed_cls = clses[2]
        settings = clses[8]
        mock_embed_cls.assert_called_once_with(settings.EMBEDDING_MODEL)

    def test_knowledge_graph_called_with_storage_settings(self):
        clses = self._run()
        mock_kg_cls = clses[3]
        storage = clses[9]
        settings = clses[8]
        mock_kg_cls.assert_called_once_with(storage, settings)

    def test_checkpoint_restore_receives_storage(self):
        clses = self._run()
        mock_replay_cls = clses[7]
        storage = clses[9]
        call_kwargs = mock_replay_cls.call_args.kwargs
        assert call_kwargs.get("storage") is storage

    def test_checkpoint_restore_receives_embeddings(self):
        clses = self._run()
        mock_replay_cls = clses[7]
        embeddings = clses[10]
        call_kwargs = mock_replay_cls.call_args.kwargs
        assert call_kwargs.get("embeddings") is embeddings

    def test_logging_disabled(self):
        """logging.disable(CRITICAL) must be called."""
        import logging

        mocks = _build_mocks()
        settings, storage, embeddings, kg, cmap, retriever, metacog, replay = mocks
        with (
            patch("yadgar.config.Settings", return_value=settings),
            patch("yadgar.storage.StorageEngine", return_value=storage),
            patch("yadgar.embeddings.EmbeddingEngine", return_value=embeddings),
            patch("yadgar.knowledge_graph.KnowledgeGraph", return_value=kg),
            patch("yadgar.cognitive_map.CognitiveMap", return_value=cmap),
            patch("yadgar.retrieval.Retriever", return_value=retriever),
            patch("yadgar.metacognition.MetaCognition", return_value=metacog),
            patch("yadgar.restoration.CheckpointRestore", return_value=replay),
            patch("logging.disable") as mock_disable,
        ):
            init_replay_lightweight()
        mock_disable.assert_called_once_with(logging.CRITICAL)
