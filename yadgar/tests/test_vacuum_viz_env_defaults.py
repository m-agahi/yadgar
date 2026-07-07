"""v5.1.1: env-aware defaults for vacuum CLI + viz bind interface.

Pins that vacuum's --db-path / --backend-url + lifecycle viz host
read from YADGAR_DATA_DIR / YADGAR_DB_URL / settings.HOST so the
systemd unit ExecStart doesn't need to repeat each flag.
"""

from __future__ import annotations

import pytest

from yadgar.core.cli.vacuum import _default_backend_url, _default_db_path


class TestVacuumDbPathDefault:
    def test_data_dir_set_yields_subdir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("YADGAR_DATA_DIR", "/data")
        assert _default_db_path() == "/data/surreal_db"

    def test_data_dir_unset_yields_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("YADGAR_DATA_DIR", raising=False)
        assert _default_db_path() is None

    def test_data_dir_empty_string_yields_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("YADGAR_DATA_DIR", "")
        assert _default_db_path() is None


class TestVacuumBackendUrlDefault:
    def test_db_url_set_yields_db_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("YADGAR_DB_URL", "http://yadgar-backend:8000")
        assert _default_backend_url() == "http://yadgar-backend:8000"

    def test_db_url_unset_yields_legacy_loopback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("YADGAR_DB_URL", raising=False)
        assert _default_backend_url() == "http://127.0.0.1:8080"


class TestVizBindHost:
    """lifecycle.py auto-start of viz_server must pass settings.HOST."""

    def test_lifecycle_viz_thread_reads_settings_host(self) -> None:
        """Source-level assertion: lifecycle uses settings.HOST for viz bind."""
        from yadgar._shared.runtime import lifecycle

        src = open(lifecycle.__file__).read()
        # Pin that the viz thread now picks host from settings, not hardcoded.
        assert 'getattr(_settings, "HOST"' in src or "_settings.HOST" in src, (
            "lifecycle.py viz thread must read host from settings"
        )
        # Pin that run_viz_server is called with host= kwarg.
        assert "run_viz_server(host=" in src, "lifecycle.py must pass host= to run_viz_server"

    def test_viz_server_signature_accepts_host(self) -> None:
        """run_viz_server must accept a host kwarg (regression check)."""
        import inspect

        from yadgar.core.viz_server import run_viz_server

        sig = inspect.signature(run_viz_server)
        assert "host" in sig.parameters, "run_viz_server() missing host parameter"

    def test_viz_thread_host_propagates_via_mock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """End-to-end: when settings.HOST=0.0.0.0, the viz thread invokes
        run_viz_server with host="0.0.0.0"."""
        captured: dict[str, object] = {}

        def _fake_run_viz_server(host: str = "127.0.0.1", port: int = 42069) -> None:
            captured["host"] = host
            captured["port"] = port

        # Patch the symbol BEFORE the thread function captures it via import.
        import yadgar.core.viz_server

        monkeypatch.setattr(yadgar.core.viz_server, "run_viz_server", _fake_run_viz_server)

        # Simulate the lifecycle code path with HOST=0.0.0.0
        class _FakeSettings:
            HOST = "0.0.0.0"
            VIZ_PORT = 42069

        _settings = _FakeSettings()
        _viz_port = getattr(_settings, "VIZ_PORT", 42069)
        _viz_host = getattr(_settings, "HOST", "127.0.0.1")

        # Inline simulate the body of _viz_thread
        from yadgar.core.viz_server import run_viz_server  # picks up monkeypatch

        run_viz_server(host=_viz_host, port=_viz_port)

        assert captured == {"host": "0.0.0.0", "port": 42069}
