"""v5.67.0 — regression tests for nightly-cycle service failures.

TDD: RED written before fixes; each test must FAIL before the corresponding fix.

Bug 1 — backup-path drift:
  nightly_cycle.main() derives db_path from Settings.DB_PATH which reads the
  stale config.yaml value (~/.yadgar/surreal_db). Fix: use yadgar.paths.DB_PATH
  (respects YADGAR_DATA_DIR / XDG default ~/.local/share/yadgar) when no
  explicit args.db_path is provided.

Bug 2 — GC-shutdown AttributeError:
  yadgar.graph_api._gc_callback accesses time.perf_counter() and _gc_start_times
  module globals which become None at interpreter shutdown. Fix: guard against
  None at top of callback.

Bug 3 — reembed_all skips None-content rows:
  Memories with None content cause encode_batch to fail for the entire batch,
  returning all-None embeddings. Fix: skip None-content rows before encoding.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Bug 1 — backup-path drift
# ---------------------------------------------------------------------------


class TestNightlyCycleDbPathDerivation:
    """db_path must come from yadgar.paths.DB_PATH, not Settings.DB_PATH."""

    def test_db_path_from_paths_not_settings_when_no_args_db_path(
        self, tmp_path: Path, monkeypatch
    ):
        """When args.db_path is None, main() must use yadgar.paths.DB_PATH.

        The stale config.yaml sets db_path: ~/.yadgar/surreal_db.
        yadgar.paths.DB_PATH resolves to YADGAR_DATA_DIR/surreal_db (or XDG
        default ~/.local/share/yadgar/surreal_db).
        """
        import yadgar.scripts.nightly_cycle as nc

        importlib.reload(nc)

        # Simulate YADGAR_DATA_DIR pointing to tmp_path (real data dir).
        real_data_dir = tmp_path / "real_data"
        real_data_dir.mkdir()
        real_db = real_data_dir / "surreal_db"
        real_db.mkdir()

        monkeypatch.setenv("YADGAR_DATA_DIR", str(real_data_dir))

        # Settings.DB_PATH returns the STALE legacy path — simulates config.yaml bug.
        stale_db_path = str(tmp_path / "stale" / "surreal_db")
        mock_settings = SimpleNamespace(DB_PATH=stale_db_path)

        captured_db_paths = []

        def _snap_capture(db_path, snapshot_dir=None, label="nightly", backend_url=None):
            captured_db_paths.append(str(db_path))
            return tmp_path / "snap"

        args = SimpleNamespace(
            db_path=None,  # no override — must use paths.DB_PATH
            backend_url="http://127.0.0.1:8080",
            service_mode=None,
            retention=3,
        )

        mock_sched = MagicMock()
        mock_sched.force_consolidate.return_value = {"merged": 0}

        with patch.multiple(
            "yadgar.scripts.nightly_cycle",
            _run_systemctl=MagicMock(),
            create_snapshot=_snap_capture,
            prune_snapshots=MagicMock(return_value=[]),
            cmd_vacuum_impl=MagicMock(return_value=0),
            StorageEngine=MagicMock(return_value=MagicMock()),
            ConsolidationScheduler=MagicMock(return_value=mock_sched),
            EmbeddingEngine=MagicMock(return_value=MagicMock()),
            Settings=MagicMock(return_value=mock_settings),
            configure_logging=MagicMock(),
            default_retention=MagicMock(return_value=3),
        ):
            nc.main(args)

        assert captured_db_paths, "create_snapshot must have been called"
        used_path = captured_db_paths[0]
        # Must NOT be the stale legacy path from config.yaml
        assert "stale" not in used_path, (
            f"nightly_cycle used the stale Settings.DB_PATH ({stale_db_path}) "
            f"instead of yadgar.paths.DB_PATH. Got: {used_path}"
        )
        # Must resolve via YADGAR_DATA_DIR
        assert str(real_data_dir) in used_path, (
            f"Expected path under YADGAR_DATA_DIR ({real_data_dir}), got: {used_path}"
        )

    def test_db_path_xdg_default_when_no_data_dir_override(self, tmp_path: Path, monkeypatch):
        """When YADGAR_DATA_DIR is unset, db_path must use XDG default
        (~/.local/share/yadgar/surreal_db), NOT ~/.yadgar/surreal_db."""
        import yadgar.scripts.nightly_cycle as nc

        importlib.reload(nc)

        monkeypatch.delenv("YADGAR_DATA_DIR", raising=False)
        monkeypatch.delenv("YADGAR_DB_PATH", raising=False)
        # Set XDG_DATA_HOME to a known temp path so we can assert on it
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg_data"))

        expected_db = tmp_path / "xdg_data" / "yadgar" / "surreal_db"
        expected_db.mkdir(parents=True, exist_ok=True)

        # Settings.DB_PATH returns the STALE legacy path
        stale_db_path = str(Path.home() / ".yadgar" / "surreal_db")
        mock_settings = SimpleNamespace(DB_PATH=stale_db_path)

        captured_db_paths = []

        def _snap_capture(db_path, snapshot_dir=None, label="nightly", backend_url=None):
            captured_db_paths.append(str(db_path))
            return tmp_path / "snap"

        args = SimpleNamespace(
            db_path=None,
            backend_url="http://127.0.0.1:8080",
            service_mode=None,
            retention=3,
        )

        mock_sched = MagicMock()
        mock_sched.force_consolidate.return_value = {"merged": 0}

        with patch.multiple(
            "yadgar.scripts.nightly_cycle",
            _run_systemctl=MagicMock(),
            create_snapshot=_snap_capture,
            prune_snapshots=MagicMock(return_value=[]),
            cmd_vacuum_impl=MagicMock(return_value=0),
            StorageEngine=MagicMock(return_value=MagicMock()),
            ConsolidationScheduler=MagicMock(return_value=mock_sched),
            EmbeddingEngine=MagicMock(return_value=MagicMock()),
            Settings=MagicMock(return_value=mock_settings),
            configure_logging=MagicMock(),
            default_retention=MagicMock(return_value=3),
        ):
            nc.main(args)

        assert captured_db_paths, "create_snapshot must have been called"
        used_path = captured_db_paths[0]
        # Must NOT be the stale ~/.yadgar path
        assert ".yadgar" not in used_path, (
            f"nightly_cycle used the legacy ~/.yadgar path. Got: {used_path}"
        )
        # Must use XDG default
        assert "xdg_data" in used_path, f"Expected XDG-derived path, got: {used_path}"

    def test_explicit_args_db_path_still_respected(self, tmp_path: Path):
        """When args.db_path is explicitly provided, it must take precedence."""
        import yadgar.scripts.nightly_cycle as nc

        explicit_db = tmp_path / "explicit_db"
        explicit_db.mkdir()

        captured_db_paths = []

        def _snap_capture(db_path, snapshot_dir=None, label="nightly", backend_url=None):
            captured_db_paths.append(str(db_path))
            return tmp_path / "snap"

        args = SimpleNamespace(
            db_path=str(explicit_db),  # explicit override
            backend_url="http://127.0.0.1:8080",
            service_mode=None,
            retention=3,
        )

        mock_sched = MagicMock()
        mock_sched.force_consolidate.return_value = {"merged": 0}

        with patch.multiple(
            "yadgar.scripts.nightly_cycle",
            _run_systemctl=MagicMock(),
            create_snapshot=_snap_capture,
            prune_snapshots=MagicMock(return_value=[]),
            cmd_vacuum_impl=MagicMock(return_value=0),
            StorageEngine=MagicMock(return_value=MagicMock()),
            ConsolidationScheduler=MagicMock(return_value=mock_sched),
            EmbeddingEngine=MagicMock(return_value=MagicMock()),
            Settings=MagicMock(return_value=SimpleNamespace(DB_PATH="/stale/path")),
            configure_logging=MagicMock(),
            default_retention=MagicMock(return_value=3),
        ):
            nc.main(args)

        assert captured_db_paths, "create_snapshot must have been called"
        assert str(explicit_db) in captured_db_paths[0], (
            f"Expected explicit path {explicit_db}, got {captured_db_paths[0]}"
        )


# ---------------------------------------------------------------------------
# Bug 2 — GC-shutdown AttributeError guard
# ---------------------------------------------------------------------------


class TestGcCallbackShutdownSafe:
    """_gc_callback must not raise when module globals are None (interpreter shutdown)."""

    def test_gc_callback_safe_when_time_is_none(self):
        """Simulate interpreter shutdown: time module torn down to None.

        _gc_callback(phase='start', info={'generation': 0}) must not raise.
        """
        import yadgar.graph_api as ga

        importlib.reload(ga)

        saved_time = ga.time
        try:
            ga.time = None  # type: ignore[attr-defined]
            # Must not raise — shutdown guard must return early
            ga._gc_callback("start", {"generation": 0})
        except Exception as exc:
            pytest.fail(
                f"_gc_callback raised {type(exc).__name__}: {exc} "
                f"when time module is None (shutdown simulation)"
            )
        finally:
            ga.time = saved_time

    def test_gc_callback_safe_when_gc_start_times_is_none(self):
        """Simulate interpreter shutdown: _gc_start_times torn down to None.

        _gc_callback(phase='stop', info={'generation': 0}) must not raise.
        """
        import yadgar.graph_api as ga

        importlib.reload(ga)

        saved_times = ga._gc_start_times
        try:
            ga._gc_start_times = None  # type: ignore[attr-defined]
            # Must not raise
            ga._gc_callback("stop", {"generation": 0})
        except Exception as exc:
            pytest.fail(
                f"_gc_callback raised {type(exc).__name__}: {exc} "
                f"when _gc_start_times is None (shutdown simulation)"
            )
        finally:
            ga._gc_start_times = saved_times

    def test_gc_callback_start_still_records_when_healthy(self):
        """In normal operation, 'start' phase still records timestamp."""
        import yadgar.graph_api as ga

        importlib.reload(ga)

        original_times = ga._gc_start_times
        ga._gc_start_times = {}

        try:
            ga._gc_callback("start", {"generation": 0})
            assert 0 in ga._gc_start_times, "Start phase must record timestamp"
        finally:
            ga._gc_start_times = original_times

    def test_gc_callback_stop_does_not_raise_when_no_start(self):
        """'stop' with no matching 'start' must be a no-op (not raise KeyError)."""
        import yadgar.graph_api as ga

        importlib.reload(ga)

        original_times = ga._gc_start_times
        ga._gc_start_times = {}  # no start recorded

        try:
            # Must not raise — pop(..., None) guards against missing key
            ga._gc_callback("stop", {"generation": 0})
        except Exception as exc:
            pytest.fail(f"_gc_callback stop without start raised: {exc}")
        finally:
            ga._gc_start_times = original_times


# ---------------------------------------------------------------------------
# Bug 3 — reembed_all skips None-content rows
# ---------------------------------------------------------------------------


class TestReembedAllSkipsNoneContent:
    """reembed_all must skip rows where content is None/empty before encoding."""

    def test_reembed_all_skips_none_content_rows(self):
        """Rows with None content are filtered out; only valid rows get embedded."""
        from yadgar.server.tools.admin_other import reembed_all

        rows = [
            {"id": 1, "content": "valid memory one"},
            {"id": 2, "content": None},
            {"id": 3, "content": "valid memory two"},
        ]

        mock_storage = MagicMock()
        mock_storage.get_memories_without_embeddings.return_value = rows

        encoded_texts = []

        def _encode_batch(texts):
            encoded_texts.extend(texts)
            # Return bytes for each text
            return [b"\x00" * 4 for _ in texts]

        mock_embeddings = MagicMock()
        mock_embeddings.encode_batch.side_effect = _encode_batch
        mock_embeddings.model_name = "all-MiniLM-L6-v2"

        with patch("yadgar.server.tools.admin_other._get_storage", return_value=mock_storage):
            with patch(
                "yadgar.server.tools.admin_other._get_embeddings", return_value=mock_embeddings
            ):
                result = reembed_all()

        # None-content row (id=2) must be excluded from encode call
        assert None not in encoded_texts, (
            f"None content was passed to encode_batch: {encoded_texts}"
        )
        # Only 2 valid rows should be encoded
        assert len(encoded_texts) == 2, (
            f"Expected 2 texts encoded (skipping None), got {len(encoded_texts)}"
        )
        # reembedded count must match successfully encoded valid rows
        assert result["reembedded"] == 2, f"Expected reembedded=2, got {result}"
        assert result["total_missing"] == 3, (
            f"Expected total_missing=3 (all rows incl None-content), got {result}"
        )

    def test_reembed_all_all_none_content_returns_zero(self):
        """If ALL rows have None content, reembedded=0 but no crash."""
        from yadgar.server.tools.admin_other import reembed_all

        rows = [
            {"id": 1, "content": None},
            {"id": 2, "content": None},
        ]

        mock_storage = MagicMock()
        mock_storage.get_memories_without_embeddings.return_value = rows
        mock_embeddings = MagicMock()
        mock_embeddings.model_name = "all-MiniLM-L6-v2"

        with patch("yadgar.server.tools.admin_other._get_storage", return_value=mock_storage):
            with patch(
                "yadgar.server.tools.admin_other._get_embeddings", return_value=mock_embeddings
            ):
                result = reembed_all()

        assert result["reembedded"] == 0
        # encode_batch must NOT be called at all (no valid texts)
        mock_embeddings.encode_batch.assert_not_called()

    def test_reembed_all_valid_content_still_embedded(self):
        """Rows with valid content must still be embedded (regression guard)."""
        from yadgar.server.tools.admin_other import reembed_all

        rows = [{"id": 1, "content": "hello world"}]

        mock_storage = MagicMock()
        mock_storage.get_memories_without_embeddings.return_value = rows

        mock_embeddings = MagicMock()
        mock_embeddings.encode_batch.return_value = [b"\x01" * 4]
        mock_embeddings.model_name = "all-MiniLM-L6-v2"

        with patch("yadgar.server.tools.admin_other._get_storage", return_value=mock_storage):
            with patch(
                "yadgar.server.tools.admin_other._get_embeddings", return_value=mock_embeddings
            ):
                result = reembed_all()

        assert result["reembedded"] == 1
        mock_storage.update_memory_embedding.assert_called_once_with(
            1, b"\x01" * 4, "all-MiniLM-L6-v2"
        )
