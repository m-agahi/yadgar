"""V5: db_size_mb metric — server-mode uses storage.get_db_size() proxy.

Tests:
- In server mode (_db_url set), sample_system_metrics uses storage.get_db_size()
  and converts db_size_bytes → db_size_mb correctly.
- In embedded mode (_db_url=None), falls through to local filesystem walk.
- storage=None still works (falls through to fs walk).
"""

from __future__ import annotations

from unittest.mock import MagicMock


def _make_storage(db_url: str | None, db_size_bytes: int) -> object:
    """Build a minimal fake StorageEngine."""
    s = MagicMock()
    s._db_url = db_url
    s.get_db_size.return_value = {"db_size_bytes": db_size_bytes}
    return s


class TestSampleSystemMetricsDbSize:
    def test_server_mode_uses_get_db_size(self, tmp_path) -> None:
        """In server mode, db_size_mb comes from storage.get_db_size(), not local walk."""
        storage = _make_storage(db_url="http://db:8000", db_size_bytes=20 * 1024 * 1024)

        from yadgar.core.graph_api import sample_system_metrics

        result = sample_system_metrics(pid=1, db_path=str(tmp_path), storage=storage)

        storage.get_db_size.assert_called_once()
        assert result["db_size_mb"] == 20.0

    def test_server_mode_converts_bytes_to_mb(self, tmp_path) -> None:
        """db_size_bytes is divided by 1024² and rounded to 1 decimal."""
        storage = _make_storage(db_url="http://db:8000", db_size_bytes=1_572_864)  # 1.5 MB

        from yadgar.core.graph_api import sample_system_metrics

        result = sample_system_metrics(pid=1, db_path=str(tmp_path), storage=storage)
        assert result["db_size_mb"] == 1.5

    def test_embedded_mode_uses_fs_walk(self, tmp_path) -> None:
        """In embedded mode (_db_url=None), local filesystem walk is used."""
        # Create a small file in the tmp_path so size > 0
        (tmp_path / "data.bin").write_bytes(b"x" * 1024 * 100)  # 100 KB

        storage = _make_storage(db_url=None, db_size_bytes=0)

        from yadgar.core.graph_api import sample_system_metrics

        result = sample_system_metrics(pid=1, db_path=str(tmp_path), storage=storage)

        storage.get_db_size.assert_not_called()
        assert result["db_size_mb"] > 0

    def test_no_storage_falls_through_to_fs(self, tmp_path) -> None:
        """storage=None still returns a valid db_size_mb from local walk."""
        (tmp_path / "db.bin").write_bytes(b"y" * 1024 * 50)  # 50 KB

        from yadgar.core.graph_api import sample_system_metrics

        result = sample_system_metrics(pid=1, db_path=str(tmp_path), storage=None)
        assert "db_size_mb" in result

    def test_server_mode_get_db_size_failure_falls_through(self, tmp_path) -> None:
        """If get_db_size raises, fall through to local fs walk (no crash)."""
        storage = MagicMock()
        storage._db_url = "http://db:8000"
        storage.get_db_size.side_effect = RuntimeError("network failure")

        (tmp_path / "fallback.bin").write_bytes(b"z" * 512)

        from yadgar.core.graph_api import sample_system_metrics

        result = sample_system_metrics(pid=1, db_path=str(tmp_path), storage=storage)
        assert "db_size_mb" in result
        # Should have fallen through — result comes from local walk
        assert result["db_size_mb"] >= 0
