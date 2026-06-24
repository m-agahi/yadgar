from unittest.mock import MagicMock, patch

import numpy as np

from yadgar.config import Settings
from yadgar.enrichment import (
    HARDCODED_EXPANSIONS,
    ConceptNetExpander,
    EnrichmentPipeline,
    EnrichmentResult,
    FPAFilter,
    LogicExpander,
)


class MockEmbeddingEngine:
    def encode_query(self, text):
        np.random.seed(abs(hash(text)) % (2**31))
        emb = np.random.randn(768).astype(np.float32)
        return emb / np.linalg.norm(emb)


def _settings(**overrides) -> Settings:
    defaults = {
        "INDEX_ENRICHMENT_ENABLED": True,
        "CONCEPTNET_ENRICHMENT_ENABLED": False,
        "COMET_ENRICHMENT_ENABLED": False,
        "DOC2QUERY_ENRICHMENT_ENABLED": False,
        "LOGIC_ENRICHMENT_ENABLED": False,
        "ENRICHMENT_MIN_CONTENT_LENGTH": 20,
    }
    defaults.update(overrides)
    return Settings(**defaults)


class TestLogicExpander:
    def test_logic_expander_hypernym(self):
        expander = LogicExpander()
        result = expander.expand("went camping at Yellowstone")
        assert any(term in result for term in ("national_park", "outdoor")), (
            f"Expected hypernym in {result}"
        )

    def test_logic_expander_verb_nominalization(self):
        expander = LogicExpander()
        result = expander.expand("enjoys reading")
        assert any("reading" in term for term in result), (
            f"Expected reading nominalization in {result}"
        )


class TestConceptNetExpander:
    def test_hardcoded_fallback(self):
        expander = ConceptNetExpander()
        # Force lite and http unavailable so it falls back to hardcoded
        expander._lite_available = False
        expander._http_available = False
        settings = _settings()
        result = expander.expand("camping", settings)
        expected = HARDCODED_EXPANSIONS["camping"]
        assert len(result) > 0, "Expected hardcoded expansions"
        for term in result:
            assert term in expected, f"{term} not in hardcoded camping expansions"

    def test_try_http_parses_edges(self):
        """Characterization: _try_http filters edges by weight and builds labels correctly."""
        expander = ConceptNetExpander()
        expander._http_available = True  # enable HTTP path
        expander._lite_available = False

        fake_response = MagicMock()
        fake_response.raise_for_status.return_value = None
        fake_response.json.return_value = {
            "edges": [
                {"weight": 2.0, "end": {"label": "outdoor activity"}},
                {"weight": 0.1, "end": {"label": "low weight term"}},  # below min_weight
                {"weight": 3.0, "end": {"label": "nature"}},
                {"weight": 1.5, "end": {"label": ""}},  # empty label — skip
            ]
        }

        with patch("httpx.get", return_value=fake_response) as mock_get:
            results = expander._try_http("camping", ["RelatedTo"], min_weight=1.0)

        assert "outdoor_activity" in results
        assert "nature" in results
        assert "low_weight_term" not in results
        assert "" not in results
        assert mock_get.call_count == 1

    def test_try_http_request_error_continues(self):
        """Characterization: per-relation request errors are swallowed; other relations proceed."""
        import httpx

        expander = ConceptNetExpander()
        expander._http_available = True
        expander._lite_available = False

        good_response = MagicMock()
        good_response.raise_for_status.return_value = None
        good_response.json.return_value = {"edges": [{"weight": 2.0, "end": {"label": "trail"}}]}

        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise httpx.RequestError("timeout", request=MagicMock())
            return good_response

        with patch("httpx.get", side_effect=side_effect):
            results = expander._try_http("camping", ["RelatedTo", "IsA"], min_weight=1.0)

        assert "trail" in results

    def test_try_http_import_error_disables(self):
        """Characterization: ImportError (httpx missing) sets _http_available=False."""
        expander = ConceptNetExpander()
        expander._http_available = True
        expander._lite_available = False

        with patch("builtins.__import__", side_effect=ImportError("no httpx")):
            results = expander._try_http("camping", ["RelatedTo"], min_weight=1.0)

        assert results == []
        assert expander._http_available is False


