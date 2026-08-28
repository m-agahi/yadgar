"""v5.3.9 N1 — backend HTTP timeout tests.

Verify that YADGAR_BACKEND_HTTP_TIMEOUT_SEC and YADGAR_BACKEND_IMPORT_TIMEOUT_SEC
bound all outbound httpx calls within their configured windows.

Pattern:
- Bind a local TCP server that sleeps before responding.
- Call the wrapped function.
- Assert it completes within ~(timeout + 2s) slack (function degrades gracefully).
- Verify env override: lower the env var, assert timeout fires sooner.
"""

from __future__ import annotations

import os
import socket
import threading
import time

import pytest

# ---------------------------------------------------------------------------
# Helpers — slow TCP server
# ---------------------------------------------------------------------------


class _SlowServer:
    """Bind a TCP socket that accepts one connection then sleeps `delay_s` before responding.

    Used to simulate an unreachable / hung backend.
    """

    def __init__(
        self,
        delay_s: float = 30.0,
        response: bytes = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok",
    ):
        self.delay_s = delay_s
        self.response = response
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(5)
        self.port: int = self._sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            conn, _ = self._sock.accept()
            # Drain the request
            conn.setblocking(False)
            try:
                conn.recv(4096)
            except OSError:
                # Non-blocking recv with nothing buffered raises BlockingIOError.
                pass
            conn.setblocking(True)
            time.sleep(self.delay_s)
            try:
                conn.sendall(self.response)
            except OSError:
                # The client already gave up: BrokenPipeError / ConnectionReset.
                pass
            conn.close()
        except OSError:
            # accept() on the closed listening socket during teardown.
            pass

    def close(self) -> None:
        self._sock.close()


# ---------------------------------------------------------------------------
# Tests: RemoteMLClient
# ---------------------------------------------------------------------------


class TestRemoteMLClientTimeout:
    """RemoteMLClient must respect RERANK_BACKEND_TIMEOUT_SEC for /rerank calls.

    v5.6.6: /rerank calls use RERANK_BACKEND_TIMEOUT_SEC (dedicated, default 90s).
    BACKEND_HTTP_TIMEOUT_SEC still governs /embed, /health, /admin/dbsize.
    """

    def test_score_cross_encoder_times_out(self, monkeypatch):
        """score_cross_encoder returns None quickly when backend is slow (N4: circuit breaker)."""
        import yadgar._shared.config as cfg

        # v5.6.6: /rerank uses RERANK_BACKEND_TIMEOUT_SEC, not BACKEND_HTTP_TIMEOUT_SEC
        monkeypatch.setenv("YADGAR_BACKEND_HTTP_TIMEOUT_SEC", "2")
        monkeypatch.setenv("YADGAR_RERANK_BACKEND_TIMEOUT_SEC", "2")
        cfg.get_settings.cache_clear()

        server = _SlowServer(delay_s=30.0)
        try:
            import yadgar.backend.ml_client.ml_client as ml

            client = ml.RemoteMLClient(base_url=server.url)
            start = time.monotonic()
            result = client.score_cross_encoder("q", ["text"])
            elapsed = time.monotonic() - start
            # Function catches exceptions and returns None — confirm it returned fast
            assert elapsed < 6.0, f"score_cross_encoder hung {elapsed:.1f}s (expected <6s)"
            assert result is None
        finally:
            server.close()
            cfg.get_settings.cache_clear()

    def test_score_nli_times_out(self, monkeypatch):
        """score_nli returns None quickly when backend is slow (N4: circuit breaker)."""
        import yadgar._shared.config as cfg

        # v5.6.6: /rerank uses RERANK_BACKEND_TIMEOUT_SEC, not BACKEND_HTTP_TIMEOUT_SEC
        monkeypatch.setenv("YADGAR_BACKEND_HTTP_TIMEOUT_SEC", "2")
        monkeypatch.setenv("YADGAR_RERANK_BACKEND_TIMEOUT_SEC", "2")
        cfg.get_settings.cache_clear()

        server = _SlowServer(delay_s=30.0)
        try:
            import yadgar.backend.ml_client.ml_client as ml

            client = ml.RemoteMLClient(base_url=server.url)
            start = time.monotonic()
            result = client.score_nli("q", ["text"])
            elapsed = time.monotonic() - start
            assert elapsed < 6.0, f"score_nli hung {elapsed:.1f}s (expected <6s)"
            assert result is None
        finally:
            server.close()
            cfg.get_settings.cache_clear()

    def test_env_override_lowers_timeout(self, monkeypatch):
        """Setting YADGAR_RERANK_BACKEND_TIMEOUT_SEC=1 fires sooner for /rerank."""
        import yadgar._shared.config as cfg

        monkeypatch.setenv("YADGAR_BACKEND_HTTP_TIMEOUT_SEC", "1")
        monkeypatch.setenv("YADGAR_RERANK_BACKEND_TIMEOUT_SEC", "1")
        cfg.get_settings.cache_clear()

        server = _SlowServer(delay_s=30.0)
        try:
            import yadgar.backend.ml_client.ml_client as ml

            client = ml.RemoteMLClient(base_url=server.url)
            start = time.monotonic()
            client.score_cross_encoder("q", ["text"])
            elapsed = time.monotonic() - start
            assert elapsed < 4.0, (
                f"timeout did not fire within 4s with 1s setting (got {elapsed:.1f}s)"
            )
        finally:
            server.close()
            cfg.get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Tests: storage/_http client timeout
