"""test_vacuum_cleanup.py — C-3 regression: inline prune logic in vacuum.py.

Verifies that _run_cleanup_script() keeps exactly keep_n snapshots (by mtime,
newest-first) and deletes the rest.
"""

from __future__ import annotations

import os
import time


def test_prune_keeps_keep_n_newest(tmp_path):
    """10 snapshot files → only keep_n=3 remain (the 3 newest)."""
    from yadgar.core.vacuum import _run_cleanup_script

    # Create 10 fake snapshot files with distinct mtimes
    files = []
    for i in range(10):
        p = tmp_path / f"surreal_db.pre-vacuum-{i:04d}"
        p.mkdir()
        # Stagger mtimes so sort order is deterministic
        mtime = time.time() - (10 - i) * 100  # higher i → newer
        os.utime(p, (mtime, mtime))
        files.append(p)

    _run_cleanup_script(tmp_path, "surreal_db.pre-vacuum-*", keep_n=3)

    # Use glob to count only matching snapshots (tmp_path may have fixture-injected dirs).
    remaining = sorted(tmp_path.glob("surreal_db.pre-vacuum-*"), key=os.path.getmtime)
    assert len(remaining) == 3, f"Expected 3 remaining, got {len(remaining)}"

    # The 3 newest (i=7,8,9) should survive
    surviving_names = {p.name for p in remaining}
    assert "surreal_db.pre-vacuum-0007" in surviving_names
    assert "surreal_db.pre-vacuum-0008" in surviving_names
    assert "surreal_db.pre-vacuum-0009" in surviving_names


def test_prune_noop_when_fewer_than_keep_n(tmp_path):
    """Fewer files than keep_n → nothing deleted."""
    from yadgar.core.vacuum import _run_cleanup_script

    for i in range(3):
        p = tmp_path / f"snap-{i:04d}.bak"
        p.write_text("x")

    _run_cleanup_script(tmp_path, "snap-*.bak", keep_n=5)

    # Use glob to count only matching files (tmp_path may have fixture-injected dirs).
    remaining = list(tmp_path.glob("snap-*.bak"))
    assert len(remaining) == 3


def test_prune_deletes_files_not_dirs_only(tmp_path):
    """Works with plain files as well as directories."""
    from yadgar.core.vacuum import _run_cleanup_script

    for i in range(6):
        p = tmp_path / f"backup-{i:04d}.surql"
        p.write_text(f"content {i}")
        mtime = time.time() - (6 - i) * 60
        os.utime(p, (mtime, mtime))

    _run_cleanup_script(tmp_path, "backup-*.surql", keep_n=2)

    # Use glob to count only matching files (tmp_path may have fixture-injected dirs).
    remaining = list(tmp_path.glob("backup-*.surql"))
    assert len(remaining) == 2
