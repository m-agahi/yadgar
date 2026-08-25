"""task:0046 (A) — the residue reap must run on EVERY vacuum exit path.

THE BUG.  ``_reap_stale_export_scratch`` was called from three sites, all inside
``_vacuum_finalize`` — reachable only after Phase 3 succeeds.  Every earlier
return (preflight skip, unreachable backend, export failure, snapshot failure,
Phase 3 abort, …) skipped it, so a host that keeps aborting — which is exactly
what a container-only or low-disk host does — accumulated ~100 MB export pairs
without bound.  ``_reap_stale_pre_vacuum_snapshots`` got abort-path coverage in
Car 0092 by adding a call to each abort return, which is how the two reapers
drifted apart in the first place.

THE FIX under test: ONE reap site, in ``cmd_vacuum_impl``'s ``finally``, which no
return and no exception can bypass.

DELIBERATE EXCLUSION — the lock-held exit.  ``cmd_vacuum_impl`` returns 0 at the
``sensitive_lock.acquire`` failure BEFORE entering the try, so the ``finally``
cannot fire for it, and it must not: when another sensitive job holds the lock a
LIVE vacuum owns the in-flight export scratch and snapshot, and reaping under its
lock is precisely the race the lock exists to prevent.  Pinned below so a future
reader does not "close the gap".

Scaffolding is imported from ``test_vacuum_preflight``, following the precedent
in ``test_vacuum_core_stays_up.py`` (a shared vacuum harness lives with the suite
that introduced it rather than being duplicated).
"""

from __future__ import annotations

from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from yadgar.tests.core.test_vacuum_preflight import (
    _base_patches,
    _fake_db,
    _fake_get,
    _fake_post,
    _vacuum_args,
)

# Seeded residue is dated within the age backstop window so this module tests
# the COVERAGE property alone; the age backstop has its own tests in
# test_vacuum_cleanup.py.
_SEEDED_RUNS = 5
_KEEP_N = 2  # VACUUM_SNAPSHOT_RETENTION under test
_KEEP_RUNS = 1  # _VACUUM_EXPORT_KEEP_RUNS under test


def _recent_stamps(n: int) -> list[str]:
    """`n` stamps, oldest first, all inside the 14d age backstop window."""
    now = datetime.now(UTC)
    return [(now - timedelta(hours=n - i)).strftime("%Y%m%d_%H%M%S") for i in range(n)]


def _seed_residue(home: Path) -> list[str]:
    """Seed _SEEDED_RUNS export pairs + snapshot dirs.  Returns the stamps used.

    Callers MUST use the returned list rather than re-deriving it — recomputing
    from ``datetime.now()`` at assert time makes any "is this stamp one of the
    seeded ones?" check pass vacuously whenever the test crosses a second
    boundary.
    """
    stamps = _recent_stamps(_SEEDED_RUNS)
    for stamp in stamps:
        (home / f"vacuum_export_{stamp}.surql").write_text("raw")
        (home / f"vacuum_export_{stamp}.filtered.surql").write_text("filtered")
        (home / f"surreal_db.pre-vacuum-{stamp}" / "vlog").mkdir(parents=True)
    return stamps


def _surviving_export_runs(home: Path) -> set[str]:
    """The distinct run stamps still on disk."""
    return {
        p.name.removeprefix("vacuum_export_").split(".")[0] for p in home.glob("vacuum_export_*")
    }


def _surviving_snapshots(home: Path) -> list[str]:
    return sorted(p.name for p in home.glob("surreal_db.pre-vacuum-*"))


# ---------------------------------------------------------------------------
# Exit-path drivers.  Each returns a callable installing the patches that force
# cmd_vacuum_impl down one specific exit, plus the exit code it must produce.
# ---------------------------------------------------------------------------


def _exit_recovery_fail(stack: ExitStack, td: str) -> None:
    stack.enter_context(
        patch("yadgar.core.vacuum._recover_interrupted_swap", side_effect=RuntimeError("boom"))
    )


def _exit_backend_unreachable(stack: ExitStack, td: str) -> None:
    stack.enter_context(patch("yadgar.core.vacuum._check_backend_reachable", return_value=False))


def _exit_skip_no_surreal(stack: ExitStack, td: str) -> None:
    stack.enter_context(
        patch(
            "yadgar.core.vacuum._has_side_build_launcher", return_value=(False, "stub-fail-detail")
        )
    )


def _exit_skip_low_disk(stack: ExitStack, td: str) -> None:
    stack.enter_context(
        patch("yadgar.core.vacuum._has_side_build_launcher", return_value=(True, ""))
    )
    stack.enter_context(patch("yadgar.core.vacuum._has_free_space", return_value=False))


