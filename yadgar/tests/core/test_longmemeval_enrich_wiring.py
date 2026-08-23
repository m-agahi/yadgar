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


# ---------------------------------------------------------------------------
# Car 8 task 293: identity-train regression — ingest_question_haystack builds
# the memory payload WITHOUT a project_id field, and ``insert_memory``
# (storage/memory.py:_resolve_project_id_for_write) raises
# ``UnresolvedProjectError`` on a missing project_id (C5 deleted the
# directory-context fallback; ADR-0227). Every haystack session ingest
# silently failed, the benchmark reported 0.000 on every metric, and the
# "0.0" was read as a real score. Two pins below: (a) every payload the
# benchmark hands to ``insert_memory`` carries a stable project_id, and
# (b) that identity is the BENCHMARK_PROJECT_ID constant so cross-runs
# of the benchmark are comparable (the haystack corpus is itself
# benchmark-shaped, not project-scoped — a fixed identity is the right
# answer; per-question identities would scatter the corpus).
# ---------------------------------------------------------------------------


def test_ingest_payload_carries_project_id():
    """Car 8 task 293: insert_memory now requires project_id (C5/ADR-0227).
    Pre-fix, the payload dict omitted project_id, _resolve_project_id_for_write
    raised UnresolvedProjectError, and ``make longmemeval`` reported 0.000
    on every metric — absence of data read as a score."""
    storage, embeddings, curator, thermo, settings = _mocks()
    ingest_question_haystack(_question(), storage, embeddings, curator, thermo, settings)
    assert storage.insert_memory.called
    payload = storage.insert_memory.call_args.args[0]
    assert payload["project_id"], (
        "ingest payload must carry project_id — C5 made it required and "
        "ADR-0227 deleted the directory-context fallback"
    )


def test_ingest_payload_uses_benchmark_identity():
    """The benchmark identity is a stable, known constant — the corpus
    is global (not project-scoped), so a fixed string is the right
    answer. Cross-run comparability depends on it being identical
    every invocation."""
    storage, embeddings, curator, thermo, settings = _mocks()
    ingest_question_haystack(_question(), storage, embeddings, curator, thermo, settings)
    payload = storage.insert_memory.call_args.args[0]
    # BENCHMARK_PROJECT_ID lives next to BENCHMARK_DIRECTORY at the top
    # of run_longmemeval.py — if this import fails the car has not
    # shipped the constant, which is itself a regression.
    from benchmarks.run_longmemeval import BENCHMARK_PROJECT_ID

    assert payload["project_id"] == BENCHMARK_PROJECT_ID, (
        f"ingest must stamp BENCHMARK_PROJECT_ID, got {payload['project_id']!r}"
    )


def test_ingest_payload_directory_context_unchanged():
    """The directory_context column stays — ADR-0233 keeps it so
    project_backfill can derive project_id FROM it on the restamp
    half of task 310. The fix adds project_id without dropping
    directory_context."""
    storage, embeddings, curator, thermo, settings = _mocks()
    ingest_question_haystack(_question(), storage, embeddings, curator, thermo, settings)
    payload = storage.insert_memory.call_args.args[0]
    assert payload["directory_context"] == "/benchmark/longmemeval"
