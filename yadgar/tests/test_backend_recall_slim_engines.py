"""Car 3 (folder-split #17) GATE: backend slim-engine set parity + presence.

The backend ``/recall`` bootstrap builds a SLIM engine set — only the 13 engines
the recall path needs — and SKIPS the 11 CORE-ONLY engines. This is the gate that
proves the split is behavior-neutral for recall:

  * ``test_slim_builds_13_skips_11`` — after ``init_engines(engine_set="slim")``
    the 13 slim engines are non-None and the 11 CORE-ONLY engines are None. A
    slim-set engine that turns out to be needed surfaces as a None-crash on the
    first ``/recall`` (caught by the parity test below).
  * ``test_slim_recall_works`` — the real backend recall path (``_fanout_recall``
    + ``_apply_recall_db_side_effects``) runs against slim engines and returns a
    sane, non-crashing result — every engine the path touches is present.
  * ``test_slim_full_recall_parity`` — the SAME query returns byte-identical
    ranked ids/scores under slim vs full engines (built sequentially, shutdown
    between, because the engine singletons are process-global). If slim output
    differs from full, a needed engine was skipped → STOP.

The 13 SLIM engines (backend builds): _storage, _embeddings, _retriever, _kg,
_wiki, _engram, _rules_engine, _metacognition, _thermo, _cognitive_map, _buffer,
_replay, _pool.

The 11 CORE-ONLY engines (backend skips): _consolidation, _staleness, _curator,
_prospective, _narrative, _sleep, _write_gate, _causal, _cls, _file_queue,
_queue_drainer.

R2a Car B: _consolidation moved SLIM->CORE-ONLY. Post-Car-A `_pool` is standalone
(shared root builds it directly), so slim no longer needs consolidation for its
`_pool` attribute. Consolidation is now built full-only by core/bootstrap, which
removes the `_shared -> core.consolidation` edge. The parity tests below confirm
this is behavior-neutral for recall.
"""

from __future__ import annotations

import pytest

from yadgar.core import server

pytestmark = pytest.mark.usefixtures("recall_backend_bypass")

_MODEL = "all-MiniLM-L6-v2"
_DIR = "/tmp/slim-parity-dir"

# The 13 engines the SLIM backend build must populate.
# R2a Car B: _consolidation moved to CORE-ONLY. Post-Car-A `_pool` is built
# STANDALONE by the shared root, so consolidation is no longer needed in slim.
# Building it in slim would reintroduce the `_shared -> core.consolidation` edge
# this Car removes; the parity tests below prove recall is behavior-neutral
# without it.
SLIM_13 = [
    "_storage",
    "_embeddings",
    "_retriever",
    "_kg",
    "_wiki",
    "_engram",
    "_rules_engine",
    "_metacognition",
    "_thermo",
    "_cognitive_map",
    "_buffer",
    "_replay",
    "_pool",
]

# The 11 CORE-ONLY engines the SLIM build must leave None.
CORE_ONLY_11 = [
    "_consolidation",  # R2a Car B: full-only (yadgar.core.consolidation)
    "_staleness",
    "_curator",
    "_prospective",
    "_narrative",
    "_sleep",
    "_write_gate",
    "_causal",
    "_cls",
    "_file_queue",
    "_queue_drainer",
]


def _seed_memories(storage) -> None:
    """Insert a few branch=NONE (legacy, always-visible) memories."""
    for i, text in enumerate(
        [
            "slim parity storage engine architecture notes alpha",
            "slim parity retrieval fusion reranking pipeline beta",
            "slim parity knowledge graph entity extraction gamma",
        ]
    ):
        storage.insert_memory(
            {
                "content": f"{text} {i}",
                "directory_context": _DIR,
                "tags": ["test"],
                "heat": 1.0,
            },
            branch=None,
        )