def _exit_count_capture_fail(stack: ExitStack, td: str) -> None:
    stack.enter_context(
        patch("yadgar.core.vacuum._has_side_build_launcher", return_value=(True, ""))
    )
    stack.enter_context(
        patch("yadgar.core.vacuum._capture_table_counts", side_effect=RuntimeError("no counts"))
    )


def _exit_export_fail(stack: ExitStack, td: str) -> None:
    stack.enter_context(
        patch("yadgar.core.vacuum._has_side_build_launcher", return_value=(True, ""))
    )
    stack.enter_context(
        patch("yadgar.core.vacuum._capture_table_counts", return_value={"memory": 1})
    )
    stack.enter_context(
        patch("yadgar.core.vacuum._vacuum_export", side_effect=RuntimeError("export died"))
    )


def _exit_snapshot_fail(stack: ExitStack, td: str) -> None:
    stack.enter_context(
        patch("yadgar.core.vacuum._has_side_build_launcher", return_value=(True, ""))
    )
    stack.enter_context(
        patch("yadgar.core.vacuum._capture_table_counts", return_value={"memory": 1})
    )
    stack.enter_context(
        patch(
            "yadgar.core.vacuum._vacuum_export",
            return_value=(Path(td) / "raw.surql", Path(td) / "filtered.surql"),
        )
    )
    stack.enter_context(
        patch("yadgar.core.vacuum._vacuum_snapshot_and_drop", side_effect=RuntimeError("copy died"))
    )
    stack.enter_context(patch("yadgar.core.vacuum._restart_services_after_abort"))


def _exit_phase3_abort(stack: ExitStack, td: str) -> None:
    stack.enter_context(
        patch("yadgar.core.vacuum._has_side_build_launcher", return_value=(True, ""))
    )
    stack.enter_context(
        patch("yadgar.core.vacuum._capture_table_counts", return_value={"memory": 1})
    )
    stack.enter_context(
        patch(
            "yadgar.core.vacuum._vacuum_export",
            return_value=(Path(td) / "raw.surql", Path(td) / "filtered.surql"),
        )
    )
    stack.enter_context(
        patch("yadgar.core.vacuum._vacuum_snapshot_and_drop", return_value=Path(td) / "snap")
    )
    stack.enter_context(patch("yadgar.core.vacuum._side_build_swap_and_start", return_value=None))


def _exit_finalize_success(stack: ExitStack, td: str) -> None:
    _exit_phase3_abort(stack, td)


def _exit_body_raises(stack: ExitStack, td: str) -> None:
    stack.enter_context(
        patch("yadgar.core.vacuum._recover_interrupted_swap", side_effect=KeyboardInterrupt)
    )


_EXITS = {
    "recovery-fail": (_exit_recovery_fail, 1),
    "missing-canonical": (None, 1),  # handled by not creating the db dir
    "backend-unreachable": (_exit_backend_unreachable, 1),
    "skip-no-surreal": (_exit_skip_no_surreal, 0),
    "skip-low-disk": (_exit_skip_low_disk, 0),
    "count-capture-fail": (_exit_count_capture_fail, 1),
    "export-fail": (_exit_export_fail, 1),
    "snapshot-fail": (_exit_snapshot_fail, 1),
    "phase3-abort": (_exit_phase3_abort, 1),
    "finalize-success": (_exit_finalize_success, 0),
}


def _drive(monkeypatch, tmp_path: Path, exit_name: str) -> int:
    td = str(tmp_path)
    db = _fake_db(td)
    if exit_name == "missing-canonical":
        import shutil as _shutil

        _shutil.rmtree(db)
    _seed_residue(tmp_path)

    monkeypatch.setattr(httpx, "get", _fake_get)
    monkeypatch.setattr(httpx, "post", _fake_post)
    monkeypatch.setenv("YADGAR_HOME", td)
    monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("YADGAR_VACUUM_SNAPSHOT_RETENTION", str(_KEEP_N))

    from yadgar.core.vacuum import cmd_vacuum_impl

    driver = _EXITS[exit_name][0]
    with ExitStack() as stack:
        _base_patches(stack)
        stack.enter_context(patch("yadgar.core.vacuum._maintenance_enter", return_value=False))
        stack.enter_context(patch("yadgar.core.vacuum._maintenance_exit"))
        stack.enter_context(patch("yadgar.core.vacuum._drain_backend_queue"))
        if driver is not None:
            driver(stack, td)
        if exit_name == "finalize-success":
            stack.enter_context(
                patch(
                    "yadgar.core.vacuum._side_build_swap_and_start", return_value=tmp_path / "old"
                )
            )
            stack.enter_context(patch("yadgar.core.vacuum._vacuum_finalize", return_value=True))
        return cmd_vacuum_impl(_vacuum_args(db))


