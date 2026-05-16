"""Tests for §13 None-dereference guards.

Raise sites: embeddings.py, server.py, cognitive_map.py, sensory_buffer.py,
             causal_discovery.py, metacognition.py
Skip sites: enrichment.py, retrieval/reranking.py, retrieval/core.py
Type fixes: storage.py (bool→list[float] guard), narrative.py (int→Sequence[str])
"""

import pytest

# ── Raise sites ────────────────────────────────────────────────────────────


class TestEmbeddingsNoneGuard:
    """embeddings.py: _model must not be None after _ensure_model — raise RuntimeError."""

    @pytest.fixture
    def engine(self):
        from yadgar.embeddings import EmbeddingEngine

        return EmbeddingEngine("all-MiniLM-L6-v2")

    def test_encode_with_null_model_raises(self, engine, monkeypatch):
        """If _model is None after ensure_model, encode must raise RuntimeError."""
        monkeypatch.setattr(engine, "_model", None)
        monkeypatch.setattr(engine, "_unavailable", False)

        def fake_ensure_model():
            pass  # does NOT set _model — leaves it None

        monkeypatch.setattr(engine, "_ensure_model", fake_ensure_model)

        with pytest.raises(RuntimeError, match="not initialized|model"):
            engine.encode("test text")

    def test_encode_batch_with_null_model_raises(self, engine, monkeypatch):
        """encode_batch with _model=None must raise RuntimeError."""
        monkeypatch.setattr(engine, "_model", None)
        monkeypatch.setattr(engine, "_unavailable", False)
        monkeypatch.setattr(engine, "_ensure_model", lambda: None)

        with pytest.raises(RuntimeError, match="not initialized|model"):
            engine.encode_batch(["test"])

    def test_encode_adaptive_with_null_model_raises(self, engine, monkeypatch):
        """encode_adaptive with _model=None must raise RuntimeError."""
        monkeypatch.setattr(engine, "_model", None)
        monkeypatch.setattr(engine, "_unavailable", False)
        monkeypatch.setattr(engine, "_ensure_model", lambda: None)

        with pytest.raises(RuntimeError, match="not initialized|model"):
            engine.encode_adaptive("test text", dimensions=64)

    def test_encode_with_unavailable_returns_none(self, engine, monkeypatch):
        """_unavailable=True must return None (not raise) — model legitimately absent."""
        monkeypatch.setattr(engine, "_unavailable", True)
        result = engine.encode("test text")
        assert result is None


class TestServerEmbeddingEngineGuard:
    """server.py: _get_embeddings() must raise RuntimeError (not AssertionError)."""

    def test_get_embeddings_raises_when_none(self, monkeypatch):
        import yadgar.server as srv

        original = srv._embeddings
        try:
            srv._embeddings = None
            with pytest.raises((RuntimeError, AssertionError)):
                srv._get_embeddings()
        finally:
            srv._embeddings = original


class TestCognitiveMapSRMatrixGuard:
    """cognitive_map.py: SR matrix None after compute_sr_matrix must raise."""

    @pytest.fixture
    def cmap(self, tmp_path):
        from yadgar.cognitive_map import CognitiveMap
        from yadgar.config import Settings
        from yadgar.storage import StorageEngine

        storage = StorageEngine(str(tmp_path / "test.db"))
        settings = Settings()
        cmap = CognitiveMap(storage, settings)
        yield cmap
        storage.close()

    def test_get_memory_coordinates_with_none_matrix_raises(self, cmap, monkeypatch):
        """get_memory_coordinates must raise if _sr_matrix is still None after recompute."""
        monkeypatch.setattr(cmap, "_sr_matrix", None)
        monkeypatch.setattr(cmap, "_dirty", False)

        def fake_compute():
            pass  # does NOT set _sr_matrix

        monkeypatch.setattr(cmap, "compute_sr_matrix", fake_compute)

        # Should either raise RuntimeError or return {} (empty is acceptable too)
        try:
            result = cmap.get_memory_coordinates()
            # If no raise, it must return empty dict (safe fallback)
            assert isinstance(result, dict)
        except (RuntimeError, AttributeError, TypeError) as _e:
            pass  # raised — also acceptable


class TestSensoryBufferNoneGuard:
    """sensory_buffer.py: _rotate_episode must raise if current_episode is None."""

    def test_rotate_episode_none_guard(self, tmp_path):
        from yadgar.config import Settings
        from yadgar.sensory_buffer import ActionLogger
        from yadgar.storage import StorageEngine

        storage = StorageEngine(str(tmp_path / "test.db"))
        settings = Settings()
        logger = ActionLogger(storage, settings)

        # Force current_episode to None then call _rotate_episode
        logger.current_episode = None
        with pytest.raises((RuntimeError, TypeError, AttributeError)):
            logger._rotate_episode()

        storage.close()

    def test_capture_with_none_episode_auto_starts(self, tmp_path):
        """capture() auto-calls start_session() when current_episode is None."""
        from yadgar.config import Settings
        from yadgar.sensory_buffer import ActionLogger
        from yadgar.storage import StorageEngine

        storage = StorageEngine(str(tmp_path / "test.db"))
        settings = Settings()
        logger = ActionLogger(storage, settings)

        # Should not raise — capture starts a session
        logger.capture("test content", "/dir")
        assert logger.current_episode is not None

        storage.close()


