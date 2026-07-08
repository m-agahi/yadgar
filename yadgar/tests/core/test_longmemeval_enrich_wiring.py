"""Fidelity (v5.82): LongMemEval haystack ingest must thread settings+embeddings
into storage.insert_memory so index-time enrichment (COMET/ConceptNet/Logic/
Doc2Query/FPA) runs during eval — matching production memorize().

Without these kwargs the enrichment guard in storage/memory.py silently no-ops,
so the eval measures retrieval over a RAW (un-enriched) corpus, unfaithful to
production. A COMET-FPA ablation via `make longmemeval` is meaningless until
this wiring exists.

TDD: red on the un-wired ingest (insert_memory called with only the memory
dict), green after the enrich= wiring. Hermetic — mocks storage/embeddings/
thermo; no SurrealDB, no model downloads.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from benchmarks.run_longmemeval import ingest_question_haystack


def _question() -> dict:
    return {
        "haystack_sessions": [
            [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi there"},
            ]
        ],
        "haystack_session_ids": ["s1"],
        "haystack_dates": ["2026-01-01"],
    }


def _mocks():
    storage = MagicMock()
    storage.insert_memory.return_value = 1
    embeddings = MagicMock()
    embeddings.encode.return_value = [0.1, 0.2, 0.3]
    embeddings.get_model_name.return_value = "all-MiniLM-L6-v2"
    thermo = MagicMock()
    thermo.compute_importance.return_value = 0.5
    curator = MagicMock()
    settings = MagicMock()
    return storage, embeddings, curator, thermo, settings


def test_ingest_enrich_on_threads_settings_and_embeddings():
    """enrich=True forwards the real embeddings + settings so enrichment runs."""
    storage, embeddings, curator, thermo, settings = _mocks()
    ingest_question_haystack(
        _question(), storage, embeddings, curator, thermo, settings, enrich=True
    )
    assert storage.insert_memory.called
    _, kwargs = storage.insert_memory.call_args
    assert kwargs.get("embeddings_engine") is embeddings
    assert kwargs.get("settings") is settings


def test_ingest_enrich_off_passes_none():
    """enrich=False preserves the legacy raw-ingest behavior (guard no-ops)."""
    storage, embeddings, curator, thermo, settings = _mocks()
    ingest_question_haystack(
        _question(), storage, embeddings, curator, thermo, settings, enrich=False
    )
    assert storage.insert_memory.called
    _, kwargs = storage.insert_memory.call_args
    assert kwargs.get("embeddings_engine") is None
    assert kwargs.get("settings") is None


def test_ingest_default_is_enriched():
    """Default (no enrich arg) must be prod-faithful = enriched."""
    storage, embeddings, curator, thermo, settings = _mocks()
    ingest_question_haystack(_question(), storage, embeddings, curator, thermo, settings)
    _, kwargs = storage.insert_memory.call_args
    assert kwargs.get("embeddings_engine") is embeddings
    assert kwargs.get("settings") is settings