@pytest.mark.parametrize("exit_name", sorted(_EXITS))
def test_residue_reaped_on_every_exit_path(monkeypatch, tmp_path, exit_name):
    """RED for 8 of the 10 on the export half before this car.

    Seeds 5 export runs and 5 snapshot dirs, drives one exit, and asserts the
    windows shrank.  This parametrisation IS the car: the reap being correct is
    worth nothing on a path that never calls it.
    """
    expected_code = _EXITS[exit_name][1]

    code = _drive(monkeypatch, tmp_path, exit_name)

    assert code == expected_code, f"{exit_name}: unexpected exit code {code}"
    runs = _surviving_export_runs(tmp_path)
    assert len(runs) <= _KEEP_RUNS, (
        f"{exit_name}: export window is {len(runs)} runs, ceiling is {_KEEP_RUNS} — "
        "this exit path does not reach the reap"
    )
    snaps = _surviving_snapshots(tmp_path)
    assert len(snaps) == _KEEP_N, (
        f"{exit_name}: snapshot window is {len(snaps)}, expected {_KEEP_N} — {snaps}"
    )


def test_residue_reaped_when_the_body_raises(monkeypatch, tmp_path):
    """The `finally` property, unreachable by any return-site patch.

    A ``BaseException`` (SIGINT during swap recovery) is the case that no amount
    of adding calls to each ``return`` can ever cover.
    """
    td = str(tmp_path)
    db = _fake_db(td)
    _seed_residue(tmp_path)
    monkeypatch.setattr(httpx, "get", _fake_get)
    monkeypatch.setattr(httpx, "post", _fake_post)
    monkeypatch.setenv("YADGAR_HOME", td)
    monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("YADGAR_VACUUM_SNAPSHOT_RETENTION", str(_KEEP_N))

    from yadgar.core.vacuum import cmd_vacuum_impl

    with ExitStack() as stack:
        _base_patches(stack)
        stack.enter_context(patch("yadgar.core.vacuum._maintenance_enter", return_value=False))
        stack.enter_context(patch("yadgar.core.vacuum._maintenance_exit"))
        stack.enter_context(patch("yadgar.core.vacuum._drain_backend_queue"))
        _exit_body_raises(stack, td)
        with pytest.raises(KeyboardInterrupt):
            cmd_vacuum_impl(_vacuum_args(db))

    assert len(_surviving_export_runs(tmp_path)) <= _KEEP_RUNS
    assert len(_surviving_snapshots(tmp_path)) == _KEEP_N


def test_current_run_export_survives_an_abort(monkeypatch, tmp_path):
    """Fix the leak, keep the diagnostics.

    ADR-0076 D2 keeps the aborting run's own scratch for forensics.  A
    keep-newest-N window preserves that for free — but only if the ceiling is
    expressed in RUNS and the current run is the newest one.
    """
    td = str(tmp_path)
    db = _fake_db(td)
    seeded = _seed_residue(tmp_path)
    monkeypatch.setattr(httpx, "get", _fake_get)
    monkeypatch.setattr(httpx, "post", _fake_post)
    monkeypatch.setenv("YADGAR_HOME", td)
    monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("YADGAR_VACUUM_SNAPSHOT_RETENTION", str(_KEEP_N))

    from yadgar.core.vacuum import cmd_vacuum_impl

    with ExitStack() as stack:
        _base_patches(stack)
        stack.enter_context(patch("yadgar.core.vacuum._maintenance_enter", return_value=False))
        stack.enter_context(patch("yadgar.core.vacuum._maintenance_exit"))
        stack.enter_context(patch("yadgar.core.vacuum._drain_backend_queue"))
        stack.enter_context(
            patch("yadgar.core.vacuum._has_side_build_launcher", return_value=(True, ""))
        )
        stack.enter_context(
            patch("yadgar.core.vacuum._capture_table_counts", return_value={"memory": 1})
        )
        # The REAL _vacuum_export runs, so this run writes its own pair with a
        # now-stamp — then Phase 2 aborts.
        stack.enter_context(
            patch("yadgar.core.vacuum._vacuum_snapshot_and_drop", side_effect=RuntimeError("boom"))
        )
        stack.enter_context(patch("yadgar.core.vacuum._restart_services_after_abort"))
        code = cmd_vacuum_impl(_vacuum_args(db))

    assert code == 1
    runs = _surviving_export_runs(tmp_path)
    assert len(runs) == _KEEP_RUNS, f"the window must hold exactly {_KEEP_RUNS} run(s): {runs}"
    # `seeded` is the list _seed_residue actually wrote — NOT a re-derivation, so
    # this cannot pass by failing to match its own fixture.
    assert not (runs & set(seeded)), (
        f"the surviving run must be THIS run's own scratch (the newest), not a seeded one: {runs}"
    )


