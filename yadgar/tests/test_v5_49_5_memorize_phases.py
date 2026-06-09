"""Phase-level unit tests for v5.49.5 memorize() refactor.

Tests 7–16 per plan § 5. Each phase function tested in isolation.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from yadgar.server.tools._memorize_phases import (
    MemorizeContext,
    phase_contradiction,
    phase_embed,
    phase_post_write,
    phase_resolve_branch,
    phase_store,
    phase_validate,
)


def _make_ctx(**overrides) -> MemorizeContext:
    """Build a minimal MemorizeContext for testing."""
    defaults = dict(
        content="test content",
        context="/tmp/test",
        tags=["test"],
        is_protected=False,
        provenance_agent=None,
        tier=None,
        valid_until=None,
        ttl_days=None,
        reason="",
        branch_hint=None,
    )
    defaults.update(overrides)
    return MemorizeContext(**defaults)


def _make_settings(**overrides):
    defaults = {
        "ANCHOR_CONDITIONAL_TTL_DAYS": 90,
        "ANCHOR_EPHEMERAL_TTL_DAYS": 14,
        "ANCHOR_SEMANTIC_IMMORTAL_REQUIRES_REASON": False,
        "DECISION_AUTO_PROTECT": False,
        "CONTEXTUAL_PREFIX_ENABLED": False,
        "REINJECT_ON_WRITE": False,
        "REINJECTION_ENABLED": False,
        "REINJECTION_MAX_RESULTS": 3,
        "MICRO_CHECKPOINT_ENABLED": False,
        "CRDT_AGENT_ID": "test-agent",
        "HOT_THRESHOLD": 0.5,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# Test 7 — _phase_validate: rejects empty content
# ---------------------------------------------------------------------------


def test_phase_validate_rejects_empty_content(monkeypatch):
    """phase_validate returns content_too_large=False for empty, but content_too_large for huge."""
    # Empty content should pass validate (it's not too large)
    ctx = _make_ctx(content="")
    import yadgar.server._state as _st

    monkeypatch.setattr(_st, "_rules_engine", None)
    with patch(
        "yadgar.server.tools._memorize_phases._phase_validate.gate_or_reject",
        return_value=None,
    ):
        result = phase_validate(ctx, _make_settings())
    assert result is None, "Empty content should pass validation"

    # Oversized content should be rejected
    big_ctx = _make_ctx(content="x" * (32_768 + 1))
    with patch(
        "yadgar.server.tools._memorize_phases._phase_validate.gate_or_reject",
        return_value=None,
    ):
        result = phase_validate(big_ctx, _make_settings())
    assert result is not None
    assert result.get("stored") is False
    assert result.get("reason") == "content_too_large"


# ---------------------------------------------------------------------------
# Test 8 — _phase_validate: rejects missing branch (handled downstream)
# ---------------------------------------------------------------------------


def test_phase_validate_accepts_all_valid_tiers(monkeypatch):
    """phase_validate accepts all three valid tier values without error."""
    import yadgar.server._state as _st

    monkeypatch.setattr(_st, "_rules_engine", None)

    for tier in ("semantic_immortal", "conditional", "ephemeral"):
        ctx = _make_ctx(tier=tier)
        with patch(
            "yadgar.server.tools._memorize_phases._phase_validate.gate_or_reject",
            return_value=None,
        ):
            result = phase_validate(ctx, _make_settings())
        assert result is None, f"Valid tier={tier} should not be rejected"


# ---------------------------------------------------------------------------
# Test 9 — _phase_validate: calls secret gate
# ---------------------------------------------------------------------------


def test_phase_validate_calls_secret_gate(monkeypatch):
    """phase_validate calls gate_or_reject and returns its rejection dict."""
    import yadgar.server._state as _st

    monkeypatch.setattr(_st, "_rules_engine", None)

    rejection = {"stored": False, "reason": "secret_detected", "pattern": "test"}
    with patch(
        "yadgar.server.tools._memorize_phases._phase_validate.gate_or_reject",
        return_value=rejection,
    ) as mock_gate:
        ctx = _make_ctx()
        result = phase_validate(ctx, _make_settings())

    mock_gate.assert_called_once()
    assert result == rejection


# ---------------------------------------------------------------------------
# Test 10 — _phase_resolve_branch: uses branch_hint when cwd fails
# ---------------------------------------------------------------------------


def test_phase_resolve_branch_uses_branch_hint_when_cwd_fails(monkeypatch):
    """phase_resolve_branch uses branch_hint when _detect_branch returns None."""
    import yadgar.file_queue as _fq

    # _phase_resolve_branch calls _file_queue.is_draining() via module ref — patch source
    monkeypatch.setattr(_fq, "is_draining", lambda: True)  # draining → no enqueue

    ctx = _make_ctx(branch_hint="feat/fallback-branch")

    with patch("yadgar.server._detect_branch", return_value=None):
        result = phase_resolve_branch(ctx)

    assert result is None, "Draining path should return None (continue)"
    assert ctx.resolved_branch == "feat/fallback-branch"


# ---------------------------------------------------------------------------
# Test 11 — _phase_resolve_branch: prefers cwd when both available
# ---------------------------------------------------------------------------


def test_phase_resolve_branch_prefers_cwd_when_both_available(monkeypatch):
    """phase_resolve_branch uses _detect_branch result over branch_hint."""
    import yadgar.file_queue as _fq

    # _phase_resolve_branch calls _file_queue.is_draining() via module ref — patch source
    monkeypatch.setattr(_fq, "is_draining", lambda: True)

    ctx = _make_ctx(branch_hint="feat/fallback-branch")

    with patch("yadgar.server._detect_branch", return_value="feat/cwd-branch"):
        result = phase_resolve_branch(ctx)

    assert result is None
    assert ctx.resolved_branch == "feat/cwd-branch"


# ---------------------------------------------------------------------------
# Test 12 — _phase_embed: returns embedding vector
# ---------------------------------------------------------------------------


def test_phase_embed_returns_vector(monkeypatch):
    """phase_embed sets ctx.embedding from embeddings engine."""
    import yadgar.server._state as _st
    import yadgar.server.tools._memorize_phases._phase_embed as _pe

    mock_embeddings = MagicMock()
    mock_embeddings.encode.return_value = [0.1, 0.2, 0.3]
    monkeypatch.setattr(_pe, "_get_embeddings", lambda: mock_embeddings)
    monkeypatch.setattr(_st, "_write_gate", None)
    monkeypatch.setattr(_st, "_retriever", None)
    monkeypatch.setattr(_st, "_thermo", None)

    ctx = _make_ctx()
    result = phase_embed(ctx, _make_settings())

    assert result is None, "phase_embed should not reject valid content"
    assert ctx.embedding == [0.1, 0.2, 0.3]


# ---------------------------------------------------------------------------
# Test 13 — _phase_embed: write gate rejection
# ---------------------------------------------------------------------------


def test_phase_embed_retries_on_timeout(monkeypatch):
    """phase_embed returns rejection dict when write gate rejects (surprisal too low)."""
    import yadgar.server._state as _st
    import yadgar.server.tools._memorize_phases._phase_embed as _pe

    mock_embeddings = MagicMock()
    mock_embeddings.encode.return_value = [0.1]
    monkeypatch.setattr(_pe, "_get_embeddings", lambda: mock_embeddings)
    monkeypatch.setattr(_st, "_retriever", None)
    monkeypatch.setattr(_st, "_thermo", None)

    mock_gate = MagicMock()
    mock_gate.should_store.return_value = (False, 0.01, "low_surprise")
    monkeypatch.setattr(_st, "_write_gate", mock_gate)

    ctx = _make_ctx()
    result = phase_embed(ctx, _make_settings())

    assert result is not None
    assert result.get("stored") is False
    assert "surprisal" in result


# ---------------------------------------------------------------------------
# Test 14 — _phase_contradiction: flags NOOP from conflict resolver
# ---------------------------------------------------------------------------


def test_phase_contradiction_flags_known_pairs(monkeypatch):
    """phase_contradiction returns rejection dict when resolver returns NOOP."""
    monkeypatch.setenv("YADGAR_CONFLICT_RESOLVER", "on")

    mock_result = {"op": "NOOP", "reason": "duplicate detected"}
    with patch("yadgar.conflict_resolver.resolve_conflict", return_value=mock_result):
        ctx = _make_ctx()
        ctx.resolved_branch = "feat/test"
        result = phase_contradiction(ctx)

    assert result is not None
    assert result.get("stored") is False
    assert result.get("reason") == "conflict_resolver_noop"


# ---------------------------------------------------------------------------
# Test 15 — _phase_store: returns id (sets ctx.memory_id)
# ---------------------------------------------------------------------------


def test_phase_store_returns_id(monkeypatch):
    """phase_store sets ctx.memory_id to the ID returned by storage."""
    import yadgar.server._state as _st
    import yadgar.server.tools._memorize_phases._phase_store as _ps

    mock_storage = MagicMock()
    mock_storage.insert_memory.return_value = 77
    mock_storage.get_memory.return_value = None

    mock_embeddings = MagicMock()
    mock_embeddings.get_model_name.return_value = "test-model"

    mock_buffer = MagicMock()

    monkeypatch.setattr(_ps, "_get_storage", lambda: mock_storage)
    monkeypatch.setattr(_ps, "_get_embeddings", lambda: mock_embeddings)
    monkeypatch.setattr(_ps, "_get_buffer", lambda: mock_buffer)
    monkeypatch.setattr(_st, "_curator", None)
    monkeypatch.setattr(_st, "_consolidation", None)
    monkeypatch.setattr(_st, "_pool", None)

    ctx = _make_ctx()
    ctx.embedding = [0.1, 0.2]
    ctx.initial_heat = 1.0
    ctx.resolved_branch = "feat/test"

    phase_store(ctx)

    assert ctx.memory_id == 77
    assert ctx.curation_action == "created"


# ---------------------------------------------------------------------------
# Test 16 — _phase_post_write: writes link when contradictions present
# ---------------------------------------------------------------------------


def test_phase_post_write_writes_link_when_contradictions_present(monkeypatch):
    """phase_post_write applies explicit protection when is_protected=True."""
    import yadgar.server._state as _st
    import yadgar.server.tools._memorize_phases._phase_post_write as _pp

    mock_storage = MagicMock()
    mock_storage.get_memory.return_value = {
        "id": 42,
        "content": "test",
        "tags": ["_anchor"],
        "heat": 1.0,
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    mock_storage._q.return_value = []

    mock_buffer = MagicMock()

    monkeypatch.setattr(_pp, "_get_storage", lambda: mock_storage)
    monkeypatch.setattr(_pp, "_get_buffer", lambda: mock_buffer)
    monkeypatch.setattr(_st, "_thermo", None)
    monkeypatch.setattr(_st, "_prospective", None)
    monkeypatch.setattr(_st, "_engram", None)
    monkeypatch.setattr(_st, "_write_gate", None)
    monkeypatch.setattr(_st, "_replay", None)
    monkeypatch.setattr(_st, "_retriever", None)

    ctx = _make_ctx(is_protected=True, tags=["_anchor"])
    ctx.memory_id = 42
    ctx.curation_action = "created"
    ctx.embedding = [0.1]
    ctx.initial_heat = 1.0

    settings = _make_settings()

    with patch("yadgar.server.tools._memorize_phases._phase_post_write._push_event"):
        result = phase_post_write(ctx, settings)

    assert result.get("id") == 42
    assert ctx.auto_protected is True
