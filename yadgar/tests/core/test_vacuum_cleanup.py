"""test_vacuum_cleanup.py — C-3 regression: inline prune logic in vacuum.py.

Verifies that _run_cleanup_script() keeps exactly keep_n snapshots (by mtime,
newest-first) and deletes the rest.

Task 0046 extends this module with the retention SEMANTICS the file-counting
prune above cannot express: export scratch is retained per RUN (a run writes two
files) and ordered by the stamp in the FILENAME, and the pre-vacuum snapshot
window carries a never-zero floor plus an age backstop that always exempts the
newest.  The asymmetry between the two artefact types is deliberate and is the
thing most likely to be "tidied" into a bug later: an export is DIAGNOSTIC, so an
unparseable name means orphan-from-a-partial-write and it goes; a snapshot is the
last-resort ROLLBACK anchor (ADR-0090), so an unparseable name is not evidence of
staleness and it stays.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta

import pytest


def _stamp(days_ago: float = 0.0) -> str:
    """A `%Y%m%d_%H%M%S` stamp in the same UTC form both writers emit."""
    return (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%Y%m%d_%H%M%S")


def _seed_pair(home, stamp: str) -> tuple:
    """Write a complete export run: raw + filtered, as _vacuum_export does."""
    raw = home / f"vacuum_export_{stamp}.surql"
    filtered = home / f"vacuum_export_{stamp}.filtered.surql"
    raw.write_text("raw")
    filtered.write_text("filtered")
    return raw, filtered


def _seed_snapshot(home, stamp: str):
    """Create a pre-vacuum snapshot dir with the on-disk shape vacuum makes."""
    snap = home / f"surreal_db.pre-vacuum-{stamp}"
    (snap / "vlog").mkdir(parents=True)
    return snap


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


# ---------------------------------------------------------------------------
# task:0046 (C) — export retention counts RUNS, not files, and orders by stamp
# ---------------------------------------------------------------------------


class TestExportPairRetention:
    def test_orphan_raw_export_is_reaped_without_eating_the_window(self, tmp_path):
        """A half-written run must not consume a slot in the retention window.

        ``_run_cleanup_script(..., keep_runs * 2)`` counts FILES, so an odd file
        left by a run that died between the raw and the filtered write silently
        converts "keep 2 runs" into "keep 1 run plus a useless orphan".
        """
        from yadgar.core.vacuum.phases import _reap_export_pairs

        old = _seed_pair(tmp_path, "20260101_000000")
        new = _seed_pair(tmp_path, "20260102_000000")
        orphan = tmp_path / "vacuum_export_partial.surql"  # died before the stamp
        orphan.write_text("partial")

        _reap_export_pairs(tmp_path, keep_runs=2)

        assert not orphan.exists(), "the stampless partial-write orphan must be reaped"
        for path in (*old, *new):
            assert path.exists(), f"{path.name} is inside the 2-run window"

    def test_half_pair_costs_one_run_slot_not_two(self, tmp_path):
        """Three runs, the newest of which wrote only its raw file.

        File-counting keeps `keep_runs * 2 = 4` files: the half-pair plus one and
        a HALF of the runs below it.  Run-grouping keeps two whole runs.
        """
        from yadgar.core.vacuum.phases import _reap_export_pairs

        oldest = _seed_pair(tmp_path, "20260101_000000")
        middle = _seed_pair(tmp_path, "20260102_000000")
        half = tmp_path / "vacuum_export_20260103_000000.surql"
        half.write_text("raw only")

        _reap_export_pairs(tmp_path, keep_runs=2)

        assert half.exists(), "the newest run is inside the window even when incomplete"
        for path in middle:
            assert path.exists(), "the 2nd-newest run is inside the window, WHOLE"
        for path in oldest:
            assert not path.exists(), "the 3rd-newest run is outside a 2-run window"

    def test_pairs_are_never_half_deleted(self, tmp_path):
        """Every surviving stamp keeps ALL of its files, or none of them."""
        from yadgar.core.vacuum.phases import _reap_export_pairs

        for i in range(1, 6):
            _seed_pair(tmp_path, f"2026010{i}_000000")

        _reap_export_pairs(tmp_path, keep_runs=1)

        remaining = sorted(p.name for p in tmp_path.glob("vacuum_export_*"))
        assert remaining == [
            "vacuum_export_20260105_000000.filtered.surql",
            "vacuum_export_20260105_000000.surql",
        ], f"keep_runs=1 must leave exactly one COMPLETE pair, got {remaining}"

    def test_retention_window_is_stamp_ordered_not_mtime_ordered(self, tmp_path):
        """A `touch` on an old pair must not reshuffle the retention window.

        mtime is not a property of the vacuum run — an rsync, a restore, or a
        backup tool rewrites it.  The stamp in the filename IS the run identity.
        """
        from yadgar.core.vacuum.phases import _reap_export_pairs

        old = _seed_pair(tmp_path, "20260101_000000")
        newest = _seed_pair(tmp_path, "20260105_000000")
        future = time.time() + 3600
        for path in old:
            os.utime(path, (future, future))  # oldest run, newest mtime

        _reap_export_pairs(tmp_path, keep_runs=1)

        for path in newest:
            assert path.exists(), "the newest run BY STAMP must survive"
        for path in old:
            assert not path.exists(), "a touched old run must still be reaped"


# ---------------------------------------------------------------------------
# task:0046 (B) — the snapshot floor and the age backstop
# ---------------------------------------------------------------------------


class TestSnapshotFloor:
    @pytest.mark.parametrize("keep_n", [0, -1])
    def test_snapshot_retention_never_reaches_zero(self, tmp_path, keep_n):
        """A vacuum must NEVER leave the host without a rollback anchor.

        ADR-0090: SurrealKV closes uncleanly often enough that the quiesced
        ``.pre-vacuum`` copy is the recovery path of record.  The clamp lives in
        the reaper itself, not in the settings resolver, so a direct call with a
        hostile ``keep_n`` cannot wipe the anchor either.
        """
        from yadgar.core.vacuum import _reap_stale_pre_vacuum_snapshots

        # The snapshot COUNT window is mtime-ordered (_run_cleanup_script, kept
        # as-is: a snapshot is one dir, so file-counting is correct there), so
        # the mtimes must be staggered to make "newest" well-defined.  Only the
        # AGE backstop is stamp-ordered — see _reap_snapshots_by_age.
        for i in range(1, 4):
            snap = _seed_snapshot(tmp_path, f"2026010{i}_000000")
            os.utime(snap, (1_000_000 + i * 1000, 1_000_000 + i * 1000))

        _reap_stale_pre_vacuum_snapshots(tmp_path, keep_n)

        remaining = sorted(tmp_path.glob("surreal_db.pre-vacuum-*"))
        assert len(remaining) == 1, (
            f"keep_n={keep_n} must clamp to 1, not 0; {len(remaining)} snapshots left"
        )
        assert remaining[0].name == "surreal_db.pre-vacuum-20260103_000000"

    def test_age_backstop_exempts_the_newest_snapshot(self, tmp_path):
        """Every snapshot older than the cutoff still leaves the newest standing."""
        from yadgar.core.vacuum.phases import _reap_snapshots_by_age

        stale = _seed_snapshot(tmp_path, _stamp(days_ago=400))
        newest = _seed_snapshot(tmp_path, _stamp(days_ago=90))

        _reap_snapshots_by_age(tmp_path, max_age_days=14)

        assert not stale.exists(), "a 400d-old snapshot is past the backstop"
        assert newest.exists(), (
            "the newest snapshot is exempt UNCONDITIONALLY — a host that has not "
            "vacuumed in a year must still have a rollback anchor"
        )

    def test_age_backstop_keeps_recent_snapshots(self, tmp_path):
        from yadgar.core.vacuum.phases import _reap_snapshots_by_age

        recent = [_seed_snapshot(tmp_path, _stamp(days_ago=d)) for d in (1, 5, 13)]

        _reap_snapshots_by_age(tmp_path, max_age_days=14)

        for snap in recent:
            assert snap.exists(), f"{snap.name} is inside the 14d window"

    def test_age_backstop_keeps_a_snapshot_whose_name_has_no_stamp(self, tmp_path):
        """Asymmetry vs exports: an unreadable NAME is not evidence of staleness.

        Deleting a rollback anchor because its name did not parse is the exact
        failure mode this car must not introduce.
        """
        from yadgar.core.vacuum.phases import _reap_snapshots_by_age

        weird = tmp_path / "surreal_db.pre-vacuum-manual-copy"
        (weird / "vlog").mkdir(parents=True)
        _seed_snapshot(tmp_path, _stamp(days_ago=400))
        _seed_snapshot(tmp_path, _stamp(days_ago=500))

        _reap_snapshots_by_age(tmp_path, max_age_days=14)

        assert weird.exists(), "an unparseable snapshot name must be KEPT, never reaped"


# ---------------------------------------------------------------------------
# task:0046 (B) — the declared defaults
# ---------------------------------------------------------------------------


def test_default_snapshot_retention_is_two():
    from yadgar._shared.config import Settings

    assert Settings.model_fields["VACUUM_SNAPSHOT_RETENTION"].default == 2


def test_default_snapshot_max_age_days_is_fourteen():
    from yadgar._shared.config import Settings

    assert Settings.model_fields["VACUUM_SNAPSHOT_MAX_AGE_DAYS"].default == 14


def test_default_export_keep_runs_is_one():
    from yadgar.core.vacuum import _VACUUM_EXPORT_KEEP_RUNS

    assert _VACUUM_EXPORT_KEEP_RUNS == 1
