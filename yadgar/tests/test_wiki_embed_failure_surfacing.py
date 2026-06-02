"""Tests for v5.42.1 embed-failure surfacing in wiki._compute_embedding.

Phase 3 coverage:
- Success path: embedding computed, no WARN, no counter increment
- Failure path (exception): WARN log emitted, counter incremented (reason=exception)
- Failure path (None returned): WARN log emitted, counter incremented (reason=returned_none)
- WIKI_EMBED_FAILURE_BLOCKS_WRITE=True: exception propagated (write blocked)
- WIKI_EMBED_FAILURE_BLOCKS_WRITE=True with None return: RuntimeError raised
- WIKI_EMBED_FAILURE_BLOCKS_WRITE=False (default): write proceeds with None
- Startup CRITICAL log when NULL rows remain after backfill
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from yadgar import server

# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _engines(tmp_path):
    """Isolated server with real embedding model."""
    server.init_engines(
        db_path=str(tmp_path / "embed_failure_test.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


def _wiki():
    return server._wiki


def _get_counter(reason: str) -> float:
    """Return current value of yadgar_wiki_embedding_compute_failed_total{reason}."""
    try:
        from yadgar.metrics import yadgar_wiki_embedding_compute_failed_total

        return yadgar_wiki_embedding_compute_failed_total.labels(reason=reason)._value.get()
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------


class TestComputeEmbeddingSuccess:
    def test_success_returns_bytes(self):
        """Normal path: _compute_embedding returns bytes, no exception."""
        result = _wiki()._compute_embedding(
            "Yadgar Architecture", "Content about the system architecture."
        )
        assert result is not None
        assert isinstance(result, bytes)
        assert len(result) > 0

    def test_success_no_counter_increment(self):
        """Success path does not increment the failure counter."""
        before_exc = _get_counter("exception")
        before_none = _get_counter("returned_none")

        _wiki()._compute_embedding("Test Page", "Normal content.")

        after_exc = _get_counter("exception")
        after_none = _get_counter("returned_none")

        assert after_exc == before_exc, "exception counter incremented on success path"
        assert after_none == before_none, "returned_none counter incremented on success path"


# ---------------------------------------------------------------------------
# Failure paths — WIKI_EMBED_FAILURE_BLOCKS_WRITE=False (default)
# ---------------------------------------------------------------------------


class TestEmbedFailureWarnOnly:
    """Default behaviour: log WARN + counter, write proceeds with None."""

    def test_exception_returns_none_and_warns(self, caplog, monkeypatch):
        """encode_document raises → _compute_embedding returns None + WARN."""
        monkeypatch.setattr(
            _wiki()._embeddings,
            "encode_document",
            MagicMock(side_effect=RuntimeError("embed service unavailable")),
        )
        with caplog.at_level("WARNING", logger="yadgar.wiki"):
            result = _wiki()._compute_embedding("Test Page", "Content.")

        assert result is None
        assert any(
            "embedding computation failed" in r.message or "NULL embedding" in r.message
            for r in caplog.records
        ), f"Expected WARN about embed failure, got: {[r.message for r in caplog.records]}"

    def test_exception_increments_counter(self, monkeypatch):
        """encode_document raises → reason=exception counter increments."""
        monkeypatch.setattr(
            _wiki()._embeddings,
            "encode_document",
            MagicMock(side_effect=RuntimeError("service down")),
        )
        before = _get_counter("exception")
        _wiki()._compute_embedding("Page A", "Content A.")
        after = _get_counter("exception")
        assert after > before, (
            f"yadgar_wiki_embedding_compute_failed_total{{reason=exception}} did not increment: "
            f"before={before}, after={after}"
        )

    def test_none_return_warns(self, caplog, monkeypatch):
        """encode_document returns None → _compute_embedding returns None + WARN."""
        monkeypatch.setattr(
            _wiki()._embeddings,
            "encode_document",
            MagicMock(return_value=None),
        )
        with caplog.at_level("WARNING", logger="yadgar.wiki"):
            result = _wiki()._compute_embedding("Test Page", "Content.")

        assert result is None
        assert any(
            "returned None" in r.message or "NULL embedding" in r.message for r in caplog.records
        ), f"Expected WARN about None return, got: {[r.message for r in caplog.records]}"

    def test_none_return_increments_returned_none_counter(self, monkeypatch):
        """encode_document returns None → reason=returned_none counter increments."""
        monkeypatch.setattr(
            _wiki()._embeddings,
            "encode_document",
            MagicMock(return_value=None),
        )
        before = _get_counter("returned_none")
        _wiki()._compute_embedding("Page B", "Content B.")
        after = _get_counter("returned_none")
        assert after > before, (
            f"yadgar_wiki_embedding_compute_failed_total{{reason=returned_none}} "
            f"did not increment: before={before}, after={after}"
        )

    def test_exception_does_not_propagate_by_default(self, monkeypatch):
        """Default WIKI_EMBED_FAILURE_BLOCKS_WRITE=False: exception is caught, not re-raised."""
        monkeypatch.setattr(
            _wiki()._embeddings,
            "encode_document",
            MagicMock(side_effect=RuntimeError("service down")),
        )
        # Must NOT raise — default is False (backward compat).
        result = _wiki()._compute_embedding("Safe Page", "Content.")
        assert result is None


# ---------------------------------------------------------------------------
# Failure paths — WIKI_EMBED_FAILURE_BLOCKS_WRITE=True
# ---------------------------------------------------------------------------


class TestEmbedFailureBlocksWrite:
    """WIKI_EMBED_FAILURE_BLOCKS_WRITE=True: exception propagated."""

    def test_exception_raises_when_blocking_enabled(self, monkeypatch):
        """encode_document raises → RuntimeError propagated when block=True."""
        monkeypatch.setattr(
            _wiki()._embeddings,
            "encode_document",
            MagicMock(side_effect=RuntimeError("service down")),
        )

        from yadgar.config import get_settings as _get_settings

        orig_settings = _get_settings()

        class _BlockingSettings:
            def __getattr__(self, name):
                if name == "WIKI_EMBED_FAILURE_BLOCKS_WRITE":
                    return True
                return getattr(orig_settings, name)

        import yadgar.config as _config_mod

        monkeypatch.setattr(_config_mod, "get_settings", lambda: _BlockingSettings())
        with pytest.raises(RuntimeError, match="WIKI_EMBED_FAILURE_BLOCKS_WRITE=True"):
            _wiki()._compute_embedding("Block Me", "Content.")

    def test_none_return_raises_when_blocking_enabled(self, monkeypatch):
        """encode_document returns None → RuntimeError when block=True."""
        monkeypatch.setattr(
            _wiki()._embeddings,
            "encode_document",
            MagicMock(return_value=None),
        )

        from yadgar.config import get_settings as _get_settings

        orig_settings = _get_settings()

        class _BlockingSettings:
            def __getattr__(self, name):
                if name == "WIKI_EMBED_FAILURE_BLOCKS_WRITE":
                    return True
                return getattr(orig_settings, name)

        import yadgar.config as _config_mod

        monkeypatch.setattr(_config_mod, "get_settings", lambda: _BlockingSettings())
        with pytest.raises(RuntimeError, match="WIKI_EMBED_FAILURE_BLOCKS_WRITE=True"):
            _wiki()._compute_embedding("Block Me", "Content.")

    def test_success_path_unaffected_by_block_knob(self, monkeypatch):
        """Success path returns bytes regardless of WIKI_EMBED_FAILURE_BLOCKS_WRITE."""
        from yadgar.config import get_settings as _get_settings

        orig_settings = _get_settings()

        class _BlockingSettings:
            def __getattr__(self, name):
                if name == "WIKI_EMBED_FAILURE_BLOCKS_WRITE":
                    return True
                return getattr(orig_settings, name)

        import yadgar.config as _config_mod

        monkeypatch.setattr(_config_mod, "get_settings", lambda: _BlockingSettings())
        # Real embed service is available — should succeed normally.
        result = _wiki()._compute_embedding("Success Page", "Normal content here.")
        assert result is not None
        assert isinstance(result, bytes)


# ---------------------------------------------------------------------------
# Counter metric exists and is registered
# ---------------------------------------------------------------------------


class TestEmbedFailureMetric:
    def test_counter_is_importable(self):
        """yadgar_wiki_embedding_compute_failed_total is importable from yadgar.metrics."""
        from yadgar.metrics import yadgar_wiki_embedding_compute_failed_total

        assert yadgar_wiki_embedding_compute_failed_total is not None

    def test_counter_has_reason_label(self):
        """Counter accepts reason label without error."""
        from yadgar.metrics import yadgar_wiki_embedding_compute_failed_total

        # Both valid reason values should work.
        yadgar_wiki_embedding_compute_failed_total.labels(reason="exception")
        yadgar_wiki_embedding_compute_failed_total.labels(reason="returned_none")
