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


# ---------------------------------------------------------------------------
# Round-trip integrity: non-trivial nested structure
# ---------------------------------------------------------------------------


class TestRoundTripIntegrity:
    """PR-7: verify create_snapshot copies byte-identical nested structures.

    PR-6 tests confirmed snapshot creation and basic content copying.
    These tests close the integrity gap: nested subdirs, empty dirs, empty
    files, a large binary blob (~200 KB), and UTF-8 text must all survive the
    copytree round-trip intact.

    Symlink behavior: shutil.copytree(symlinks=False) (the default) dereferences
    symlinks into regular files.  We include a symlink in the source and assert
    only that the resolved content is present in the snapshot — not that link-ness
    is preserved, since that is stdlib-documented behaviour, not a bug.

    We do NOT assert mtime or directory permissions because copytree creates
    snapshot directories at the current time (directory mtimes will diverge).
    """

    @staticmethod
    def _build_complex_source(base: Path) -> Path:
        """Create a non-trivial nested source directory tree."""
        src = base / "complex_db"
        src.mkdir()

        # Nested subdirs
        (src / "level1" / "level2").mkdir(parents=True)
        (src / "level1" / "sibling").mkdir(parents=True)

        # Empty directory
        (src / "empty_dir").mkdir()

        # Small text file
        (src / "readme.txt").write_text("hello yadgar\nline two\n", encoding="utf-8")

        # UTF-8 file with non-ASCII characters
        (src / "unicode.txt").write_text(
            "Привет мир — こんにちは — 你好世界 — 🎉\n", encoding="utf-8"
        )

        # Empty file
        (src / "level1" / "empty_file.bin").write_bytes(b"")

        # Small binary file in a subdir
        (src / "level1" / "level2" / "data.kvs").write_bytes(bytes(range(256)) * 4)

        # Large binary blob (~200 KB) — confirms copytree doesn't mangle raw bytes
        large_blob = bytes((i % 251) for i in range(204_800))
        (src / "level1" / "large_blob.bin").write_bytes(large_blob)

        # Sibling subdir file
        (src / "level1" / "sibling" / "notes.txt").write_text(
            "surreal data\n" * 50, encoding="utf-8"
        )

        # Symlink — dereferenced by copytree; assert content not link-ness
        link_target = src / "readme.txt"
        symlink = src / "readme_link.txt"
        symlink.symlink_to(link_target)

        return src

    def test_snapshot_contains_identical_relative_paths(self, tmp_path: Path) -> None:
        """Every relative path in source (excluding symlinks) must exist in snapshot."""

        from yadgar.backup import create_snapshot

        src = self._build_complex_source(tmp_path)
        snap = create_snapshot(src, snapshot_dir=tmp_path, label="rt-test")

        src_paths = {p.relative_to(src) for p in src.rglob("*") if not p.is_symlink()}
        snap_paths = {p.relative_to(snap) for p in snap.rglob("*")}

        # All non-symlink source paths must be present in snapshot
        missing = src_paths - snap_paths
        assert not missing, f"Paths missing from snapshot: {sorted(missing)}"

    def test_snapshot_file_contents_byte_identical(self, tmp_path: Path) -> None:
        """Every non-symlink file in source must be byte-identical in snapshot."""
        import filecmp
        import hashlib

        from yadgar.backup import create_snapshot

        src = self._build_complex_source(tmp_path)
        snap = create_snapshot(src, snapshot_dir=tmp_path, label="rt-test")

        mismatches = []
        for src_file in src.rglob("*"):
            if src_file.is_symlink() or not src_file.is_file():
                continue
            rel = src_file.relative_to(src)
            snap_file = snap / rel
            if not snap_file.exists():
                mismatches.append(f"MISSING: {rel}")
                continue
            if not filecmp.cmp(str(src_file), str(snap_file), shallow=False):
                src_sha = hashlib.sha256(src_file.read_bytes()).hexdigest()[:12]
                snap_sha = hashlib.sha256(snap_file.read_bytes()).hexdigest()[:12]
                mismatches.append(f"CONTENT_DIFFERS: {rel} src={src_sha} snap={snap_sha}")

        assert not mismatches, "Round-trip failures:\n" + "\n".join(mismatches)

    def test_snapshot_large_binary_blob_integrity(self, tmp_path: Path) -> None:
        """Large binary blob (~200 KB) must survive copytree byte-for-byte."""
        import hashlib

        from yadgar.backup import create_snapshot

        src = self._build_complex_source(tmp_path)
        snap = create_snapshot(src, snapshot_dir=tmp_path, label="rt-test")

        src_blob = src / "level1" / "large_blob.bin"
        snap_blob = snap / "level1" / "large_blob.bin"

        assert snap_blob.exists(), "Large blob missing from snapshot"
        assert snap_blob.stat().st_size == src_blob.stat().st_size, (
            f"Size mismatch: src={src_blob.stat().st_size} snap={snap_blob.stat().st_size}"
        )
        src_sha = hashlib.sha256(src_blob.read_bytes()).hexdigest()
        snap_sha = hashlib.sha256(snap_blob.read_bytes()).hexdigest()
        assert src_sha == snap_sha, f"SHA256 mismatch: src={src_sha} snap={snap_sha}"

    def test_snapshot_empty_directory_preserved(self, tmp_path: Path) -> None:
        """Empty directories in source must be present in snapshot."""
        from yadgar.backup import create_snapshot

        src = self._build_complex_source(tmp_path)
        snap = create_snapshot(src, snapshot_dir=tmp_path, label="rt-test")

        empty_dir_in_snap = snap / "empty_dir"
        assert empty_dir_in_snap.exists(), "empty_dir missing from snapshot"
        assert empty_dir_in_snap.is_dir(), "empty_dir must be a directory in snapshot"
        assert not any(empty_dir_in_snap.iterdir()), "empty_dir must remain empty in snapshot"

    def test_snapshot_symlink_content_dereferenced(self, tmp_path: Path) -> None:
        """Symlink in source is dereferenced: snapshot contains a regular file with linked content."""
        from yadgar.backup import create_snapshot

        src = self._build_complex_source(tmp_path)
        snap = create_snapshot(src, snapshot_dir=tmp_path, label="rt-test")

        snap_link = snap / "readme_link.txt"
        assert snap_link.exists(), "Symlink target content must exist in snapshot"
        # copytree dereferences by default — must be a regular file, not a link
        assert not snap_link.is_symlink(), "Symlink must be dereferenced to regular file"
        expected = (src / "readme.txt").read_bytes()
        assert snap_link.read_bytes() == expected, (
            "Dereferenced symlink content must match original"
        )

    def test_snapshot_utf8_text_not_mangled(self, tmp_path: Path) -> None:
        """UTF-8 text file with non-ASCII content must survive copytree byte-identical."""
        from yadgar.backup import create_snapshot

        src = self._build_complex_source(tmp_path)
        snap = create_snapshot(src, snapshot_dir=tmp_path, label="rt-test")

        src_utf8 = src / "unicode.txt"
        snap_utf8 = snap / "unicode.txt"
        assert snap_utf8.exists(), "unicode.txt missing from snapshot"
        assert snap_utf8.read_bytes() == src_utf8.read_bytes(), (
            "UTF-8 file must be byte-identical in snapshot"
        )
