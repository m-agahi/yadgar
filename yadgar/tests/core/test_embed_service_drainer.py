"""Backend lifespan starts the QueueDrainer (P0 fix, backend 5.30.1).

R3 Car 1 (87143dd0) moved QueueDrainer core→backend and removed the
construction from core ``_get_file_queue`` with the comment "started by the
backend lifecycle half" — but no backend startup code ever built it. Production
backend 5.30.0 therefore NEVER drained the file queue: writes enqueued by core
sat in queue/ forever (observed: 50+ min old entries, zero drainer log lines).

These tests pin the missing wiring:
  1. lifespan constructs + starts a QueueDrainer against YADGAR_QUEUE_BASE
     with the real settings-driven drain_interval + DrainerConfig,
  2. the recall engine stack (``_ensure_recall_engines``) is initialised BEFORE
     the drainer starts (the write replay impls read ``_st._storage``),
  3. the drainer stops cleanly on shutdown,
  4. missing YADGAR_QUEUE_BASE → drainer skipped (unit tests / core-only),
  5. unwritable queue base → fail-loud ERROR + gauge 0.
"""

from __future__ import annotations

import importlib
import logging
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reload_embed_service(monkeypatch, tmp_path):
    """Reload embed_service with isolated marker/db paths (v5.3.0 test pattern)."""
    monkeypatch.setenv("YADGAR_SHUTDOWN_MARKER_PATH", str(tmp_path / ".shutdown_clean"))
    monkeypatch.setenv("YADGAR_DB_PATH", str(tmp_path / "surreal_db"))
    monkeypatch.setenv("YADGAR_ALLOW_ROOT", "1")

    import yadgar._shared.config as cfg

    cfg.get_settings.cache_clear()

    import yadgar.backend.embed_service as es

    importlib.reload(es)
    return es


def _lifespan_client(es):
    from fastapi.testclient import TestClient

    return TestClient(es.app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# 1+2. Startup wiring
# ---------------------------------------------------------------------------


class TestDrainerStartup:
    def test_lifespan_starts_drainer_with_settings_wiring(self, monkeypatch, tmp_path):
        """Lifespan builds FileQueue at YADGAR_QUEUE_BASE + QueueDrainer from settings.

        Also pins: engine bootstrap (_ensure_recall_engines) runs BEFORE the
        drainer thread starts, so ensure_write_engines + replay impls see live
        _st._storage on the first drain pass.
        """
        qbase = tmp_path / "qbase"
        monkeypatch.setenv("YADGAR_QUEUE_BASE", str(qbase))
        # Non-default knobs to prove settings (not literals) feed the drainer.
        monkeypatch.setenv("YADGAR_QUEUE_DRAIN_INTERVAL", "7")
        monkeypatch.setenv("YADGAR_QUEUE_MAX_PERMANENT_ATTEMPTS", "5")
        monkeypatch.setenv("YADGAR_QUEUE_DLQ_RETENTION_DAYS", "11")

        es = _reload_embed_service(monkeypatch, tmp_path)

        engine_calls: list[str] = []

        def _fake_engines():
            # Ordering pin: drainer must NOT exist yet when engines init.
            assert es._queue_drainer is None, "engines must init before drainer start"
            engine_calls.append("engines")

        with (
            patch.object(es, "_get_engine", return_value=MagicMock()),
            patch.object(es, "_ensure_recall_engines", side_effect=_fake_engines),
        ):
            with _lifespan_client(es) as client:
                drainer = es._queue_drainer
                assert drainer is not None, "lifespan did not start a QueueDrainer"
                assert drainer.is_alive(), "drainer thread not running"

                # Real config knobs honored (not hardcoded defaults)
                assert drainer._drain_interval == 7.0
                assert drainer._max_permanent == 5
                assert drainer._dlq_retention_days == 11

                # FileQueue rooted at YADGAR_QUEUE_BASE (R3 Car 0 /queue-data mount)
                assert drainer._queue.queue_dir == qbase / "queue"

                # Engines initialised exactly once, before start
                assert engine_calls == ["engines"]

                # Shared runtime slot assigned (dlq tools / drain_now helpers)
                import yadgar._shared.runtime.state as _st

                assert _st._queue_drainer is drainer

                # Observability: gauge 1 + /health payload field
                import yadgar.backend.embed_service_metrics as esm

                assert esm.embed_drainer_running._value.get() == 1.0
                assert client.get("/health").json()["drainer"] is True

    def test_lifespan_stops_drainer_on_shutdown(self, monkeypatch, tmp_path):
        """Drainer thread stops cleanly when the app shuts down."""
        monkeypatch.setenv("YADGAR_QUEUE_BASE", str(tmp_path / "qbase"))
        es = _reload_embed_service(monkeypatch, tmp_path)

        with (
            patch.object(es, "_get_engine", return_value=MagicMock()),
            patch.object(es, "_ensure_recall_engines", MagicMock()),
        ):
            with _lifespan_client(es) as _:
                drainer = es._queue_drainer
                assert drainer is not None

        assert not drainer.is_alive(), "drainer thread still alive after shutdown"
        assert drainer._stop_event.is_set()
        assert es._queue_drainer is None

        import yadgar.backend.embed_service_metrics as esm

        assert esm.embed_drainer_running._value.get() == 0.0


# ---------------------------------------------------------------------------
# 4+5. Gate + fail-loud
# ---------------------------------------------------------------------------


class TestDrainerGate:
    def test_no_queue_base_env_skips_drainer(self, monkeypatch, tmp_path):
        """Without YADGAR_QUEUE_BASE the drainer is skipped (no engine init)."""
        monkeypatch.delenv("YADGAR_QUEUE_BASE", raising=False)
        es = _reload_embed_service(monkeypatch, tmp_path)

        engines = MagicMock()
        with (
            patch.object(es, "_get_engine", return_value=MagicMock()),
            patch.object(es, "_ensure_recall_engines", engines),
        ):
            with _lifespan_client(es) as client:
                assert es._queue_drainer is None
                engines.assert_not_called()
                assert client.get("/health").json()["drainer"] is False

        import yadgar.backend.embed_service_metrics as esm

        assert esm.embed_drainer_running._value.get() == 0.0

    def test_unwritable_queue_base_fails_loud(self, monkeypatch, tmp_path, caplog):
        """Queue base that cannot be created → ERROR log, no drainer, gauge 0."""
        # Parent path is a FILE → mkdir(parents=True) raises regardless of uid.
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory")
        monkeypatch.setenv("YADGAR_QUEUE_BASE", str(blocker / "queue-root"))

        es = _reload_embed_service(monkeypatch, tmp_path)

        engines = MagicMock()
        with (
            patch.object(es, "_get_engine", return_value=MagicMock()),
            patch.object(es, "_ensure_recall_engines", engines),
            caplog.at_level(logging.ERROR, logger="yadgar.backend.embed_service"),
        ):
            with _lifespan_client(es) as _:
                assert es._queue_drainer is None
                engines.assert_not_called()

        assert any(
            r.levelno == logging.ERROR and "queue_drainer_start_failed" in r.getMessage()
            for r in caplog.records
        ), f"expected fail-loud ERROR log, got: {[r.getMessage() for r in caplog.records]}"

        import yadgar.backend.embed_service_metrics as esm

        assert esm.embed_drainer_running._value.get() == 0.0