class TestFPAFilter:
    def _make_embedding(self, text: str) -> bytes:
        engine = MockEmbeddingEngine()
        vec = engine.encode_query(text)
        return vec.tobytes()

    def test_fpa_filter_accepts_related(self):
        """Similar embeddings (same text) should pass the filter."""
        engine = MockEmbeddingEngine()
        original = engine.encode_query("camping outdoors nature")
        fpa = FPAFilter(engine)
        # Use the same text so cosine similarity is 1.0
        kept = fpa.filter(
            original.tobytes(),
            ["camping outdoors nature"],
            threshold=0.25,
        )
        assert len(kept) == 1

    def test_fpa_filter_rejects_unrelated(self):
        """Distant embeddings should be rejected by the filter."""

        class DistantEmbeddingEngine:
            """Returns orthogonal embeddings for different texts."""

            def __init__(self):
                self._call_count = 0

            def encode_query(self, text):
                # Return a vector with a single 1.0 at a unique position
                emb = np.zeros(768, dtype=np.float32)
                emb[self._call_count % 768] = 1.0
                self._call_count += 1
                return emb

        engine = DistantEmbeddingEngine()
        # Original embedding: [1, 0, 0, ...]
        original = engine.encode_query("original concept")
        fpa = FPAFilter(engine)
        # Next calls produce orthogonal vectors → cosine = 0
        kept = fpa.filter(
            original.tobytes(),
            ["unrelated1", "unrelated2", "unrelated3"],
            threshold=0.25,
        )
        assert len(kept) == 0, f"Expected all rejected, got {kept}"


class TestEnrichmentResult:
    def test_dataclass_defaults(self):
        result = EnrichmentResult()
        assert result.concepts == []
        assert result.comet_inferences == []
        assert result.queries == []
        assert result.logic_expansions == []
        assert result.enriched_content == ""
        assert result.model_versions == {}

    def test_dataclass_fields(self):
        result = EnrichmentResult(
            concepts=["a"],
            logic_expansions=["b"],
            enriched_content="test",
        )
        assert result.concepts == ["a"]
        assert result.logic_expansions == ["b"]
        assert result.enriched_content == "test"


class TestEnrichmentPipeline:
    def _dummy_embedding(self) -> bytes:
        vec = np.ones(768, dtype=np.float32)
        vec = vec / np.linalg.norm(vec)
        return vec.tobytes()

    def test_short_content_skips(self):
        settings = _settings(ENRICHMENT_MIN_CONTENT_LENGTH=20)
        pipeline = EnrichmentPipeline(settings)
        result = pipeline.enrich("short", self._dummy_embedding(), settings)
        assert result.concepts == []
        assert result.comet_inferences == []
        assert result.queries == []
        assert result.logic_expansions == []
        assert result.enriched_content == "short"

    def test_logic_only(self):
        settings = _settings(
            LOGIC_ENRICHMENT_ENABLED=True,
            ENRICHMENT_MIN_CONTENT_LENGTH=5,
        )
        pipeline = EnrichmentPipeline(settings)
        content = "went camping at Yellowstone last summer"
        result = pipeline.enrich(content, self._dummy_embedding(), settings)
        assert result.concepts == []
        assert result.comet_inferences == []
        assert result.queries == []
        assert len(result.logic_expansions) > 0, "Expected logic expansions"
        assert "[enrichment]" in result.enriched_content

    def test_comet_disabled_skips_comet_branch(self):
        """COMET retired/dormant (ADR-0004): disabled → pipeline never touches COMET.

        With COMET_ENRICHMENT_ENABLED=False the enrich() COMET branch must be
        skipped — _get_comet() is never called and comet_inferences stays empty.
        """
        settings = _settings(COMET_ENRICHMENT_ENABLED=False, ENRICHMENT_MIN_CONTENT_LENGTH=5)
        pipeline = EnrichmentPipeline(settings)
        content = "went camping at Yellowstone last summer"
        with patch.object(pipeline, "_get_comet") as mock_get_comet:
            result = pipeline.enrich(content, self._dummy_embedding(), settings)
            mock_get_comet.assert_not_called()
        assert result.comet_inferences == []


class TestCometRetiredDefault:
    """ADR-0004: COMET retired to dormant — flag default must be False."""

    def test_comet_enrichment_default_is_false(self):
        from yadgar.config import Settings as _Settings

        assert _Settings().COMET_ENRICHMENT_ENABLED is False
