"""
Tests for scripts/cleanup-backups.sh

Uses pytest + subprocess (bats unavailable in this environment).
Each test sets up a tmpdir with synthetic snapshots, invokes the shell
script, and asserts the correct survivors remain.

NOTE on sparse vs real blocks:
  The size-cap test uses fallocate to allocate real disk blocks so that
  `du -sBG` (block-based, what the script uses) reports real usage.
  truncate creates sparse files — du would report 0 blocks, never
  triggering the cap.
"""

import os
import subprocess
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "cleanup-backups.sh"


def run_cleanup(
    backup_dir: Path, env_extra: dict | None = None, extra_args: list | None = None
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["YADGAR_BACKUP_DIR"] = str(backup_dir)
    if env_extra:
        env.update(env_extra)
    cmd = ["bash", str(SCRIPT)] + (extra_args or [])
    return subprocess.run(cmd, env=env, capture_output=True, text=True)


def make_snapshot(
    backup_dir: Path, name: str, mtime_offset_days: float = 0.0, size_gib: float = 0.0
) -> Path:
    """
    Create a fake surreal_db_* snapshot directory.
    mtime_offset_days: negative = that many days in the past.
    size_gib: if > 0, allocate that many GiB of real blocks inside the dir.
    """
    snap = backup_dir / name
    snap.mkdir(parents=True, exist_ok=True)
    if size_gib > 0:
        filler = snap / "data"
        size_bytes = int(size_gib * 1024**3)
        try:
            subprocess.run(
                ["fallocate", "-l", str(size_bytes), str(filler)],
                check=True,
                capture_output=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            # fallocate unavailable — fall back to dd (slow but correct)
            subprocess.run(
                ["dd", "if=/dev/zero", f"of={filler}", "bs=1M", f"count={int(size_gib * 1024)}"],
                check=True,
                capture_output=True,
            )
    if mtime_offset_days != 0:
        offset_secs = mtime_offset_days * 86400
        new_mtime = time.time() + offset_secs
        os.utime(snap, (new_mtime, new_mtime))
    return snap


# ── Test 1: count cap ─────────────────────────────────────────────────────────


def test_count_cap(tmp_path):
    """10 snapshots in, MAX_COUNT=5 → exactly 5 remain."""
    backup_dir = tmp_path / "db"
    backup_dir.mkdir()

    # Create 10 snapshots with distinct mtimes (newest first when sorted by -dt)
    for i in range(10):
        snap = backup_dir / f"surreal_db_2026050{i}_120000"
        snap.mkdir()
        # Stagger mtimes: i=0 is newest (offset 0), i=9 is oldest (offset -9 h)
        offset_secs = -i * 3600
        new_mtime = time.time() + offset_secs
        os.utime(snap, (new_mtime, new_mtime))

    result = run_cleanup(
        backup_dir,
        env_extra={
            "YADGAR_BACKUP_MAX_COUNT": "5",
            "YADGAR_BACKUP_MAX_AGE_DAYS": "999",  # disable age cap
            "YADGAR_BACKUP_MAX_GIB": "999",  # disable size cap
        },
    )
    assert result.returncode == 0, f"Script failed: {result.stderr}"
    survivors = sorted(backup_dir.iterdir())
    assert len(survivors) == 5, (
        f"Expected 5 survivors, got {len(survivors)}: {[s.name for s in survivors]}"
    )


# ── Test 2: age cap ───────────────────────────────────────────────────────────


def test_age_cap(tmp_path):
    """3 snapshots: 2 older than 7 days, 1 recent → only the recent one survives."""
    backup_dir = tmp_path / "db"
    backup_dir.mkdir()

    make_snapshot(backup_dir, "surreal_db_20260501_000000", mtime_offset_days=-10)
    make_snapshot(backup_dir, "surreal_db_20260505_000000", mtime_offset_days=-8)
    make_snapshot(backup_dir, "surreal_db_20260513_000000", mtime_offset_days=-1)

    result = run_cleanup(
        backup_dir,
        env_extra={
            "YADGAR_BACKUP_MAX_AGE_DAYS": "7",
            "YADGAR_BACKUP_MAX_COUNT": "999",  # disable count cap
            "YADGAR_BACKUP_MAX_GIB": "999",  # disable size cap
        },
    )
    assert result.returncode == 0, f"Script failed: {result.stderr}"
    survivors = sorted(backup_dir.iterdir())
    assert len(survivors) == 1, (
        f"Expected 1 survivor, got {len(survivors)}: {[s.name for s in survivors]}"
    )
    assert survivors[0].name == "surreal_db_20260513_000000"


# ── Test 3: size cap ──────────────────────────────────────────────────────────


def test_size_cap(tmp_path):
    """7 snapshots × 2 GiB real blocks each = 14 GiB. MAX_GIB=5 → ≤5 GiB total."""
    backup_dir = tmp_path / "db"
    backup_dir.mkdir()

    for i in range(7):
        # Oldest = highest offset so it gets pruned first
        make_snapshot(
            backup_dir,
            f"surreal_db_2026050{i}_120000",
            mtime_offset_days=-(7 - i),  # i=0 oldest, i=6 newest
            size_gib=2.0,
        )

    result = run_cleanup(
        backup_dir,
        env_extra={
            "YADGAR_BACKUP_MAX_GIB": "5",
            "YADGAR_BACKUP_MAX_AGE_DAYS": "999",  # disable age cap
            "YADGAR_BACKUP_MAX_COUNT": "999",  # disable count cap
        },
    )
    assert result.returncode == 0, f"Script failed: {result.stderr}"

    # Measure real disk usage after cleanup
    du = subprocess.run(
        ["du", "-sBG", str(backup_dir)],
        capture_output=True,
        text=True,
        check=True,
    )
    used_gib = int(du.stdout.split()[0].rstrip("G"))
    assert used_gib <= 5, f"Total usage {used_gib} GiB exceeds 5 GiB cap after cleanup"
    survivors = list(backup_dir.iterdir())
    assert len(survivors) >= 1, "All snapshots were deleted — at least 1 must be kept"


# ── Test 4: dry-run ──────────────────────────────────────────────────────────


def test_dry_run(tmp_path):
    """--dry-run prints what it would delete but does not actually delete."""
    backup_dir = tmp_path / "db"
    backup_dir.mkdir()

    make_snapshot(backup_dir, "surreal_db_20260501_000000", mtime_offset_days=-10)
    make_snapshot(backup_dir, "surreal_db_20260513_000000", mtime_offset_days=-1)

    before = set(backup_dir.iterdir())
    result = run_cleanup(
        backup_dir,
        env_extra={
            "YADGAR_BACKUP_MAX_AGE_DAYS": "7",
            "YADGAR_BACKUP_MAX_COUNT": "999",
            "YADGAR_BACKUP_MAX_GIB": "999",
        },
        extra_args=["--dry-run"],
    )
    assert result.returncode == 0, f"Script failed: {result.stderr}"
    after = set(backup_dir.iterdir())
    assert before == after, (
        f"--dry-run deleted files: removed={before - after}, added={after - before}"
    )
    # Must print something about what it would remove
    combined = result.stdout + result.stderr
    assert "surreal_db_20260501_000000" in combined, (
        f"dry-run output did not mention the old snapshot. Output:\n{combined}"
    )


# ── Test 5: empty dir is a no-op ─────────────────────────────────────────────


def test_empty_dir_noop(tmp_path):
    """Empty backup dir should not error."""
    backup_dir = tmp_path / "db"
    backup_dir.mkdir()
    result = run_cleanup(backup_dir)
    assert result.returncode == 0, f"Script failed on empty dir: {result.stderr}"


# ── Test 6: nonexistent dir is a no-op ───────────────────────────────────────


def test_nonexistent_dir_noop(tmp_path):
    """Nonexistent backup dir should exit 0 (nothing to clean up)."""
    backup_dir = tmp_path / "db_does_not_exist"
    result = run_cleanup(backup_dir)
    assert result.returncode == 0, f"Script failed on nonexistent dir: {result.stderr}"
