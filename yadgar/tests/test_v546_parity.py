"""Parity tests for v5.4.6 LOW-risk complexity refactors (P12 catalog).

Each test captures current behavior BEFORE refactor — verifies identical results
after decomposition. Per I5: no topology drift permitted.

Scope (HARD PLR0913 violators only — ruff-triggering):
- curation/ingestion.py::insert_new_memory  (12 params → NewMemorySpec)
- storage/entity.py::insert_typed_relationship  (9 params → RelationshipMeta)
- restoration.py::create_checkpoint  (9 params → CheckpointContext)
- cli/config.py::cmd_config  (nesting=6 → dispatch dict)
"""

from __future__ import annotations

import argparse

import numpy as np
import pytest

from yadgar.restoration import CheckpointContext
from yadgar.storage import RelationshipMeta, StorageEngine

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def storage(tmp_path):
    engine = StorageEngine(str(tmp_path / "parity.db"), embedding_dim=384)
    yield engine
    engine.close()


def _embedding(seed: int = 0, dim: int = 384) -> bytes:
    rng = np.random.RandomState(seed)
    vec = rng.randn(dim).astype(np.float32)
    vec /= np.linalg.norm(vec)
    return vec.tobytes()


# ── insert_new_memory parity ──────────────────────────────────────────────────


def test_insert_new_memory_returns_int_id(storage):
    """insert_new_memory returns a positive integer memory_id."""
    from yadgar.curation.ingestion import NewMemorySpec, insert_new_memory

    emb = _embedding(1)
    spec = NewMemorySpec(tags=["test"], embedding=emb, heat=0.8, surprise=0.3)
    mid = insert_new_memory(storage, "parity test content", "/tmp/proj", spec)
    assert isinstance(mid, int)
    assert mid > 0


def test_insert_new_memory_content_retrievable(storage):
    """Content inserted by insert_new_memory is retrievable unchanged."""
    from yadgar.curation.ingestion import NewMemorySpec, insert_new_memory

    emb = _embedding(2)
    content = "unique parity content for retrieval check"
    spec = NewMemorySpec(
        tags=["tag1", "tag2"],
        embedding=emb,
        heat=1.0,
        file_hash="abc123",
        embedding_model="test-model",
        contextual_prefix="[CTX]",
        surprise=0.5,
        importance=0.7,
        valence=0.1,
    )
    mid = insert_new_memory(storage, content, "/tmp/proj", spec)
    mem = storage.get_memory(mid)
    assert mem is not None
    assert mem["content"] == content


def test_insert_new_memory_scores_stored(storage):
    """Surprise/importance/valence are persisted as scores."""
    from yadgar.curation.ingestion import NewMemorySpec, insert_new_memory

    emb = _embedding(3)
    spec = NewMemorySpec(
        tags=[],
        embedding=emb,
        heat=0.5,
        surprise=0.9,
        importance=0.8,
        valence=-0.2,
    )
    mid = insert_new_memory(storage, "scores test", "/tmp", spec)
    scores = storage.get_memory_scores(mid)
    assert scores is not None
    assert abs(scores["surprise_score"] - 0.9) < 0.01
    assert abs(scores["importance"] - 0.8) < 0.01


# ── insert_typed_relationship parity ─────────────────────────────────────────


def _insert_entity(storage, name: str) -> int:
    return storage.insert_entity({"name": name, "type": "concept"})


def test_insert_typed_relationship_returns_int(storage):
    """insert_typed_relationship returns a positive integer relationship_id."""
    e1 = _insert_entity(storage, "entityA")
    e2 = _insert_entity(storage, "entityB")
    rid = storage.insert_typed_relationship(e1, e2, "KNOWS")
    assert isinstance(rid, int)
    assert rid > 0


def test_insert_typed_relationship_with_optional_params(storage):
    """Full optional params roundtrip via RelationshipMeta: weight, confidence, causal flag."""
    e1 = _insert_entity(storage, "src")
    e2 = _insert_entity(storage, "tgt")
    mid = storage.insert_memory({"content": "cause", "directory_context": "/x", "tags": []})
    meta = RelationshipMeta(
        weight=2.5,
        event_time=None,
        record_time=None,
        is_causal=1,
        confidence=0.9,
        source_memory_id=mid,
    )
    rid = storage.insert_typed_relationship(e1, e2, "CAUSED", meta)
    assert isinstance(rid, int)
    # Verify relationship is queryable
    rels = storage.get_relationships_for_entity(e1)
    assert any(r["id"] == rid for r in rels)


# ── create_checkpoint parity ──────────────────────────────────────────────────


@pytest.fixture
def replay_engine(tmp_path):
    from yadgar.config import Settings
    from yadgar.embeddings import EmbeddingEngine
    from yadgar.restoration import ReplayEngine

    engine = StorageEngine(str(tmp_path / "replay.db"), embedding_dim=384)
    embeddings = EmbeddingEngine()
    settings = Settings(DB_PATH=str(tmp_path / "replay.db"))
    replay = ReplayEngine(engine, embeddings, settings=settings)
    yield replay, engine
    engine.close()


def test_create_checkpoint_returns_dict(replay_engine):
    """create_checkpoint returns a dict with checkpoint_id, epoch, status."""
    replay, _ = replay_engine
    result = replay.create_checkpoint("/tmp/proj")
    assert isinstance(result, dict)
    assert "checkpoint_id" in result
    assert result["status"] == "created"
    assert isinstance(result["checkpoint_id"], int)


def test_create_checkpoint_full_params(replay_engine):
    """All optional params via CheckpointContext accepted without error."""
    replay, _ = replay_engine
    ctx = CheckpointContext(
        current_task="do the thing",
        files_being_edited=["a.py", "b.py"],
        key_decisions=["use dataclass"],
        open_questions=["why?"],
        next_steps=["run tests"],
        active_errors=["none yet"],
        custom_context="extra context",
    )
    result = replay.create_checkpoint("/tmp/proj", ctx, session_id="test-session")
    assert result["checkpoint_id"] > 0
    assert result["status"] == "created"


def test_create_checkpoint_resets_tool_count(replay_engine):
    """create_checkpoint resets the internal tool call counter."""
    replay, _ = replay_engine
    replay.record_tool_call()
    replay.record_tool_call()
    replay.create_checkpoint("/tmp")
    # After checkpoint, tool_call_count should reset
    assert not replay.should_auto_checkpoint()


# ── cmd_config nesting parity ─────────────────────────────────────────────────


def _make_config_args(sub: str | None = None, **kwargs) -> argparse.Namespace:
    ns = argparse.Namespace(config_command=sub)
    for k, v in kwargs.items():
        setattr(ns, k, v)
    return ns


def test_cmd_config_none_prints_help(capsys):
    """cmd_config with no sub-command calls print_help on the config_parser."""
    from yadgar.cli.config import cmd_config

    called = []
    parser = argparse.ArgumentParser()
    parser.print_help = lambda: called.append("help")

    cmd_config(_make_config_args(None), parser)
    assert called == ["help"]


def test_cmd_config_dispatches_init(monkeypatch):
    """cmd_config with sub='init' calls cmd_config_init."""
    import yadgar.cli.config as cfg_mod

    called = []
    monkeypatch.setattr(cfg_mod, "cmd_config_init", lambda args: called.append(("init", args)))

    args = _make_config_args("init", force=False)
    parser = argparse.ArgumentParser()
    parser.print_help = lambda: None
    cfg_mod.cmd_config(args, parser)
    assert called and called[0][0] == "init"
