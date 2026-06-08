"""v5.49.0 Phase 8 — Upgrade snapshot artefact module tests.

TDD: tests written RED first, then implementation makes them GREEN.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts_fmt() -> str:
    """Timestamp format used by snapshot dirs."""
    return "%Y-%m-%dT%H-%M-%S-%fZ"


# ---------------------------------------------------------------------------
# Snapshot lifecycle tests
# ---------------------------------------------------------------------------


def test_create_snapshot_writes_timestamped_dir(tmp_path: Path) -> None:
    """create_snapshot() creates a dir with ISO-8601-derived name + chmod 700."""
    from yadgar.update.snapshot import create_snapshot

    snap = create_snapshot(base_dir=tmp_path)

    assert snap.path.exists()
    assert snap.path.is_dir()
    # Dir name matches timestamp pattern (with microseconds, no colons)
    name = snap.path.name
    dt = datetime.strptime(name, _ts_fmt())
    assert dt.year >= 2024

    # chmod 700
    mode = stat.S_IMODE(snap.path.stat().st_mode)
    assert mode == 0o700


def test_write_and_read_prev_image_tag(tmp_path: Path) -> None:
    """write_prev_image_tag() persists; read_prev_image_tag() returns same value."""
    from yadgar.update.snapshot import create_snapshot

    snap = create_snapshot(base_dir=tmp_path)
    tag = "docker.io/openfantasy/yadgar:5.48.0"
    snap.write_prev_image_tag(tag)

    assert (snap.path / "prev_image_tag").exists()
    assert snap.read_prev_image_tag() == tag


def test_write_and_read_prev_unit_file(tmp_path: Path) -> None:
    """write_prev_unit_file() / read_prev_unit_file() round-trip multi-line content."""
    from yadgar.update.snapshot import create_snapshot

    snap = create_snapshot(base_dir=tmp_path)
    content = "[Unit]\nDescription=yadgar\n\n[Service]\nExecStart=/usr/bin/yadgar\n"
    snap.write_prev_unit_file(content)

    assert (snap.path / "prev_unit_file").exists()
    assert snap.read_prev_unit_file() == content


def test_write_and_read_prev_cli_version(tmp_path: Path) -> None:
    """write_prev_cli_version() / read_prev_cli_version() round-trip."""
    from yadgar.update.snapshot import create_snapshot

    snap = create_snapshot(base_dir=tmp_path)
    snap.write_prev_cli_version("5.48.0")

    assert snap.read_prev_cli_version() == "5.48.0"


def test_append_forward_log_creates_then_appends(tmp_path: Path) -> None:
    """append_forward_log() creates the JSON array and appends subsequent entries."""
    from yadgar.update.snapshot import create_snapshot

    snap = create_snapshot(base_dir=tmp_path)
    snap.append_forward_log("PROBING", {"version": "5.49.0"})
    snap.append_forward_log("PULLING", {"image": "docker.io/openfantasy/yadgar:5.49.0"})

    log_path = snap.path / "forward_log.json"
    assert log_path.exists()
    entries = json.loads(log_path.read_text())
    assert len(entries) == 2
    assert entries[0]["state"] == "PROBING"
    assert entries[0]["detail"] == {"version": "5.49.0"}
    assert entries[1]["state"] == "PULLING"
    # chronological order: first entry ts <= second entry ts
    ts0 = datetime.fromisoformat(entries[0]["ts"])
    ts1 = datetime.fromisoformat(entries[1]["ts"])
    assert ts0 <= ts1


def test_append_rollback_log_separate_file(tmp_path: Path) -> None:
    """append_rollback_log() writes rollback_log.json, separate from forward_log.json."""
    from yadgar.update.snapshot import create_snapshot

    snap = create_snapshot(base_dir=tmp_path)
    snap.append_rollback_log("ROLLING_BACK", {"reason": "health-check-failed"})

    rollback_path = snap.path / "rollback_log.json"
    forward_path = snap.path / "forward_log.json"
    assert rollback_path.exists()
    assert not forward_path.exists()

    entries = json.loads(rollback_path.read_text())
    assert len(entries) == 1
    assert entries[0]["state"] == "ROLLING_BACK"


def test_atomic_write_no_partial_file(tmp_path: Path) -> None:
    """If os.replace raises mid-write, original file is unaffected (atomic)."""
    from yadgar.update.snapshot import create_snapshot

    snap = create_snapshot(base_dir=tmp_path)
    original_tag = "docker.io/openfantasy/yadgar:5.47.0"
    snap.write_prev_image_tag(original_tag)

    # Now patch os.replace to raise on the *next* call
    original_replace = os.replace
    call_count = 0

    def failing_replace(src: str, dst: str) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise OSError("simulated disk full")
        return original_replace(src, dst)

    with patch("os.replace", side_effect=failing_replace):
        with pytest.raises(OSError):
            snap.write_prev_image_tag("docker.io/openfantasy/yadgar:5.49.0")

    # Original content must be intact (no partial write)
    assert snap.read_prev_image_tag() == original_tag

    # No stray tmp files should linger
    tmp_files = list(snap.path.glob("tmp*"))
    assert tmp_files == [], f"Stale tmp files: {tmp_files}"


def test_list_snapshots_sorted_newest_first(tmp_path: Path) -> None:
    """list_snapshots() returns all snapshots newest-first by timestamp."""
    from yadgar.update.snapshot import create_snapshot, list_snapshots

    s1 = create_snapshot(base_dir=tmp_path)
    time.sleep(0.02)  # ensure distinct microsecond timestamps
    s2 = create_snapshot(base_dir=tmp_path)
    time.sleep(0.02)
    s3 = create_snapshot(base_dir=tmp_path)

    snaps = list_snapshots(base_dir=tmp_path)
    assert len(snaps) == 3
    # newest first
    assert snaps[0].path == s3.path
    assert snaps[1].path == s2.path
    assert snaps[2].path == s1.path


# ---------------------------------------------------------------------------
# Retention tests
# ---------------------------------------------------------------------------


def test_prune_keeps_n_newest(tmp_path: Path) -> None:
    """prune_old_snapshots(retention=2) keeps 2 newest, deletes the rest."""
    from yadgar.update.snapshot import create_snapshot, prune_old_snapshots

    snaps = []
    for _ in range(5):
        snaps.append(create_snapshot(base_dir=tmp_path))
        time.sleep(0.02)

    deleted = prune_old_snapshots(retention=2, base_dir=tmp_path)

    assert deleted == 3
    # Use list_snapshots to count only valid snapshot dirs (not conftest fixtures)
    from yadgar.update.snapshot import list_snapshots as _ls

    remaining = _ls(base_dir=tmp_path)
    assert len(remaining) == 2
    # The 2 newest should survive
    assert snaps[-1].path.exists()
    assert snaps[-2].path.exists()
    # The 3 oldest should be gone
    for snap in snaps[:3]:
        assert not snap.path.exists()


def test_prune_retention_zero_or_negative_keeps_all(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """prune_old_snapshots(retention=0) keeps all and emits a WARNING."""
    from yadgar.update.snapshot import create_snapshot, prune_old_snapshots

    for _ in range(3):
        create_snapshot(base_dir=tmp_path)
        time.sleep(0.01)

    with caplog.at_level(logging.WARNING, logger="yadgar.update.snapshot"):
        deleted = prune_old_snapshots(retention=0, base_dir=tmp_path)

    assert deleted == 0
    from yadgar.update.snapshot import list_snapshots as _ls

    assert len(_ls(base_dir=tmp_path)) == 3
    assert any("retention" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# Config three-way registration test
# ---------------------------------------------------------------------------


def test_snapshot_retention_config_three_way() -> None:
    """UPDATE_SNAPSHOT_RETENTION registered in Settings + registry + FIELD_META."""
    from yadgar.config import Settings
    from yadgar.config_registry import list_config
    from yadgar.config_yaml import FIELD_META

    # Settings has the field
    assert "UPDATE_SNAPSHOT_RETENTION" in Settings.model_fields

    # Registry has the env-var entry
    registry_names = {entry.name for entry in list_config()}
    assert "YADGAR_UPDATE_SNAPSHOT_RETENTION" in registry_names

    # FIELD_META has the yaml key
    assert "update_snapshot_retention" in FIELD_META
    assert FIELD_META["update_snapshot_retention"]["section"] == "update"