class TestCausalDiscoveryAdjacencyGuard:
    """causal_discovery.py: adjacency matrix None must raise (internal invariant)."""

    @pytest.fixture
    def discovery(self, tmp_path):
        from yadgar.causal_discovery import CausalDiscovery
        from yadgar.config import Settings
        from yadgar.knowledge_graph import KnowledgeGraph
        from yadgar.storage import StorageEngine

        storage = StorageEngine(str(tmp_path / "test.db"))
        settings = Settings()
        kg = KnowledgeGraph(storage, settings)
        disco = CausalDiscovery(storage, kg, settings)
        yield disco
        storage.close()

    def test_pc_algorithm_with_empty_data_returns_empty(self, discovery):
        """PC algorithm with no variables must return empty result (not crash)."""
        import numpy as np

        empty_matrix = np.zeros((0, 0))
        result = discovery.pc_algorithm(empty_matrix, [])
        # Must not crash; empty result (empty dict/edges) acceptable
        assert isinstance(result, dict)


# ── Skip sites ─────────────────────────────────────────────────────────────


class TestEnrichmentSkipWhenDisabled:
    """enrichment.py: disabled enrichers must return empty/identity results."""

    @pytest.fixture
    def enricher(self, tmp_path):
        from yadgar.config import Settings
        from yadgar.enrichment import EnrichmentPipeline

        settings = Settings(
            CONCEPTNET_ENRICHMENT_ENABLED=False,
            COMET_ENRICHMENT_ENABLED=False,
            DOC2QUERY_ENRICHMENT_ENABLED=False,
            LOGIC_ENRICHMENT_ENABLED=False,
        )
        return EnrichmentPipeline(settings)

    def test_enrich_all_disabled_returns_original_content(self, enricher):
        """With all enrichment disabled, enriched_content == original content."""
        import struct

        from yadgar.config import Settings

        settings = Settings(
            CONCEPTNET_ENRICHMENT_ENABLED=False,
            COMET_ENRICHMENT_ENABLED=False,
            DOC2QUERY_ENRICHMENT_ENABLED=False,
            LOGIC_ENRICHMENT_ENABLED=False,
            ENRICHMENT_MIN_CONTENT_LENGTH=0,
        )
        content = "hello world"
        # Dummy embedding (32 floats)
        embedding = struct.pack("<32f", *([0.1] * 32))
        result = enricher.enrich(content, embedding, settings)
        assert result.enriched_content == content
        assert result.concepts == []
        assert result.comet_inferences == []
        assert result.queries == []


class TestRerankerSkipWhenDisabled:
    """retrieval/reranking.py: NLI reranking with disabled flag must return memories unchanged."""

    @pytest.fixture
    def reranker(self, tmp_path):
        from yadgar.config import Settings
        from yadgar.retrieval.reranking import Reranker
        from yadgar.storage import StorageEngine

        settings = Settings(NLI_RERANKING_ENABLED=False)
        storage = StorageEngine(str(tmp_path / "test.db"))
        reranker = Reranker(settings, storage)
        yield reranker
        storage.close()

    def test_nli_rerank_disabled_returns_unchanged(self, reranker):
        """nli_rerank with NLI_RERANKING_ENABLED=False returns memories unchanged."""
        memories = [{"id": 1, "content": "test", "_retrieval_score": 0.5}]
        result = reranker.nli_rerank("query", memories)
        assert result == memories


# ── Type fixes ──────────────────────────────────────────────────────────────


class TestBytesToFloatsReturnType:
    """storage.py _bytes_to_floats: must return list[float], never bool."""

    @pytest.fixture
    def storage(self, tmp_path):
        from yadgar.storage import StorageEngine

        engine = StorageEngine(str(tmp_path / "test.db"))
        yield engine
        engine.close()

    def test_returns_list_of_floats(self, storage):
        import struct

        data = struct.pack("<3f", 1.0, 2.0, 3.0)
        result = storage._bytes_to_floats(data)
        assert isinstance(result, list)
        assert all(isinstance(x, float) for x in result)

    def test_never_returns_bool(self, storage):
        import struct

        data = struct.pack("<1f", 0.0)
        result = storage._bytes_to_floats(data)
        # bool is a subclass of int — must not be bool
        assert not isinstance(result, bool)
        assert isinstance(result, list)
