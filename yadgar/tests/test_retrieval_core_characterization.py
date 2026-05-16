"""Characterization tests for retrieval/core.py::Retriever.recall.

Target: recall() at line 306 (cognitive complexity ~330 — the mega-function
flagged for Stage 11 decomposition into pipeline stages:
candidate_fetch -> score_fusion -> rerank -> filter -> trim).

These tests pin the ranked-content lists and per-result retrieval scores
produced by recall() against a fixed 20-memory corpus and 10 representative
queries. Stage 11 decomposition must produce identical outputs.

Design decisions:
- Storage: real SQLite StorageEngine (embedded mode, no SurrealDB needed)
- Embeddings: stub DeterministicEmbeddings — content-hash-based vectors,
  no model download, fully deterministic
- All ML-based re-rankers disabled (CROSS_ENCODER_ENABLED=False,
  NLI_RERANKING_ENABLED=False, ADVERSARIAL_*, MULTI_PASSAGE_*, CONFIDENCE_GATING_ENABLED=False)
- rules_engine=None, engram=None, metacognition=None passed to Retriever
- Fixture keyed by memory content strings (NOT raw record IDs — IDs are
  not stable across fresh-DB runs)
- Scores compared with math.isclose(rel_tol=1e-9)

Fixture generation: set YADGAR_REGEN_FIXTURES=1 to regenerate
yadgar/tests/fixtures/retrieval_core_expected.json.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from yadgar.config import Settings
from yadgar.embeddings import EmbeddingEngine
from yadgar.knowledge_graph import KnowledgeGraph
from yadgar.retrieval.core import Retriever
from yadgar.storage import StorageEngine

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "retrieval_core_expected.json"
REGEN = os.environ.get("YADGAR_REGEN_FIXTURES", "").lower() in {"1", "true", "yes"}

# ---------------------------------------------------------------------------
# Deterministic stub embeddings
# ---------------------------------------------------------------------------

DIM = 384  # match all-MiniLM-L6-v2 expected dimension


class DeterministicEmbeddings(EmbeddingEngine):
    """Stub EmbeddingEngine that hashes text to a deterministic unit vector.

    Each unique text produces a distinct vector; similar content (sharing
    tokens) will have higher dot product, preserving relevance ordering.
    The approach: map each text to a seed, use numpy RNG to produce a
    stable normal-distributed vector, then L2-normalize. This avoids
    float32 overflow that arises from raw sha256 bit patterns.
    """

    def __init__(self):
        super().__init__(model_name="all-MiniLM-L6-v2")
        self._unavailable = False  # prevent any real model load

    def _text_to_vector(self, text: str) -> np.ndarray:
        # Derive a 32-bit seed from the text via sha256, then use numpy RNG
        seed_int = int.from_bytes(hashlib.sha256(text.encode()).digest()[:4], "big")
        rng = np.random.default_rng(seed_int)
        vec = rng.standard_normal(DIM).astype(np.float64)
        # Add per-word components so semantically similar texts share direction
        for word in text.lower().split():
            w_seed = int.from_bytes(hashlib.sha256(word.encode()).digest()[:4], "big")
            w_rng = np.random.default_rng(w_seed)
            vec += 0.3 * w_rng.standard_normal(DIM)
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm
        return vec.astype(np.float32)

    def encode(self, text: str) -> bytes:
        return self._text_to_vector(text).tobytes()

    def encode_query(self, text: str) -> bytes:
        return self.encode(text)

    def encode_document(self, text: str) -> bytes:
        return self.encode(text)

    def encode_document_enriched(self, content: str, enriched_content=None) -> bytes:
        return self.encode(content)

    def encode_batch(self, texts: list[str]) -> list[bytes]:
        return [self.encode(t) for t in texts]

    def get_model_name(self) -> str:
        return "all-MiniLM-L6-v2"

    def get_dimensions(self) -> int:
        return DIM

    def _ensure_model(self) -> None:
        pass  # Never load a real model


# ---------------------------------------------------------------------------
# Corpus
# ---------------------------------------------------------------------------

CORPUS = [
    # Python / backend
    "FastAPI REST server with uvicorn and async handlers",
    "Python asyncio event loop for concurrent I/O operations",
    "Pydantic data validation for request and response schemas",
    "SQLite WAL mode improves concurrent read throughput",
    "pytest fixtures and parametrize for unit test suites",
    # Machine learning
    "Sentence transformers encode text to dense embeddings",
    "Cosine similarity measures angle between embedding vectors",
    "ONNX runtime runs cross-encoder models on CPU efficiently",
    "BM25 keyword search ranks documents by term frequency",
    "Vector similarity search with HNSW index for fast ANN",
    # Infrastructure
    "Docker container with read-only filesystem and no-new-privileges",
    "SurrealDB surrealkv storage engine with WAL journaling",
    "Prometheus metrics endpoint for latency and error rate",
    "Forgejo CI workflow with matrix strategy for Python versions",
    "systemd service unit with EnvironmentFile for secrets",
    # Causal / memory
    "PC algorithm discovers causal DAG from observational data",
    "Meek orientation rules propagate edge directions in CPDAG",
    "Hippocampal replay consolidates short-term to long-term memory",
    "Heat decay function reduces memory salience over time",
    "Engram slot allocator assigns memories to fixed capacity slots",
]

QUERIES = [
    "FastAPI async server",
    "embedding similarity search",
    "SurrealDB storage",
    "causal discovery algorithm",
    "memory consolidation heat",
    "Python test framework",
    "Docker container security",
    "BM25 keyword retrieval",
    "Prometheus monitoring metrics",
    "Meek rules orientation",
]


# ---------------------------------------------------------------------------
# Settings: all ML re-rankers off
# ---------------------------------------------------------------------------


def _make_settings(db_path: str) -> Settings:
    return Settings(
        DB_PATH=db_path,
        RERANKER_ENABLED=True,  # heuristic reranker only (no ML)
        CROSS_ENCODER_ENABLED=False,
        NLI_RERANKING_ENABLED=False,
        ADVERSARIAL_DETECTION_ENABLED=False,
        ADVERSARIAL_DIVERSITY_ENFORCEMENT=False,
        MULTI_PASSAGE_RERANKING_ENABLED=False,
        CONFIDENCE_GATING_ENABLED=False,
        COMPARISON_DUAL_SEARCH_ENABLED=False,
        TEMPORAL_RETRIEVAL_ENABLED=False,
        QUERY_EXPANSION_ENABLED=False,
        COMET_QUERY_EXPANSION_ENABLED=False,
        RETRIEVAL_PROFILE="balanced",
        FUSION_METHOD="wrrf",
        COMBMNZ_ENABLED=False,
    )


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _build_corpus(storage: StorageEngine, embeddings: DeterministicEmbeddings) -> dict[int, str]:
    """Insert CORPUS into storage, return {id: content} map."""
    id_to_content = {}
    for content in CORPUS:
        emb = embeddings.encode(content)
        mid = storage.insert_memory(
            {
                "content": content,
                "embedding": emb,
                "tags": [],
                "directory_context": "/characterization",
                "heat": 1.0,
                "is_stale": False,
                "file_hash": None,
                "embedding_model": embeddings.get_model_name(),
            }
        )
        id_to_content[mid] = content
    return id_to_content


def _make_retriever(
    storage: StorageEngine, embeddings: DeterministicEmbeddings, settings: Settings
) -> Retriever:
    """Build a Retriever with a stub MLClient (no real model loads)."""
    kg = KnowledgeGraph(storage, settings)

    # Stub out the MLClient so cross-encoder / NLI never attempt model loads
    stub_ml = MagicMock()
    stub_ml.cross_encode.return_value = []
    stub_ml.nli_score.return_value = []
    stub_ml.is_idle.return_value = True

    retriever = Retriever(storage, embeddings, kg, settings, ml_client=stub_ml)
    # Ensure no external engines attached
    retriever._rules_engine = None
    retriever._engram = None
    retriever._metacognition = None
    return retriever


def _recall_results(
    retriever: Retriever, id_to_content: dict[int, str], query: str, max_results: int = 5
) -> dict:
    """Run recall and return {ordered_contents: [...], scores: [...]} for fixture."""
    results = retriever.recall(query, max_results=max_results, min_heat=0.01)
    ordered_contents = []
    scores = []
    for mem in results:
        mid = mem.get("id")
        content = id_to_content.get(mid, mem.get("content", f"unknown_id_{mid}"))
        score = mem.get("_retrieval_score", 0.0)
        ordered_contents.append(content)
        scores.append(round(float(score), 6))  # store rounded for readability
    return {"ordered_contents": ordered_contents, "scores": scores}


def _generate_fixture_data(tmp_path_base: str) -> list[dict]:
    """Run all queries against the corpus and return serializable fixture."""
    import tempfile
    from pathlib import Path as _Path

    with tempfile.TemporaryDirectory() as tmp:
        storage = StorageEngine(str(_Path(tmp) / "retrieval_char.db"))
        embeddings = DeterministicEmbeddings()
        settings = _make_settings(str(_Path(tmp) / "settings.db"))
        id_to_content = _build_corpus(storage, embeddings)
        retriever = _make_retriever(storage, embeddings, settings)

        query_results = []
        for query in QUERIES:
            result = _recall_results(retriever, id_to_content, query, max_results=5)
            query_results.append({"query": query, **result})

        storage.close()

    return query_results


# ---------------------------------------------------------------------------
# Fixture regen path
# ---------------------------------------------------------------------------


def test_regen_fixture_if_requested():
    """Only runs when YADGAR_REGEN_FIXTURES=1. Writes fixture to disk."""
    if not REGEN:
        pytest.skip("Set YADGAR_REGEN_FIXTURES=1 to regenerate")

    data = _generate_fixture_data("/tmp")
    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE_PATH.write_text(json.dumps(data, indent=2))
    print(f"\nWrote {FIXTURE_PATH} ({FIXTURE_PATH.stat().st_size} bytes)")
    assert FIXTURE_PATH.exists()
    # Sanity: all queries should have at least 1 result
    for item in data:
        assert len(item["ordered_contents"]) >= 1, (
            f"Query {item['query']!r} returned no results — corpus may be empty"
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def expected_fixture():
    if not FIXTURE_PATH.exists():
        pytest.skip(
            f"Fixture missing: {FIXTURE_PATH}. Run with YADGAR_REGEN_FIXTURES=1 to generate."
        )
    return json.loads(FIXTURE_PATH.read_text())


@pytest.fixture(scope="module")
def retrieval_env(tmp_path_factory):
    """Build corpus + retriever for characterization tests (module-scoped)."""
    tmp = tmp_path_factory.mktemp("retrieval_char")
    storage = StorageEngine(str(tmp / "retrieval_char.db"))
    embeddings = DeterministicEmbeddings()
    settings = _make_settings(str(tmp / "settings.db"))
    id_to_content = _build_corpus(storage, embeddings)
    retriever = _make_retriever(storage, embeddings, settings)
    yield retriever, id_to_content
    storage.close()


# ---------------------------------------------------------------------------
# Characterization assertions
# ---------------------------------------------------------------------------


class TestRetrievalCoreCharacterization:
    """Pin recall() ranked-content lists and scores for 10 representative queries.

    Assertions:
    - ordered_contents list: exact equality (order matters)
    - scores: math.isclose(rel_tol=1e-9) element-wise
    """

    def _assert_query_result(self, retrieval_env, expected_fixture, query_idx: int):
        retriever, id_to_content = retrieval_env
        exp = expected_fixture[query_idx]
        query = QUERIES[query_idx]
        assert exp["query"] == query, f"Fixture query mismatch at index {query_idx}"

        actual = _recall_results(retriever, id_to_content, query, max_results=5)

        assert actual["ordered_contents"] == exp["ordered_contents"], (
            f"Query {query!r}: content order mismatch.\n"
            f"actual:   {actual['ordered_contents']}\n"
            f"expected: {exp['ordered_contents']}"
        )
        assert len(actual["scores"]) == len(exp["scores"]), f"Query {query!r}: score count mismatch"
        for i, (a_score, e_score) in enumerate(zip(actual["scores"], exp["scores"], strict=False)):
            assert math.isclose(a_score, e_score, rel_tol=1e-9, abs_tol=1e-12), (
                f"Query {query!r} result[{i}] score mismatch: {a_score} != {e_score}"
            )

    def test_query_0_fastapi_async(self, retrieval_env, expected_fixture):
        self._assert_query_result(retrieval_env, expected_fixture, 0)

    def test_query_1_embedding_similarity(self, retrieval_env, expected_fixture):
        self._assert_query_result(retrieval_env, expected_fixture, 1)

    def test_query_2_surrealdb_storage(self, retrieval_env, expected_fixture):
        self._assert_query_result(retrieval_env, expected_fixture, 2)

    def test_query_3_causal_discovery(self, retrieval_env, expected_fixture):
        self._assert_query_result(retrieval_env, expected_fixture, 3)

    def test_query_4_memory_consolidation(self, retrieval_env, expected_fixture):
        self._assert_query_result(retrieval_env, expected_fixture, 4)

    def test_query_5_python_test_framework(self, retrieval_env, expected_fixture):
        self._assert_query_result(retrieval_env, expected_fixture, 5)

    def test_query_6_docker_security(self, retrieval_env, expected_fixture):
        self._assert_query_result(retrieval_env, expected_fixture, 6)

    def test_query_7_bm25_retrieval(self, retrieval_env, expected_fixture):
        self._assert_query_result(retrieval_env, expected_fixture, 7)

    def test_query_8_prometheus_metrics(self, retrieval_env, expected_fixture):
        self._assert_query_result(retrieval_env, expected_fixture, 8)

    def test_query_9_meek_orientation(self, retrieval_env, expected_fixture):
        self._assert_query_result(retrieval_env, expected_fixture, 9)

    def test_fixture_covers_ten_queries(self, expected_fixture):
        """Fixture has exactly 10 queries."""
        assert len(expected_fixture) == 10

    def test_all_queries_return_results(self, retrieval_env, expected_fixture):
        """Every query must return at least 1 result from the 20-memory corpus."""
        retriever, id_to_content = retrieval_env
        for query in QUERIES:
            results = retriever.recall(query, max_results=5, min_heat=0.01)
            assert len(results) >= 1, f"Query {query!r} returned 0 results"

    def test_scores_are_non_negative(self, retrieval_env, expected_fixture):
        """All retrieval scores must be non-negative."""
        for item in expected_fixture:
            for score in item["scores"]:
                assert score >= 0.0, f"Negative score {score} for query {item['query']!r}"

    def test_no_score_ties_in_top5(self, expected_fixture):
        """No two adjacent results should have identical scores (tie-break instability).

        If ties exist, the fixture may fail after Stage 11 refactor due to
        legitimate tie-reordering. This test guards against that.
        """
        for item in expected_fixture:
            scores = item["scores"]
            for i in range(len(scores) - 1):
                assert not math.isclose(scores[i], scores[i + 1], rel_tol=1e-6), (
                    f"Query {item['query']!r}: tied scores at positions {i}/{i + 1}: "
                    f"{scores[i]} == {scores[i + 1]}. Perturb corpus to resolve."
                )
