"""Tests for yadgar.backup — create_snapshot + prune_snapshots helpers.

TDD: written before implementation, confirmed red before green.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_dummy_db(path: Path) -> Path:
    """Create a minimal placeholder that shutil.copytree can copy."""
    path.mkdir(parents=True, exist_ok=True)
    (path / "data.kvs").write_bytes(b"\x00\x01\x02")
    (path / "index.kvs").write_bytes(b"\x03\x04\x05")
    return path


def _make_snapshot_dir(base: Path, name: str, *, older: bool = False) -> Path:
    """Create a dummy snapshot directory (optionally with old mtime)."""
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "placeholder").write_bytes(b"x")
    if older:
        old_ts = time.time() - 10000
        os.utime(d, (old_ts, old_ts))
    return d


# ---------------------------------------------------------------------------
# create_snapshot
# ---------------------------------------------------------------------------


class TestCreateSnapshot:
    def test_creates_directory_matching_pattern(self, tmp_path: Path) -> None:
        """Snapshot dir must exist and match <db_basename>.<label>-<TS> pattern."""
        from yadgar.backup import create_snapshot

        db = _make_dummy_db(tmp_path / "surreal_db")
        result = create_snapshot(db, snapshot_dir=tmp_path, label="nightly")

        assert result.exists()
        assert result.is_dir()
        assert result.name.startswith("surreal_db.nightly-")

    def test_timestamp_format_yyyy_mm_dd_hhmmss(self, tmp_path: Path) -> None:
        """Timestamp must be YYYY-MM-DD-HHMMSS (dashes, seconds precision)."""
        import re

        from yadgar.backup import create_snapshot

        db = _make_dummy_db(tmp_path / "surreal_db")
        result = create_snapshot(db, snapshot_dir=tmp_path, label="nightly")

        # e.g. surreal_db.nightly-2026-05-26-143025
        assert re.match(
            r"surreal_db\.nightly-\d{4}-\d{2}-\d{2}-\d{6}$",
            result.name,
        ), f"Unexpected name: {result.name}"

    def test_copies_contents_byte_for_byte(self, tmp_path: Path) -> None:
        """Contents must be identical to source."""
        from yadgar.backup import create_snapshot

        db = _make_dummy_db(tmp_path / "surreal_db")
        result = create_snapshot(db, snapshot_dir=tmp_path)

        for src_file in db.rglob("*"):
            if src_file.is_file():
                rel = src_file.relative_to(db)
                dst_file = result / rel
                assert dst_file.exists(), f"Missing {rel} in snapshot"
                assert dst_file.read_bytes() == src_file.read_bytes()

    def test_default_snapshot_dir_is_parent_of_db(self, tmp_path: Path) -> None:
        """When snapshot_dir=None, parent of db_path is used."""
        from yadgar.backup import create_snapshot

        db_parent = tmp_path / "yadgar_home"
        db_parent.mkdir()
        db = _make_dummy_db(db_parent / "surreal_db")
        result = create_snapshot(db)

        assert result.parent == db_parent

    def test_raises_runtime_error_on_missing_source(self, tmp_path: Path) -> None:
        """Missing db_path must raise RuntimeError."""
        from yadgar.backup import create_snapshot

        with pytest.raises(RuntimeError):
            create_snapshot(tmp_path / "nonexistent_db", snapshot_dir=tmp_path)

    def test_label_appears_in_directory_name(self, tmp_path: Path) -> None:
        """Custom label must appear in the snapshot directory name."""
        from yadgar.backup import create_snapshot

        db = _make_dummy_db(tmp_path / "surreal_db")
        result = create_snapshot(db, snapshot_dir=tmp_path, label="pre-cycle")

        assert ".pre-cycle-" in result.name

    def test_timestamp_collision_increments_counter(self, tmp_path: Path) -> None:
        """If target already exists, a counter suffix is appended to avoid collision."""
        from unittest.mock import patch

        from yadgar.backup import create_snapshot

        db = _make_dummy_db(tmp_path / "surreal_db")
        fixed_ts = "2026-05-26-143025"
        first_target = tmp_path / f"surreal_db.nightly-{fixed_ts}"

        with patch("yadgar.backup.datetime") as mock_dt:
            mock_dt.now.return_value.strftime.return_value = fixed_ts
            result1 = create_snapshot(db, snapshot_dir=tmp_path, label="nightly")

        assert result1 == first_target

        # Second call with same ts — must get a different path
        _make_dummy_db(tmp_path / "surreal_db2")

        with patch("yadgar.backup.datetime") as mock_dt:
            mock_dt.now.return_value.strftime.return_value = fixed_ts
            result2 = create_snapshot(db, snapshot_dir=tmp_path, label="nightly")

        assert result2 != result1
        assert result2.exists()


# ---------------------------------------------------------------------------
# prune_snapshots
# ---------------------------------------------------------------------------


class TestPruneSnapshots:
    def test_keeps_newest_n_deletes_rest(self, tmp_path: Path) -> None:
        """With 5 snapshots and retention=3, the 2 oldest are deleted."""
        from yadgar.backup import prune_snapshots

        for i in range(5):
            d = tmp_path / f"surreal_db.nightly-2026-05-26-10000{i}"
            d.mkdir()
            old_ts = time.time() - (5 - i) * 1000  # oldest first
            os.utime(d, (old_ts, old_ts))

        removed = prune_snapshots(tmp_path, "surreal_db.nightly-*", retention=3)

        remaining = sorted(tmp_path.iterdir(), key=lambda p: p.stat().st_mtime)
        assert len(remaining) == 3
        assert len(removed) == 2
        # Removed paths must no longer exist
        for r in removed:
            assert not r.exists()

    def test_returns_removed_paths(self, tmp_path: Path) -> None:
        """Returned list must contain exactly the removed Path objects."""
        from yadgar.backup import prune_snapshots

        dirs = []
        for i in range(4):
            d = tmp_path / f"surreal_db.nightly-snap-{i:04d}"
            d.mkdir()
            old_ts = time.time() - (4 - i) * 500
            os.utime(d, (old_ts, old_ts))
            dirs.append(d)

        removed = prune_snapshots(tmp_path, "surreal_db.nightly-*", retention=2)
        assert len(removed) == 2
        for path in removed:
            assert isinstance(path, Path)

    def test_idempotent_second_call_returns_empty(self, tmp_path: Path) -> None:
        """Second prune with same retention on already-pruned dir returns []."""
        from yadgar.backup import prune_snapshots

        for i in range(3):
            d = tmp_path / f"surreal_db.nightly-snap-{i:04d}"
            d.mkdir()

        prune_snapshots(tmp_path, "surreal_db.nightly-*", retention=3)
        removed2 = prune_snapshots(tmp_path, "surreal_db.nightly-*", retention=3)
        assert removed2 == []

    def test_does_not_touch_non_matching_dirs(self, tmp_path: Path) -> None:
        """Directories not matching the pattern are not deleted."""
        from yadgar.backup import prune_snapshots

        # 5 matching
        for i in range(5):
            (tmp_path / f"surreal_db.nightly-snap-{i:04d}").mkdir()
        # 2 non-matching
        (tmp_path / "surreal_db.pre-vacuum-20260526").mkdir()
        (tmp_path / "unrelated-dir").mkdir()

        prune_snapshots(tmp_path, "surreal_db.nightly-*", retention=2)

        assert (tmp_path / "surreal_db.pre-vacuum-20260526").exists()
        assert (tmp_path / "unrelated-dir").exists()

    def test_retention_zero_deletes_all(self, tmp_path: Path) -> None:
        """retention=0 removes all matching directories."""
        from yadgar.backup import prune_snapshots

        for i in range(3):
            (tmp_path / f"surreal_db.nightly-snap-{i:04d}").mkdir()

        removed = prune_snapshots(tmp_path, "surreal_db.nightly-*", retention=0)
        assert len(removed) == 3
        assert not any(r.exists() for r in removed)

    def test_retention_ge_count_deletes_none(self, tmp_path: Path) -> None:
        """When retention >= count, nothing is deleted."""
        from yadgar.backup import prune_snapshots

        for i in range(3):
            (tmp_path / f"surreal_db.nightly-snap-{i:04d}").mkdir()

        removed = prune_snapshots(tmp_path, "surreal_db.nightly-*", retention=10)
        assert removed == []
        assert len(list(tmp_path.iterdir())) == 3

    def test_missing_snapshot_dir_returns_empty(self, tmp_path: Path) -> None:
        """Missing snapshot_dir returns [] rather than raising."""
        from yadgar.backup import prune_snapshots

        removed = prune_snapshots(tmp_path / "nonexistent", "surreal_db.nightly-*", retention=3)
        assert removed == []


# ---------------------------------------------------------------------------
# Env knob
# ---------------------------------------------------------------------------


class TestEnvKnob:
    def test_backup_retention_default_is_3(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """YADGAR_BACKUP_RETENTION defaults to 3 when not set."""
        monkeypatch.delenv("YADGAR_BACKUP_RETENTION", raising=False)
        from yadgar.backup import default_retention

        assert default_retention() == 3

    def test_backup_retention_reads_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """YADGAR_BACKUP_RETENTION is read from environment."""
        monkeypatch.setenv("YADGAR_BACKUP_RETENTION", "7")
        # Need fresh read — function reads os.getenv live
        import importlib

        import yadgar.backup as bkp

        importlib.reload(bkp)

        assert bkp.default_retention() == 7

    def test_config_registry_contains_knob(self) -> None:
        """YADGAR_BACKUP_RETENTION is registered in config_registry."""
        from yadgar.config_registry import list_config

        names = [e.name for e in list_config()]
        assert "YADGAR_BACKUP_RETENTION" in names
