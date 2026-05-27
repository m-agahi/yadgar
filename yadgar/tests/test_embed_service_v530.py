"""v5.3.0 — /admin/dbsize 60s cache + backend restart-attribution counter.

TDD: these tests MUST fail before implementation.

Coverage:
A. dbsize cache
   A1. Two calls within 1s → os.walk called once (second call served from cache).
   A2. Two calls straddling TTL (monkeypatched clock) → os.walk called twice.
   A3. cache_age_seconds is monotonic within cache window.
   A4. YADGAR_DBSIZE_CACHE_TTL_SEC=0 disables cache (every call recomputes).

B. Restart attribution
   B1. Startup with clean marker → reason="clean" counter incremented; marker removed.
   B2. Startup with no marker, surreal_db exists → reason="crash" counter incremented.
   B3. Startup with no marker, no surreal_db → reason="first_boot" counter incremented.
   B4. Shutdown handler writes marker file.
"""

from __future__ import annotations

import importlib
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reload_es(monkeypatch, *, allow_root: bool = True, ttl: int | None = None):
    """Reload embed_service with clean state; return module."""
    import yadgar.config as cfg

    monkeypatch.setenv("YADGAR_ALLOW_ROOT", "1" if allow_root else "0")
    if ttl is not None:
        monkeypatch.setenv("YADGAR_DBSIZE_CACHE_TTL_SEC", str(ttl))
    else:
        monkeypatch.delenv("YADGAR_DBSIZE_CACHE_TTL_SEC", raising=False)
    cfg.get_settings.cache_clear()

    import yadgar.embed_service as es

    importlib.reload(es)
    return es


def _make_client(es):
    from fastapi.testclient import TestClient

    return TestClient(es.app, raise_server_exceptions=False)


def _fake_walk(root, call_counter: list):
    """Replacement for os.walk that records calls and returns minimal data."""
    call_counter.append(1)
    # Yield nothing (empty DB dir) — enough to test cache hit/miss semantics.
    return iter([])


# ---------------------------------------------------------------------------
# A. dbsize cache
# ---------------------------------------------------------------------------


class TestDbsizeCache:
    def test_two_rapid_calls_walk_once(self, monkeypatch, tmp_path):
        """A1: Two calls within TTL window → os.walk called once."""
        es = _reload_es(monkeypatch, ttl=60)
        call_counter: list = []

        def _patched_walk(root):
            return _fake_walk(root, call_counter)

        # Patch os.walk on the embed_service module (it uses module-level os import)
        with patch.object(es.os, "walk", side_effect=_patched_walk):
            client = _make_client(es)
            r1 = client.get("/admin/dbsize")
            r2 = client.get("/admin/dbsize")

        assert r1.status_code == 200, f"first call failed: {r1.text}"
        assert r2.status_code == 200, f"second call failed: {r2.text}"
        assert len(call_counter) == 1, f"expected 1 os.walk call, got {len(call_counter)}"

    def test_call_after_ttl_recomputes(self, monkeypatch, tmp_path):
        """A2: After TTL expires (monkeypatched time.time), second call recomputes."""
        es = _reload_es(monkeypatch, ttl=60)
        call_counter: list = []

        def _patched_walk(root):
            return _fake_walk(root, call_counter)

        _fake_now = [1_000_000.0]

        def _fake_time():
            return _fake_now[0]

        with (
            patch.object(es.os, "walk", side_effect=_patched_walk),
            patch("yadgar.embed_service.time.time", side_effect=_fake_time),
        ):
            client = _make_client(es)
            r1 = client.get("/admin/dbsize")
            assert r1.status_code == 200

            # Advance clock beyond TTL
            _fake_now[0] += 61.0

            r2 = client.get("/admin/dbsize")
            assert r2.status_code == 200

        assert len(call_counter) == 2, f"expected 2 os.walk calls, got {len(call_counter)}"

    def test_cache_age_seconds_monotonic(self, monkeypatch):
        """A3: cache_age_seconds increases monotonically within cache window."""
        es = _reload_es(monkeypatch, ttl=60)

        _fake_now = [1_000_000.0]

        def _fake_time():
            return _fake_now[0]

        with (
            patch.object(es.os, "walk", return_value=iter([])),
            patch("yadgar.embed_service.time.time", side_effect=_fake_time),
        ):
            client = _make_client(es)
            r1 = client.get("/admin/dbsize")
            assert r1.status_code == 200
            age1 = r1.json()["cache_age_seconds"]
            assert age1 == pytest.approx(0.0, abs=1.0), f"first call age should be ~0, got {age1}"

            # Advance clock by 10s (still within TTL)
            _fake_now[0] += 10.0

            r2 = client.get("/admin/dbsize")
            assert r2.status_code == 200
            age2 = r2.json()["cache_age_seconds"]
            assert age2 > age1, f"age should increase: {age2} > {age1}"
            assert age2 == pytest.approx(10.0, abs=1.0), f"expected ~10s, got {age2}"

    def test_ttl_zero_disables_cache(self, monkeypatch):
        """A4: YADGAR_DBSIZE_CACHE_TTL_SEC=0 → every call recomputes (no cache)."""
        es = _reload_es(monkeypatch, ttl=0)
        call_counter: list = []

        def _patched_walk(root):
            return _fake_walk(root, call_counter)

        with patch.object(es.os, "walk", side_effect=_patched_walk):
            client = _make_client(es)
            client.get("/admin/dbsize")
            client.get("/admin/dbsize")

        assert len(call_counter) == 2, (
            f"expected 2 os.walk calls with TTL=0, got {len(call_counter)}"
        )


