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

The 11 SLIM engines (shared root builds): _storage, _embeddings, _kg, _wiki,
_engram, _rules_engine, _metacognition, _thermo, _cognitive_map (session-side
SRTransitionRecorder since T2 Car B), _buffer, _pool. T2 Car E2: _retriever
left this set — retrieval sank to the backend; ensure_retrieval_engine
composes it (see BACKEND_COMPOSED).

T2 Car B: _replay (CheckpointRestore) left the shared root — it is composed
BACKEND-SIDE by yadgar.backend.restoration.ensure_restoration_engines (called
from embed_service._ensure_recall_engines), which also UPGRADES _cognitive_map
to the full CognitiveMap subclass. test_restoration_engines_composed_backend_side
covers that step.

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

from yadgar._shared.storage.directory import RecallScope
from yadgar.core import server
from yadgar.tests.core.conftest import TEST_PROJECT_ID

pytestmark = pytest.mark.usefixtures("recall_backend_bypass")

_MODEL = "all-MiniLM-L6-v2"
_DIR = "/tmp/slim-parity-dir"

# The 12 engines the SLIM shared-root build must populate.
# R2a Car B: _consolidation moved to CORE-ONLY. Post-Car-A `_pool` is built
# STANDALONE by the shared root, so consolidation is no longer needed in slim.
# Building it in slim would reintroduce the `_shared -> core.consolidation` edge
# this Car removes; the parity tests below prove recall is behavior-neutral
# without it.
# T2 Car B: `_replay` left this set — CheckpointRestore is a backend engine now,
# composed by ensure_restoration_engines (see BACKEND_COMPOSED below).
SLIM_12 = [
    "_storage",
    "_embeddings",
    "_kg",
    "_wiki",
    "_engram",
    "_rules_engine",
    "_metacognition",
    "_thermo",
    "_cognitive_map",
    "_buffer",
    "_pool",
]

# Engines composed backend-side AFTER init_engines (T2 Car B + E2):
# ensure_restoration_engines builds _replay + upgrades _cognitive_map;
# ensure_retrieval_engine builds _retriever (retrieval sank to the backend).
BACKEND_COMPOSED = ["_replay", "_retriever"]

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
    """Insert a few always-visible memories."""
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
                "project_id": TEST_PROJECT_ID,
            },
        )


def _run_backend_recall(query: str) -> list[dict]:
    """Exercise the exact backend /recall path: _fanout_recall + db side-effects."""
    from yadgar.backend.retrieval.compose import ensure_retrieval_engine
    from yadgar.backend.retrieval.recall_pipeline import (
        _apply_recall_db_side_effects,
        _fanout_recall,
    )

    # T2 Car E2: mirror embed_service._ensure_recall_engines — the shared root
    # no longer builds the retriever; the backend composes it lazily.
    ensure_retrieval_engine()

    storage = server._get_storage()
    merged = _fanout_recall(
        query=query,
        max_results=5,
        min_heat=0.0,
        recall_scope=RecallScope(project_id=TEST_PROJECT_ID),
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


def test_slim_builds_12_skips_11(tmp_path):
    """engine_set='slim' populates the 12 slim engines and skips the 11 core-only."""
    db_path = str(tmp_path / "slim.db")
    server.init_engines(db_path=db_path, embedding_model=_MODEL, engine_set="slim")
    try:
        import yadgar._shared.runtime.state as _st

        missing = [name for name in SLIM_12 if getattr(_st, name) is None]
        assert not missing, f"slim build MISSING required engines (backend would crash): {missing}"

        present = [name for name in CORE_ONLY_11 if getattr(_st, name) is not None]
        assert not present, f"slim build built CORE-ONLY engines it should skip: {present}"

        # T2 Car B: _replay is NOT built by the shared root anymore.
        composed = [name for name in BACKEND_COMPOSED if getattr(_st, name) is not None]
        assert not composed, f"shared root built backend-composed engines: {composed}"
    finally:
        server.shutdown()


def test_restoration_engines_composed_backend_side(tmp_path):
    """T2 Car B: ensure_restoration_engines builds _replay + upgrades _cognitive_map.

    The backend composition point (called from embed_service._ensure_recall_engines
    and the drainer's ensure_write_engines) must, on top of a slim shared-root
    build: (1) build CheckpointRestore into _st._replay, (2) upgrade the
    session-side SRTransitionRecorder in _st._cognitive_map to the full
    CognitiveMap compute subclass. Idempotent on second call.
    """
    from yadgar._shared.runtime.sr_session import SRTransitionRecorder
    from yadgar.backend.restoration import (
        CheckpointRestore,
        CognitiveMap,
        ensure_restoration_engines,
    )

    db_path = str(tmp_path / "slim_compose.db")
    server.init_engines(db_path=db_path, embedding_model=_MODEL, engine_set="slim")
    try:
        import yadgar._shared.runtime.state as _st

        # Shared root: session-side recorder only, no replay.
        assert isinstance(_st._cognitive_map, SRTransitionRecorder)
        assert not isinstance(_st._cognitive_map, CognitiveMap)
        assert _st._replay is None

        ensure_restoration_engines()
        assert isinstance(_st._cognitive_map, CognitiveMap)
        assert isinstance(_st._replay, CheckpointRestore)
        # CheckpointRestore must navigate on the SAME upgraded map instance.
        assert _st._replay._cognitive_map is _st._cognitive_map

        # Idempotent: second call keeps the same instances.
        replay_before, map_before = _st._replay, _st._cognitive_map
        ensure_restoration_engines()
        assert _st._replay is replay_before
        assert _st._cognitive_map is map_before
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
            project_id=TEST_PROJECT_ID,
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