class TestComposedFloor:
    """The floor must hold for the COMPOSITION, not just each reaper alone.

    ``_reap_stale_pre_vacuum_snapshots`` and ``_reap_snapshots_by_age`` each
    guarantee at least one survivor in isolation, and each is tested that way in
    test_vacuum_cleanup.py — but production never calls either in isolation.
    ``_reap_vacuum_residue`` runs them back to back, so a floor bug lives exactly
    here: the count-prune leaves N, and the age-backstop then sees all N as
    ancient.  Cheap to get wrong, catastrophic if wrong (no rollback anchor).
    """

    def _ancient_snapshots(self, home: Path, n: int) -> list[Path]:
        now = datetime.now(UTC)
        made = []
        for i in range(n):
            stamp = (now - timedelta(days=400 + i)).strftime("%Y%m%d_%H%M%S")
            snap = home / f"surreal_db.pre-vacuum-{stamp}"
            (snap / "vlog").mkdir(parents=True)
            made.append(snap)
        return made

    def test_hostile_keep_n_and_every_snapshot_expired_still_leaves_one(self, tmp_path):
        """Both floor mechanisms attacked at once: keep_n=0 AND everything ancient."""
        from types import SimpleNamespace

        from yadgar.core.vacuum import _reap_vacuum_residue

        self._ancient_snapshots(tmp_path, 4)
        settings = SimpleNamespace(VACUUM_SNAPSHOT_RETENTION=0, VACUUM_SNAPSHOT_MAX_AGE_DAYS=14)

        _reap_vacuum_residue(tmp_path, settings)

        remaining = sorted(tmp_path.glob("surreal_db.pre-vacuum-*"))
        assert len(remaining) == 1, (
            "a vacuum must NEVER leave the host without a rollback anchor "
            f"(ADR-0090); {len(remaining)} survived"
        )

    def test_a_raising_reap_does_not_skip_the_reaps_after_it(self, tmp_path, capsys):
        """Per-step try/except: step 1 blowing up must not cost steps 2 and 3."""
        from types import SimpleNamespace

        from yadgar.core.vacuum import _reap_vacuum_residue

        for stamp in ("20260101_000000", "20260102_000000"):
            (tmp_path / f"vacuum_export_{stamp}.surql").write_text("raw")
            (tmp_path / f"vacuum_export_{stamp}.filtered.surql").write_text("filtered")
        settings = SimpleNamespace(VACUUM_SNAPSHOT_RETENTION=2, VACUUM_SNAPSHOT_MAX_AGE_DAYS=14)

        with patch(
            "yadgar.core.vacuum._reap_stale_pre_vacuum_snapshots",
            side_effect=OSError("disk on fire"),
        ):
            _reap_vacuum_residue(tmp_path, settings)

        assert "residue reap (pre-vacuum snapshots) failed" in capsys.readouterr().err
        runs = _surviving_export_runs(tmp_path)
        assert len(runs) == _KEEP_RUNS, (
            f"the export reap must still have run after the snapshot reap raised: {runs}"
        )


def test_lock_held_exit_deliberately_does_not_reap(monkeypatch, tmp_path):
    """A live vacuum owns the in-flight scratch — reaping under its lock is the race.

    This exit returns BEFORE the try/finally by design.  Pinned so "close the
    remaining gap" is recognisable as a regression, not a fix.
    """
    td = str(tmp_path)
    db = _fake_db(td)
    _seed_residue(tmp_path)
    monkeypatch.setenv("YADGAR_HOME", td)
    monkeypatch.setenv("YADGAR_VACUUM_SNAPSHOT_RETENTION", str(_KEEP_N))

    from yadgar.core.vacuum import cmd_vacuum_impl

    fake_lock = MagicMock()
    fake_lock.acquire.return_value = False
    fake_lock.read.return_value = {"job": "consolidation", "pid": 4242}
    with patch("yadgar.core.sensitive_lock.sensitive_lock", fake_lock):
        code = cmd_vacuum_impl(_vacuum_args(db))

    assert code == 0, "a held lock is a skip, not a failure"
    assert len(_surviving_export_runs(tmp_path)) == _SEEDED_RUNS
    assert len(_surviving_snapshots(tmp_path)) == _SEEDED_RUNS
