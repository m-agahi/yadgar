"""Tests for v4.4 async memorize() behavior.

Verifies:
  - memorize() returns immediately with {stored, queued, queue_id}
  - Early-reject paths (too-large, secret-detected) remain synchronous
  - Drain replay actually persists memories (searchable after flush)
  - Write gate skipped when WRITE_GATE_THRESHOLD <= 0
"""

from unittest.mock import patch

import pytest

from yadgar.core import server


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    """Initialize global engines with an isolated temp database per test."""
    tmp_path = tmp_path_factory.mktemp("memorize_async")
    server.init_engines(
        db_path=str(tmp_path / "async_test.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


# ── Fast-path shape ───────────────────────────────────────────────────────────


def test_memorize_returns_immediately_with_queued_status():
    """memorize() fast path returns {stored, queued, queue_id} without running heavy work."""
    result = server.memorize("fast path test content", "/tmp/test", ["test"])
    assert result["stored"] is True
    assert result["queued"] is True
    assert "queue_id" in result
    assert result["queue_id"]  # non-empty string


def test_memorize_response_has_no_id_on_fast_path():
    """The fast path does not return an 'id' field — callers must flush to get one."""
    result = server.memorize("no id on fast path", "/tmp/test", ["test"])
    assert "id" not in result


# ── Early-reject paths stay synchronous ──────────────────────────────────────


def test_memorize_too_large_returns_synchronously():
    """Content > 32 KB is rejected immediately without enqueueing."""
    # Check queue dir is empty before call
    queue_dir = server._get_file_queue().queue_dir
    before_count = len(list(queue_dir.glob("*.json")))

    big_content = "x" * (32_768 + 1)
    result = server.memorize(big_content, "/tmp/test", ["test"])

    after_count = len(list(queue_dir.glob("*.json")))
    assert result["stored"] is False
    assert result["reason"] == "content_too_large"
    assert after_count == before_count, "Oversized content should not be enqueued"


def test_memorize_secret_detected_returns_synchronously():
    """Content with a secret pattern is rejected immediately without enqueueing."""
    queue_dir = server._get_file_queue().queue_dir
    before_count = len(list(queue_dir.glob("*.json")))

    # Use a pattern that the secrets scanner recognises
    secret_content = (
        "My AWS key is AKIAIOSFODNN7EXAMPLE and secret is wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    )
    result = server.memorize(secret_content, "/tmp/test", ["test"])

    after_count = len(list(queue_dir.glob("*.json")))
    assert result["stored"] is False
    assert after_count == before_count, "Secret-containing content should not be enqueued"


# ── Drain replay persists memories ───────────────────────────────────────────


def test_memorize_drain_actually_persists(flush_queue, recall_backend_bypass):
    """memorize() + flush_queue() makes the memory searchable via recall()."""
    content = "drain replay persistence test unique content xyz"
    result = server.memorize(content, "/tmp/persist", ["test"])
    assert result["queued"] is True

    flush_queue()

    hits = server.recall(content[:50], directory="/tmp/persist")
    assert any(h["content"] == content for h in hits), "Memory was not found after drain replay"


def test_memorize_drain_preserves_context_and_tags(flush_queue, recall_backend_bypass):
    """After drain, the memory has the correct directory_context and tags."""
    content = "context and tags preservation test content abc"
    server.memorize(content, "/projects/myapp", ["infra", "v4test"])
    flush_queue()

    hits = server.recall(content[:50], directory="/projects/myapp")
    match = next((h for h in hits if h["content"] == content), None)
    assert match is not None
    assert match["directory_context"] == "/projects/myapp"
    assert "infra" in match["tags"]


# ── Write gate fast-path ──────────────────────────────────────────────────────
# R3 migration: _st._write_gate is None core-side (WriteGate lives in backend).
# Construct WriteGate directly from the live engines that _engines sets up.


def _make_write_gate():
    """Construct a WriteGate from the live engine stack (_engines fixture must be active)."""
    import yadgar._shared.runtime.state as _st
    from yadgar._shared.config import get_settings
    from yadgar.backend.predictive_coding import WriteGate

    assert _st._storage is not None, "WriteGate requires _engines fixture to be active"
    return WriteGate(
        storage=_st._storage,
        embeddings=_st._embeddings,
        retriever=_st._retriever,
        settings=get_settings(),
    )


def test_write_gate_skipped_when_threshold_zero():
    """WRITE_GATE_THRESHOLD=0.0 skips the full surprisal pipeline."""
    gate = _make_write_gate()

    gate._settings.WRITE_GATE_THRESHOLD = 0.0
    gate._threshold = 0.0
    with patch.object(gate, "compute_surprisal", wraps=gate.compute_surprisal) as mock_surprisal:
        should_store, surprisal, reason = gate.should_store(
            "some content to check gate",
            "/tmp",
            ["test"],
        )

    assert should_store is True
    assert surprisal == 0.0
    assert reason == "gate_disabled"
    mock_surprisal.assert_not_called()


def test_write_gate_positive_threshold_does_not_short_circuit():
    """With WRITE_GATE_THRESHOLD > 0, the gate still runs the surprisal pipeline."""
    gate = _make_write_gate()

    gate._settings.WRITE_GATE_THRESHOLD = 0.5
    gate._threshold = 0.5
    with patch.object(gate, "compute_surprisal", return_value=0.9) as mock_surprisal:
        should_store, surprisal, reason = gate.should_store(
            "high surprisal novel content qzxwvutsrqponmlkjihgfedcba",
            "/tmp",
            ["test"],
        )

    mock_surprisal.assert_called_once()
    assert should_store is True
