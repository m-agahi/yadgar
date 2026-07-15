"""Car 2 Part B — memorize soft-gate + memory_update re-embed (TDD).

Uses deterministic crafted embeddings (no sentence-transformers required) so the
tests exercise the wiring in any environment — mirrors test_write_time_contradiction.

memorize soft-gate (phase_soft_gate, non-blocking):
  * durable write (tags∩{feedback,decision,_anchor} / is_protected / tier) with a
    near-duplicate present → ctx.near_duplicates populated (>= threshold); the
    store phase is never blocked.
  * episodic write (no durable signals) → BYPASS (near_duplicates stays empty).
  * threshold boundary — a below-threshold neighbour is NOT surfaced.
  * MEMORIZE_SIM_GATE_ENABLED=false → gate off.
  * _build_response attaches near_duplicates to the memorize result.

memory_update re-embed (content-change guard):
  * content patch that CHANGES content → embedding re-encoded (vector changes).
  * same-value content OR tags-only patch → embedding UNCHANGED (no re-embed).

RED before implementation; GREEN after.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from yadgar._shared.storage import StorageEngine
from yadgar._shared.write_exec import MemorizeContext


@pytest.fixture
def storage(tmp_path):
    engine = StorageEngine(str(tmp_path / "partb.db"), embedding_dim=4)
    yield engine
    engine.close()


def _vec(*xs) -> list[float]:
    return [float(x) for x in xs]


def _make_ctx(tags, *, is_protected=False, tier=None, embedding=None) -> MemorizeContext:
    ctx = MemorizeContext(
        content="candidate content",
        context="/tmp/partb",
        tags=tags,
        is_protected=is_protected,
        provenance_agent=None,
        tier=tier,
        valid_until=None,
        ttl_days=None,
        reason="",
        branch_hint="master",
    )
    ctx.embedding = embedding
    return ctx


class _Settings:
    MEMORIZE_SIM_GATE_ENABLED = True
    MEMORIZE_SIM_THRESHOLD = 0.85
    MEMORIZE_SIM_TOP_K = 3


# ── memorize soft-gate: _is_durable_write ─────────────────────────────────────


class TestIsDurableWrite:
    def test_durable_signals(self):
        from yadgar.backend.write_exec._memorize_phases._phase_soft_gate import (
            _is_durable_write,
        )

        assert _is_durable_write(_make_ctx(["feedback"])) is True
        assert _is_durable_write(_make_ctx(["decision"])) is True
        assert _is_durable_write(_make_ctx(["_anchor"])) is True
        assert _is_durable_write(_make_ctx([], is_protected=True)) is True
        assert _is_durable_write(_make_ctx([], tier="conditional")) is True
        assert _is_durable_write(_make_ctx([], tier="ephemeral")) is True

    def test_episodic_signals(self):
        from yadgar.backend.write_exec._memorize_phases._phase_soft_gate import (
            _is_durable_write,
        )

        assert _is_durable_write(_make_ctx(["misc", "note"])) is False
        assert _is_durable_write(_make_ctx([])) is False


# ── memorize soft-gate: phase_soft_gate (crafted embeddings) ──────────────────


class TestPhaseSoftGate:
    def _patch_storage(self, monkeypatch, storage):
        import yadgar._shared.runtime.lifecycle as _lc

        monkeypatch.setattr(_lc, "_get_storage", lambda: storage)

    def _seed(self, storage, content, embedding):
        return storage.insert_memory(
            {
                "content": content,
                "tags": ["decision"],
                "store_type": "semantic",
                "heat": 0.9,
                "directory_context": "/tmp/partb",
                "embedding": storage._floats_to_bytes(embedding),
                "embedding_model": "crafted",
            }
        )

    def test_durable_write_surfaces_near_duplicate(self, monkeypatch, storage):
        """A durable write whose vector matches an existing memory → near_duplicates."""
        from yadgar.backend.write_exec._memorize_phases import _phase_soft_gate

        self._patch_storage(monkeypatch, storage)
        mid = self._seed(storage, "prior decision", _vec(1, 0, 0, 0))

        ctx = _make_ctx(["decision"], embedding=_vec(1, 0, 0, 0))  # identical → cosine 1.0
        _phase_soft_gate.phase_soft_gate(ctx, _Settings())

        assert ctx.near_duplicates, "durable near-dup write must populate near_duplicates"
        top = ctx.near_duplicates[0]
        assert top["id"] == mid
        assert top["score"] >= 0.85
        assert "content" in top

    def test_episodic_write_bypasses(self, monkeypatch, storage):
        from yadgar.backend.write_exec._memorize_phases import _phase_soft_gate

        self._patch_storage(monkeypatch, storage)
        self._seed(storage, "prior", _vec(1, 0, 0, 0))

        ctx = _make_ctx(["misc"], embedding=_vec(1, 0, 0, 0))  # no durable signal
        _phase_soft_gate.phase_soft_gate(ctx, _Settings())
        assert ctx.near_duplicates == [], "episodic write must bypass the gate"

    def test_below_threshold_not_surfaced(self, monkeypatch, storage):
        from yadgar.backend.write_exec._memorize_phases import _phase_soft_gate

        self._patch_storage(monkeypatch, storage)
        self._seed(storage, "orthogonal prior", _vec(1, 0, 0, 0))

        ctx = _make_ctx(["decision"], embedding=_vec(0, 1, 0, 0))  # orthogonal → cosine 0
        _phase_soft_gate.phase_soft_gate(ctx, _Settings())
        assert ctx.near_duplicates == [], "below-threshold neighbour must not surface"

    def test_gate_disabled(self, monkeypatch, storage):
        from yadgar.backend.write_exec._memorize_phases import _phase_soft_gate

        self._patch_storage(monkeypatch, storage)
        self._seed(storage, "prior", _vec(1, 0, 0, 0))

        class _Off(_Settings):
            MEMORIZE_SIM_GATE_ENABLED = False

        ctx = _make_ctx(["decision"], embedding=_vec(1, 0, 0, 0))
        _phase_soft_gate.phase_soft_gate(ctx, _Off())
        assert ctx.near_duplicates == [], "disabled gate must leave near_duplicates empty"

    def test_no_embedding_is_noop(self, monkeypatch, storage):
        from yadgar.backend.write_exec._memorize_phases import _phase_soft_gate

        self._patch_storage(monkeypatch, storage)
        ctx = _make_ctx(["decision"], embedding=None)
        _phase_soft_gate.phase_soft_gate(ctx, _Settings())
        assert ctx.near_duplicates == []


class TestBuildResponseAttachesDups:
    def test_near_duplicates_in_response(self, monkeypatch):
        """_build_response includes near_duplicates when ctx has them."""
        import yadgar.backend.write_exec._memorize_phases._phase_post_write as pw

        ctx = _make_ctx(["decision"], embedding=_vec(1, 0, 0, 0))
        ctx.memory_id = 42
        ctx.near_duplicates = [{"id": 7, "content": "dup", "score": 0.91}]
        ctx.curation_action = "created"

        storage = MagicMock()
        storage.get_memory.return_value = {"id": 42, "content": "candidate", "heat": 1.0}

        class _S:
            CRDT_AGENT_ID = "agent"

        monkeypatch.setattr(pw, "_push_event", lambda *_a, **_k: None)
        result = pw._build_response(ctx, storage, _S())
        assert result.get("near_duplicates") == [{"id": 7, "content": "dup", "score": 0.91}]


# ── memory_update re-embed (content-change guard) ─────────────────────────────


class TestMemoryUpdateReembed:
    """Drives the backend memory_update impl with a mocked embeddings engine so the
    re-embed guard is exercised deterministically (no sentence-transformers)."""

    def _patch(self, monkeypatch, storage, embeddings):
        import yadgar._shared.runtime.state as _st
        import yadgar.backend.admin_exec.memory as mem

        monkeypatch.setattr(_st, "_storage", storage, raising=False)
        monkeypatch.setattr(mem, "_get_embeddings", lambda: embeddings)

    def _insert(self, storage, content, embedding):
        return storage.insert_memory(
            {
                "content": content,
                "tags": ["t"],
                "store_type": "episodic",
                "heat": 0.7,
                "directory_context": "/tmp/reembed",
                "embedding": storage._floats_to_bytes(embedding),
                "embedding_model": "crafted",
            }
        )

    def _emb(self, storage, mid):
        m = storage.get_memory(mid)
        emb = m.get("embedding") if m else None
        return bytes(emb) if isinstance(emb, (bytes, bytearray)) else emb

    def test_content_change_reembeds(self, monkeypatch, storage):
        from yadgar.backend.admin_exec.memory import memory_update

        embeddings = MagicMock()
        embeddings.model_name = "crafted"
        # encode_batch returns list[bytes | None] (real EmbeddingEngine contract).
        embeddings.encode_batch.return_value = [storage._floats_to_bytes(_vec(0, 0, 0, 1))]
        self._patch(monkeypatch, storage, embeddings)

        mid = self._insert(storage, "old text", _vec(1, 0, 0, 0))
        before = self._emb(storage, mid)
        memory_update({"memory_id": mid, "fields": {"content": "brand new text"}})
        after = self._emb(storage, mid)
        embeddings.encode_batch.assert_called_once()
        assert before != after, "content change must re-embed the vector"

    def test_tags_only_does_not_reembed(self, monkeypatch, storage):
        from yadgar.backend.admin_exec.memory import memory_update

        embeddings = MagicMock()
        embeddings.model_name = "crafted"
        self._patch(monkeypatch, storage, embeddings)

        mid = self._insert(storage, "stable text", _vec(1, 0, 0, 0))
        before = self._emb(storage, mid)
        memory_update({"memory_id": mid, "fields": {"tags": ["a", "b"]}})
        after = self._emb(storage, mid)
        embeddings.encode_batch.assert_not_called()
        assert before == after, "tags-only patch must NOT re-embed"

    def test_same_value_content_does_not_reembed(self, monkeypatch, storage):
        from yadgar.backend.admin_exec.memory import memory_update

        embeddings = MagicMock()
        embeddings.model_name = "crafted"
        self._patch(monkeypatch, storage, embeddings)

        text = "unchanged text"
        mid = self._insert(storage, text, _vec(1, 0, 0, 0))
        before = self._emb(storage, mid)
        memory_update({"memory_id": mid, "fields": {"content": text}})
        after = self._emb(storage, mid)
        embeddings.encode_batch.assert_not_called()
        assert before == after, "same-value content must NOT re-embed"