def _run_backend_recall(query: str) -> list[dict]:
    """Exercise the exact backend /recall path: _fanout_recall + db side-effects."""
    from yadgar._shared.runtime.recall_pipeline import (
        _apply_recall_db_side_effects,
        _fanout_recall,
    )

    storage = server._get_storage()
    merged = _fanout_recall(
        query=query,
        max_results=5,
        min_heat=0.0,
        directory=_DIR,
        current_branch=None,
        default_branch="master",
        type_filter="all",
        tags=None,
        profile=None,
    )
    _apply_recall_db_side_effects(merged, query, storage)
    return merged


def _result_signature(results: list[dict]) -> list[tuple]:
    """Stable ranked signature: (id, rounded score) in returned order."""
    sig = []
    for r in results:
        score = r.get("_retrieval_score") or r.get("score") or 0.0
        sig.append((r.get("id"), round(float(score), 6)))
    return sig


def test_slim_builds_13_skips_11(tmp_path):
    """engine_set='slim' populates the 13 slim engines and skips the 11 core-only."""
    db_path = str(tmp_path / "slim.db")
    server.init_engines(db_path=db_path, embedding_model=_MODEL, engine_set="slim")
    try:
        import yadgar._shared.runtime.state as _st

        missing = [name for name in SLIM_13 if getattr(_st, name) is None]
        assert not missing, f"slim build MISSING required engines (backend would crash): {missing}"

        present = [name for name in CORE_ONLY_11 if getattr(_st, name) is not None]
        assert not present, f"slim build built CORE-ONLY engines it should skip: {present}"
    finally:
        server.shutdown()


def test_slim_recall_works(tmp_path):
    """The real backend recall path runs against slim engines without crashing."""
    db_path = str(tmp_path / "slim_recall.db")
    server.init_engines(db_path=db_path, embedding_model=_MODEL, engine_set="slim")
    try:
        _seed_memories(server._get_storage())
        results = _run_backend_recall("slim parity retrieval fusion")
        assert isinstance(results, list)
        assert results, "slim backend recall returned no results (engine missing?)"
    finally:
        server.shutdown()


def test_slim_landscape_recall_no_crash(tmp_path):
    """The backend LANDSCAPE path (mode='landscape') runs on slim without crashing.

    Landscape is the reason _pool is in the slim-14 — it reaches deeper than
    fanout (AstrocytePool.consensus_retrieve). This proves that path touches none
    of the 10 skipped engines (a missing one = backend crash on the first
    landscape /recall in prod).
    """
    from yadgar.backend.embed_service import _run_landscape_backend

    db_path = str(tmp_path / "slim_landscape.db")
    server.init_engines(db_path=db_path, embedding_model=_MODEL, engine_set="slim")
    try:
        storage = server._get_storage()
        _seed_memories(storage)
        results = _run_landscape_backend(
            query="slim parity knowledge graph",
            max_results=5,
            directory=_DIR,
            storage=storage,
        )
        assert isinstance(results, list)  # no crash; empty is acceptable
    finally:
        server.shutdown()


def test_slim_full_recall_parity(tmp_path):
    """SAME query → byte-identical ranked ids/scores under slim vs full engines.

    Built sequentially (shutdown between) because engine singletons are
    process-global. If the signatures differ, a needed engine was skipped in the
    slim set → the split is NOT behavior-neutral → STOP.
    """
    query = "slim parity retrieval fusion reranking pipeline"

    # SLIM build.
    slim_db = str(tmp_path / "parity_slim.db")
    server.init_engines(db_path=slim_db, embedding_model=_MODEL, engine_set="slim")
    try:
        _seed_memories(server._get_storage())
        slim_sig = _result_signature(_run_backend_recall(query))
    finally:
        server.shutdown()

    # FULL build (default engine_set).
    full_db = str(tmp_path / "parity_full.db")
    server.init_engines(db_path=full_db, embedding_model=_MODEL)
    try:
        _seed_memories(server._get_storage())
        full_sig = _result_signature(_run_backend_recall(query))
    finally:
        server.shutdown()

    assert slim_sig == full_sig, (
        "slim /recall output DIFFERS from full — a needed engine was skipped.\n"
        f"  slim: {slim_sig}\n  full: {full_sig}"
    )