# ---------------------------------------------------------------------------


class TestStorageHttpTimeout:
    """StorageEngine._http must use BACKEND_HTTP_TIMEOUT_SEC."""

    def test_storage_http_client_uses_config_timeout(self, monkeypatch):
        """StorageEngine._http.timeout matches BACKEND_HTTP_TIMEOUT_SEC."""
        import unittest.mock as mock

        import yadgar._shared.config as cfg

        monkeypatch.setenv("YADGAR_BACKEND_HTTP_TIMEOUT_SEC", "7")
        monkeypatch.setenv("YADGAR_DB_URL", "http://127.0.0.1:9999")
        monkeypatch.setenv("YADGAR_ALLOW_ROOT", "1")
        cfg.get_settings.cache_clear()

        try:
            from yadgar._shared.storage import StorageEngine

            with mock.patch.object(StorageEngine, "_init_schema", return_value=None):
                engine = StorageEngine(db_path="/tmp/test_yadgar_db")
                timeout = engine._http.timeout
                # httpx.Timeout with connect=2 and read=7 → timeout.read == 7
                assert timeout.read == pytest.approx(7.0), (
                    f"Expected read timeout 7.0, got {timeout.read}"
                )
        finally:
            cfg.get_settings.cache_clear()
            monkeypatch.delenv("YADGAR_DB_URL", raising=False)


# ---------------------------------------------------------------------------
# Tests: dbsize timeout
# ---------------------------------------------------------------------------


class TestDbSizeTimeout:
    """get_db_size must not hang when backend is slow."""

    def test_dbsize_times_out_quickly(self, monkeypatch):
        """get_db_size returns zero dict quickly when backend slow."""
        import yadgar._shared.config as cfg

        monkeypatch.setenv("YADGAR_BACKEND_HTTP_TIMEOUT_SEC", "2")
        cfg.get_settings.cache_clear()

        server = _SlowServer(delay_s=30.0)
        try:
            from yadgar._shared.storage.dbsize import _DbSizeMixin

            mixin = _DbSizeMixin.__new__(_DbSizeMixin)
            mixin._db_url = server.url
            monkeypatch.setenv("YADGAR_BACKEND_EMBED_URL", server.url)

            start = time.monotonic()
            result = mixin.get_db_size()
            elapsed = time.monotonic() - start
            assert elapsed < 6.0, f"get_db_size hung {elapsed:.1f}s (expected <6s)"
            assert result["db_size_bytes"] == 0
        finally:
            server.close()
            cfg.get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Tests: vacuum preflight health check
# ---------------------------------------------------------------------------


class TestVacuumPreflightTimeout:
    """vacuum cmd_vacuum_impl preflight health check must respect timeout."""

    def test_preflight_health_times_out(self, monkeypatch):
        """cmd_vacuum_impl returns 1 quickly when backend unreachable."""
        import types

        import yadgar._shared.config as cfg
        import yadgar.core.vacuum as vac

        monkeypatch.setenv("YADGAR_BACKEND_HTTP_TIMEOUT_SEC", "2")
        cfg.get_settings.cache_clear()

        server = _SlowServer(delay_s=30.0)
        fake_db = "/tmp/yadgar_fake_db_preflight"
        os.makedirs(fake_db, exist_ok=True)
        try:
            args = types.SimpleNamespace(
                backend_url=server.url,
                service_mode="manual",
                db_path=fake_db,
                yes=True,
            )
            start = time.monotonic()
            rc = vac.cmd_vacuum_impl(args)
            elapsed = time.monotonic() - start
            assert rc != 0, "should fail when backend unreachable"
            assert elapsed < 10.0, f"preflight did not timeout quickly (took {elapsed:.1f}s)"
        finally:
            server.close()
            cfg.get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Tests: migration vs operational HTTP timeout separation (N1-fixup)
# ---------------------------------------------------------------------------


class TestMigrationTimeout:
    """Migration HTTP timeout must be MIGRATION_HTTP_TIMEOUT_SEC (30), not BACKEND_HTTP_TIMEOUT_SEC (5)."""

    def test_migration_uses_separate_longer_timeout(self, monkeypatch):
        """During _init_schema, _http uses migration timeout (30s); after init, uses operational (5s)."""
        import unittest.mock as mock

        import yadgar._shared.config as cfg

        monkeypatch.setenv("YADGAR_DB_URL", "http://127.0.0.1:9999")
        monkeypatch.setenv("YADGAR_ALLOW_ROOT", "1")
        cfg.get_settings.cache_clear()

        captured = {}

        def spy_init_schema(self):
            captured["mid_init_read"] = self._http.timeout.read

        try:
            from yadgar._shared.storage import StorageEngine

            with mock.patch.object(StorageEngine, "_init_schema", spy_init_schema):
                engine = StorageEngine(db_path="/tmp/test_yadgar_db_mig")

            assert captured.get("mid_init_read") == pytest.approx(30.0), (
                f"migration timeout should be 30.0, got {captured.get('mid_init_read')}"
            )
            assert engine._http.timeout.read == pytest.approx(5.0), (
                f"operational timeout should be 5.0 post-init, got {engine._http.timeout.read}"
            )
        finally:
            cfg.get_settings.cache_clear()
            monkeypatch.delenv("YADGAR_DB_URL", raising=False)