# ---------------------------------------------------------------------------
# B. Restart attribution
# ---------------------------------------------------------------------------


class TestRestartAttribution:
    """Test yadgar_embed_restart_reason_total counter + shutdown marker logic."""

    def _make_lifespan_client(self, es):
        """Return TestClient context manager to fire lifespan events."""
        from fastapi.testclient import TestClient

        return TestClient(es.app, raise_server_exceptions=False)

    def test_startup_clean_marker_increments_clean(self, monkeypatch, tmp_path):
        """B1: Startup with clean marker → reason='clean' incremented, marker removed."""
        marker = tmp_path / ".shutdown_clean"
        marker.write_text("1")
        db_path = tmp_path / "surreal_db"
        db_path.mkdir()

        monkeypatch.setenv("YADGAR_SHUTDOWN_MARKER_PATH", str(marker))
        monkeypatch.setenv("YADGAR_DB_PATH", str(db_path))
        monkeypatch.setenv("YADGAR_ALLOW_ROOT", "1")

        import yadgar.config as cfg

        cfg.get_settings.cache_clear()

        import yadgar.embed_service as es

        importlib.reload(es)

        # Capture the counter INSTANCE from the reloaded embed_service_metrics
        import yadgar.embed_service_metrics as esm

        counter = esm.embed_restart_reason_total
        before = counter.labels(reason="clean")._value.get()

        with patch.object(es, "_get_engine", return_value=MagicMock()):
            with self._make_lifespan_client(es) as _:
                pass  # lifespan fires on enter + exit

        after = counter.labels(reason="clean")._value.get()
        assert after == before + 1, f"expected clean counter +1: before={before} after={after}"
        # Note: shutdown writes marker back, so marker.exists() may be True after test.
        # Counter increment proves the marker was seen and acted on at startup.

    def test_startup_no_marker_surreal_db_exists_increments_crash(self, monkeypatch, tmp_path):
        """B2: No marker, surreal_db exists → reason='crash' incremented."""
        marker = tmp_path / ".shutdown_clean"
        # no marker written
        db_path = tmp_path / "surreal_db"
        db_path.mkdir()

        monkeypatch.setenv("YADGAR_SHUTDOWN_MARKER_PATH", str(marker))
        monkeypatch.setenv("YADGAR_DB_PATH", str(db_path))
        monkeypatch.setenv("YADGAR_ALLOW_ROOT", "1")

        import yadgar.config as cfg

        cfg.get_settings.cache_clear()

        import yadgar.embed_service as es

        importlib.reload(es)

        import yadgar.embed_service_metrics as esm

        counter = esm.embed_restart_reason_total
        before = counter.labels(reason="crash")._value.get()

        with patch.object(es, "_get_engine", return_value=MagicMock()):
            with self._make_lifespan_client(es) as _:
                pass

        after = counter.labels(reason="crash")._value.get()
        assert after == before + 1, f"expected crash counter +1: before={before} after={after}"

    def test_startup_no_marker_no_db_increments_first_boot(self, monkeypatch, tmp_path):
        """B3: No marker, no surreal_db → reason='first_boot' incremented."""
        marker = tmp_path / ".shutdown_clean"
        # no marker, no surreal_db
        monkeypatch.setenv("YADGAR_SHUTDOWN_MARKER_PATH", str(marker))
        monkeypatch.setenv("YADGAR_DB_PATH", str(tmp_path / "surreal_db"))  # does not exist
        monkeypatch.setenv("YADGAR_ALLOW_ROOT", "1")

        import yadgar.config as cfg

        cfg.get_settings.cache_clear()

        import yadgar.embed_service as es

        importlib.reload(es)

        import yadgar.embed_service_metrics as esm

        counter = esm.embed_restart_reason_total
        before = counter.labels(reason="first_boot")._value.get()

        with patch.object(es, "_get_engine", return_value=MagicMock()):
            with self._make_lifespan_client(es) as _:
                pass

        after = counter.labels(reason="first_boot")._value.get()
        assert after == before + 1, f"expected first_boot counter +1: before={before} after={after}"

    def test_shutdown_writes_marker(self, monkeypatch, tmp_path):
        """B4: Shutdown event writes clean marker file."""
        marker = tmp_path / ".shutdown_clean"
        db_path = tmp_path / "surreal_db"
        db_path.mkdir()

        monkeypatch.setenv("YADGAR_SHUTDOWN_MARKER_PATH", str(marker))
        monkeypatch.setenv("YADGAR_DB_PATH", str(db_path))
        monkeypatch.setenv("YADGAR_ALLOW_ROOT", "1")

        import yadgar.config as cfg

        cfg.get_settings.cache_clear()

        import yadgar.embed_service as es

        importlib.reload(es)

        with patch.object(es, "_get_engine", return_value=MagicMock()):
            with self._make_lifespan_client(es) as _:
                pass  # exit fires shutdown

        assert marker.exists(), "marker file should exist after clean shutdown"
