"""Tests for yadgar.scripts.nightly_cycle — nightly backup/consolidation/vacuum cycle.

TDD: written before implementation, confirmed red before green (v5.7.0 PR-1a).

Lifecycle under test (steps 1-7):
  1. Stop core (systemctl --user stop yadgar)
  2. Pre-backup snapshot (backup.create_snapshot label="nightly-pre")
  3. Consolidation (StorageEngine + ConsolidationScheduler.run_nightly_consolidation)
  4. Vacuum (cmd_vacuum_impl)
  5. Stop core again + post-backup snapshot (label="nightly-post") — quiesced
  6. Prune snapshots (backup.prune_snapshots)
  7. Restart core (systemctl --user start yadgar)
"""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Module constant
# ---------------------------------------------------------------------------

_MODULE = "yadgar.core.scripts.nightly_cycle"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_args(**kwargs):
    """Build a minimal args namespace for main()."""
    defaults = {
        "db_path": "/fake/surreal_db",
        "backend_url": "http://127.0.0.1:8080",
        "service_mode": "systemd",
        "retention": 3,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _import_module():
    """Import (or reload) nightly_cycle and return it."""
    import yadgar.core.scripts.nightly_cycle as _mod

    importlib.reload(_mod)
    return _mod


def _run_with_mocks(
    tmp_path: Path,
    *,
    extra_patches=None,
    sched_side_effect=None,
    vac_return=0,
    snap_side_effect=None,
):
    """Run main() with all external deps mocked. Returns (exit_code, module, mocks)."""
    mod = _import_module()

    mock_storage = MagicMock()
    mock_sched = MagicMock()
    mock_sched.run_nightly_consolidation.return_value = {"merged": 0}
    if sched_side_effect is not None:
        mock_sched.run_nightly_consolidation.side_effect = sched_side_effect

    db_dir = tmp_path / "surreal_db"
    db_dir.mkdir(exist_ok=True)

    args = _make_args(db_path=str(db_dir))

    snap_mock = MagicMock(return_value=tmp_path / "snap")
    if snap_side_effect is not None:
        snap_mock.side_effect = snap_side_effect

    mock_ctl = MagicMock(return_value=None)
    mock_maint = MagicMock(return_value=None)  # v5.50.3: _maintenance_http mock
    mock_prune = MagicMock(return_value=[])
    mock_vac = MagicMock(return_value=vac_return)

    base = dict(
        _run_systemctl=mock_ctl,
        _maintenance_http=mock_maint,  # v5.50.3: core stop/start now uses HTTP
        create_snapshot=snap_mock,
        prune_snapshots=mock_prune,
        cmd_vacuum_impl=mock_vac,
        StorageEngine=MagicMock(return_value=mock_storage),
        ConsolidationScheduler=MagicMock(return_value=mock_sched),
        Settings=MagicMock(return_value=SimpleNamespace(DB_PATH=str(db_dir))),
        EmbeddingEngine=MagicMock(return_value=MagicMock()),
        configure_logging=MagicMock(),
        default_retention=MagicMock(return_value=3),
    )
    if extra_patches:
        base.update(extra_patches)

    with patch.multiple(_MODULE, **base):
        code = mod.main(args)

    return (
        code,
        mod,
        {
            "storage": mock_storage,
            "sched": mock_sched,
            "ctl": mock_ctl,
            "maint": mock_maint,
            "snap": snap_mock,
            "prune": mock_prune,
            "vac": mock_vac,
        },
    )


# ---------------------------------------------------------------------------
# Importability
# ---------------------------------------------------------------------------


class TestImportability:
    def test_module_importable(self) -> None:
        """yadgar.scripts.nightly_cycle must be importable and expose main()."""
        from yadgar.core.scripts import nightly_cycle

        assert hasattr(nightly_cycle, "main")
        assert callable(nightly_cycle.main)


# ---------------------------------------------------------------------------
# Happy path: all steps succeed → exit 0
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_all_success_exits_zero(self, tmp_path: Path) -> None:
        code, _, _ = _run_with_mocks(tmp_path)
        assert code == 0

    def test_step_order_matches_lifecycle(self, tmp_path: Path) -> None:
        """Verify call ordering: enter-maint → pre-backup → consolidate → vacuum → post-backup → prune → exit-maint."""
        mod = _import_module()

        call_order = []

        def _ctl(action, unit):
            # v5.50.3: core no longer uses systemctl; backend still does
            call_order.append(f"systemctl_{action}_{unit}")

        def _maint_http(action, url):
            # v5.50.3: core stop/start replaced by maintenance HTTP
            call_order.append(f"maintenance_{action}")

        mock_storage = MagicMock()
        mock_sched = MagicMock()

        def _sched_factory(*a, **kw):
            call_order.append("consolidation_scheduler_init")
            return mock_sched

        def _storage_factory(*a, **kw):
            call_order.append("storage_engine_init")
            return mock_storage

        def _snap(*a, **kw):
            call_order.append("create_snapshot")
            return tmp_path / "snap"

        def _prune(*a, **kw):
            call_order.append("prune_snapshots")
            return []

        def _vacuum(args):
            call_order.append("cmd_vacuum_impl")
            return 0

        def _force_cons():
            call_order.append("run_nightly_consolidation")
            return {"merged": 0}

        mock_sched.run_nightly_consolidation.side_effect = _force_cons

        db_dir = tmp_path / "surreal_db"
        db_dir.mkdir()

        args = _make_args(db_path=str(db_dir))

        with patch.multiple(
            _MODULE,
            _run_systemctl=_ctl,
            _maintenance_http=_maint_http,  # v5.50.3
            create_snapshot=_snap,
            prune_snapshots=_prune,
            cmd_vacuum_impl=_vacuum,
            StorageEngine=_storage_factory,
            ConsolidationScheduler=_sched_factory,
            EmbeddingEngine=MagicMock(return_value=MagicMock()),
            Settings=MagicMock(return_value=SimpleNamespace(DB_PATH=str(db_dir))),
            configure_logging=MagicMock(),
            default_retention=MagicMock(return_value=3),
        ):
            mod.main(args)

        # Step 1: maintenance enter first (replaces systemctl stop yadgar)
        assert call_order[0] == "maintenance_enter", (
            f"First call must be maintenance_enter, got: {call_order}"
        )
        # Step 2: pre-backup (first snapshot) before consolidation
        first_snap = call_order.index("create_snapshot")
        storage_init = call_order.index("storage_engine_init")
        assert first_snap < storage_init, "Pre-backup must happen before StorageEngine opens"
        # Step 3: consolidation
        assert "storage_engine_init" in call_order
        assert "consolidation_scheduler_init" in call_order
        assert "run_nightly_consolidation" in call_order
        # Step 4: vacuum
        assert "cmd_vacuum_impl" in call_order
        cons_idx = call_order.index("run_nightly_consolidation")
        vac_idx = call_order.index("cmd_vacuum_impl")
        assert vac_idx > cons_idx, "Vacuum must come after consolidation"
        # Steps 5-6: second snapshot after vacuum
        snap_idxs = [i for i, v in enumerate(call_order) if v == "create_snapshot"]
        assert len(snap_idxs) == 2, f"Expected 2 snapshots, got: {call_order}"
        assert snap_idxs[1] > vac_idx, "Post-backup snapshot must be after vacuum"
        # Prune after post-backup
        prune_idx = call_order.index("prune_snapshots")
        assert prune_idx > snap_idxs[1], "Prune must come after post-backup snapshot"
        # Step 7: maintenance exit is last (replaces systemctl start yadgar)
        assert call_order[-1] == "maintenance_exit", (
            f"Last call must be maintenance_exit, got: {call_order}"
        )

    def test_structured_json_logged_per_step(self, tmp_path: Path) -> None:
        """Each step emits at least one structured log line (I14 schema)."""
        import logging

        mod = _import_module()

        # Install a custom handler to capture log records
        captured_records = []

        class CapturingHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured_records.append(record)

        cap_handler = CapturingHandler()
        cap_handler.setLevel(logging.DEBUG)
        nc_logger = logging.getLogger("yadgar.nightly_cycle")
        nc_logger.setLevel(logging.DEBUG)
        nc_logger.addHandler(cap_handler)
        nc_logger.propagate = False  # avoid double-counting

        mock_sched = MagicMock()
        mock_sched.force_consolidate.return_value = {"merged": 0}

        db_dir = tmp_path / "surreal_db"
        db_dir.mkdir()
        args = _make_args(db_path=str(db_dir))

        try:
            with patch.multiple(
                _MODULE,
                _run_systemctl=MagicMock(),
                _maintenance_http=MagicMock(return_value=None),  # v5.50.3
                create_snapshot=MagicMock(return_value=tmp_path / "snap"),
                prune_snapshots=MagicMock(return_value=[]),
                cmd_vacuum_impl=MagicMock(return_value=0),
                StorageEngine=MagicMock(return_value=MagicMock()),
                ConsolidationScheduler=MagicMock(return_value=mock_sched),
                EmbeddingEngine=MagicMock(return_value=MagicMock()),
                Settings=MagicMock(return_value=SimpleNamespace(DB_PATH=str(db_dir))),
                configure_logging=MagicMock(),
                default_retention=MagicMock(return_value=3),
            ):
                mod.main(args)
        finally:
            nc_logger.removeHandler(cap_handler)
            nc_logger.propagate = True

        # Check that nightly_cycle logger emitted records with I14 fields
        nc_records = captured_records
        assert len(nc_records) >= 7, (
            f"Expected ≥7 log records from nightly_cycle, got {len(nc_records)}: "
            f"{[r.getMessage() for r in nc_records]}"
        )
        # Spot-check that outcome field is present (I14 schema)
        outcome_records = [r for r in nc_records if hasattr(r, "outcome")]
        assert len(outcome_records) >= 7, (
            f"Expected ≥7 records with 'outcome' field, got {len(outcome_records)}"
        )

    def test_core_running_at_exit(self, tmp_path: Path) -> None:
        """Final maintenance action must be 'exit' (replaces systemctl start yadgar, v5.50.3)."""
        mod = _import_module()

        maint_calls = []

        def _maint_http(action, url):
            maint_calls.append(action)

        mock_sched = MagicMock()
        mock_sched.force_consolidate.return_value = {"merged": 0}

        db_dir = tmp_path / "surreal_db"
        db_dir.mkdir()
        args = _make_args(db_path=str(db_dir))

        with patch.multiple(
            _MODULE,
            _run_systemctl=MagicMock(),
            _maintenance_http=_maint_http,
            create_snapshot=MagicMock(return_value=tmp_path / "snap"),
            prune_snapshots=MagicMock(return_value=[]),
            cmd_vacuum_impl=MagicMock(return_value=0),
            StorageEngine=MagicMock(return_value=MagicMock()),
            ConsolidationScheduler=MagicMock(return_value=mock_sched),
            EmbeddingEngine=MagicMock(return_value=MagicMock()),
            Settings=MagicMock(return_value=SimpleNamespace(DB_PATH=str(db_dir))),
            configure_logging=MagicMock(),
            default_retention=MagicMock(return_value=3),
        ):
            mod.main(args)

        assert "exit" in maint_calls, f"Expected maintenance exit, got: {maint_calls}"
        assert maint_calls[-1] == "exit", f"Maintenance exit must be last: {maint_calls}"


# ---------------------------------------------------------------------------
# Stop core failure: step 10 — FATAL, abort
# ---------------------------------------------------------------------------


class TestStopCoreFailure:
    def test_stop_core_failure_nonfatal_cycle_proceeds(self, tmp_path: Path) -> None:
        """v5.72 (#62): maintenance-enter is BEST-EFFORT. If it fails (old/down core,
        404, unreachable), the nightly does NOT abort — it proceeds through backup +
        vacuum + prune and exits 0. A down/old core has no external writers to gate,
        so losing the whole cycle would be strictly worse than a small write-race window.
        """
        mod = _import_module()

        def _maint_fail(action, url):
            if action == "enter":
                raise ConnectionError("core unreachable")

        mock_snap = MagicMock(return_value=tmp_path / "snap")
        mock_vac = MagicMock(return_value=0)
        mock_prune = MagicMock(return_value=[])

        db_dir = tmp_path / "surreal_db"
        db_dir.mkdir()
        args = _make_args(db_path=str(db_dir))

        with patch.multiple(
            _MODULE,
            _run_systemctl=MagicMock(),
            _maintenance_http=_maint_fail,
            create_snapshot=mock_snap,
            prune_snapshots=mock_prune,
            cmd_vacuum_impl=mock_vac,
            StorageEngine=MagicMock(return_value=MagicMock()),
            ConsolidationScheduler=MagicMock(return_value=MagicMock()),
            EmbeddingEngine=MagicMock(return_value=MagicMock()),
            Settings=MagicMock(return_value=SimpleNamespace(DB_PATH=str(db_dir))),
            configure_logging=MagicMock(),
            default_retention=MagicMock(return_value=3),
        ):
            code = mod.main(args)

        # Best-effort enter failure must NOT abort: cycle runs to completion, exit 0.
        assert code == 0, f"maintenance-enter failure must be non-fatal (exit 0), got {code}"
        mock_snap.assert_called()  # pre-backup ran despite enter failure
        mock_vac.assert_called()  # vacuum ran
        mock_prune.assert_called()  # prune ran


# ---------------------------------------------------------------------------
# Pre-backup failure: step 20 — FATAL, abort
# ---------------------------------------------------------------------------


class TestPreBackupFailure:
    def test_pre_backup_failure_exits_20(self, tmp_path: Path) -> None:
        mod = _import_module()

        mock_vac = MagicMock(return_value=0)
        mock_prune = MagicMock(return_value=[])
        mock_sched = MagicMock()

        db_dir = tmp_path / "surreal_db"
        db_dir.mkdir()
        args = _make_args(db_path=str(db_dir))

        with patch.multiple(
            _MODULE,
            _run_systemctl=MagicMock(),
            _maintenance_http=MagicMock(return_value=None),  # v5.50.3
            create_snapshot=MagicMock(side_effect=RuntimeError("disk full")),
            prune_snapshots=mock_prune,
            cmd_vacuum_impl=mock_vac,
            StorageEngine=MagicMock(return_value=MagicMock()),
            ConsolidationScheduler=MagicMock(return_value=mock_sched),
            EmbeddingEngine=MagicMock(return_value=MagicMock()),
            Settings=MagicMock(return_value=SimpleNamespace(DB_PATH=str(db_dir))),
            configure_logging=MagicMock(),
            default_retention=MagicMock(return_value=3),
        ):
            code = mod.main(args)

        assert code == 20
        mock_vac.assert_not_called()
        mock_prune.assert_not_called()
        mock_sched.run_nightly_consolidation.assert_not_called()


# ---------------------------------------------------------------------------
# Consolidation failure: step 30 — non-fatal, vacuum + post-backup still run
# ---------------------------------------------------------------------------


class TestConsolidationFailure:
    def test_consolidation_failure_exits_30_continues(self, tmp_path: Path) -> None:
        mod = _import_module()

        mock_sched = MagicMock()
        mock_sched.run_nightly_consolidation.side_effect = RuntimeError("OOM")
        mock_vac = MagicMock(return_value=0)
        snap_calls = []

        def _snap(*a, **kw):
            snap_calls.append(kw.get("label", "?"))
            return tmp_path / "snap"

        db_dir = tmp_path / "surreal_db"
        db_dir.mkdir()
        args = _make_args(db_path=str(db_dir))

        with patch.multiple(
            _MODULE,
            _run_systemctl=MagicMock(),
            _maintenance_http=MagicMock(return_value=None),  # v5.50.3
            create_snapshot=_snap,
            prune_snapshots=MagicMock(return_value=[]),
            cmd_vacuum_impl=mock_vac,
            StorageEngine=MagicMock(return_value=MagicMock()),
            ConsolidationScheduler=MagicMock(return_value=mock_sched),
            EmbeddingEngine=MagicMock(return_value=MagicMock()),
            Settings=MagicMock(return_value=SimpleNamespace(DB_PATH=str(db_dir))),
            configure_logging=MagicMock(),
            default_retention=MagicMock(return_value=3),
        ):
            code = mod.main(args)

        assert code == 30, f"Expected 30, got {code}"
        mock_vac.assert_called_once()
        assert len(snap_calls) == 2, f"Expected 2 snapshots (pre + post), got: {snap_calls}"

    def test_consolidation_storage_engine_closed_on_failure(self, tmp_path: Path) -> None:
        """StorageEngine must be closed even when run_nightly_consolidation raises."""
        mod = _import_module()

        mock_storage = MagicMock()
        mock_sched = MagicMock()
        mock_sched.run_nightly_consolidation.side_effect = RuntimeError("crash")

        db_dir = tmp_path / "surreal_db"
        db_dir.mkdir()
        args = _make_args(db_path=str(db_dir))

        with patch.multiple(
            _MODULE,
            _run_systemctl=MagicMock(),
            _maintenance_http=MagicMock(return_value=None),  # v5.50.3
            create_snapshot=MagicMock(return_value=tmp_path / "snap"),
            prune_snapshots=MagicMock(return_value=[]),
            cmd_vacuum_impl=MagicMock(return_value=0),
            StorageEngine=MagicMock(return_value=mock_storage),
            ConsolidationScheduler=MagicMock(return_value=mock_sched),
            EmbeddingEngine=MagicMock(return_value=MagicMock()),
            Settings=MagicMock(return_value=SimpleNamespace(DB_PATH=str(db_dir))),
            configure_logging=MagicMock(),
            default_retention=MagicMock(return_value=3),
        ):
            mod.main(args)

        mock_storage.close.assert_called()


# ---------------------------------------------------------------------------
# Vacuum failure: step 40 — non-fatal, post-backup still runs
# ---------------------------------------------------------------------------


class TestVacuumFailure:
    def test_vacuum_failure_exits_40_post_backup_still_runs(self, tmp_path: Path) -> None:
        mod = _import_module()

        mock_sched = MagicMock()
        mock_sched.force_consolidate.return_value = {"merged": 0}
        snap_calls = []

        def _snap(*a, **kw):
            snap_calls.append(kw.get("label", "?"))
            return tmp_path / "snap"

        mock_prune = MagicMock(return_value=[])

        db_dir = tmp_path / "surreal_db"
        db_dir.mkdir()
        args = _make_args(db_path=str(db_dir))

        with patch.multiple(
            _MODULE,
            _run_systemctl=MagicMock(),
            _maintenance_http=MagicMock(return_value=None),  # v5.50.3
            create_snapshot=_snap,
            prune_snapshots=mock_prune,
            cmd_vacuum_impl=MagicMock(return_value=1),  # non-zero = failure
            StorageEngine=MagicMock(return_value=MagicMock()),
            ConsolidationScheduler=MagicMock(return_value=mock_sched),
            EmbeddingEngine=MagicMock(return_value=MagicMock()),
            Settings=MagicMock(return_value=SimpleNamespace(DB_PATH=str(db_dir))),
            configure_logging=MagicMock(),
            default_retention=MagicMock(return_value=3),
        ):
            code = mod.main(args)

        assert code == 40, f"Expected 40, got {code}"
        assert len(snap_calls) == 2, f"Expected pre + post snapshots, got: {snap_calls}"
        mock_prune.assert_called_once()

    def test_vacuum_exit_code_2_treated_as_success(self, tmp_path: Path) -> None:
        """cmd_vacuum_impl exit code 2 (succeeded, warn-only invariants) must not fail the cycle."""
        mod = _import_module()

        mock_sched = MagicMock()
        mock_sched.force_consolidate.return_value = {"merged": 0}

        db_dir = tmp_path / "surreal_db"
        db_dir.mkdir()
        args = _make_args(db_path=str(db_dir))

        with patch.multiple(
            _MODULE,
            _run_systemctl=MagicMock(),
            _maintenance_http=MagicMock(return_value=None),  # v5.50.3
            create_snapshot=MagicMock(return_value=tmp_path / "snap"),
            prune_snapshots=MagicMock(return_value=[]),
            cmd_vacuum_impl=MagicMock(return_value=2),  # degraded-success: warn-only invariants
            StorageEngine=MagicMock(return_value=MagicMock()),
            ConsolidationScheduler=MagicMock(return_value=mock_sched),
            EmbeddingEngine=MagicMock(return_value=MagicMock()),
            Settings=MagicMock(return_value=SimpleNamespace(DB_PATH=str(db_dir))),
            configure_logging=MagicMock(),
            default_retention=MagicMock(return_value=3),
        ):
            code = mod.main(args)

        assert code == 0, f"Exit code 2 from vacuum must yield overall exit 0, got {code}"

    def test_vacuum_failure_post_backup_uses_nightly_post_label(self, tmp_path: Path) -> None:
        mod = _import_module()

        mock_sched = MagicMock()
        mock_sched.force_consolidate.return_value = {}
        snap_labels = []

        def _snap(db_path, snapshot_dir=None, label="nightly", backend_url=None):
            snap_labels.append(label)
            return tmp_path / f"snap-{label}"

        db_dir = tmp_path / "surreal_db"
        db_dir.mkdir()
        args = _make_args(db_path=str(db_dir))

        with patch.multiple(
            _MODULE,
            _run_systemctl=MagicMock(),
            _maintenance_http=MagicMock(return_value=None),  # v5.50.3
            create_snapshot=_snap,
            prune_snapshots=MagicMock(return_value=[]),
            cmd_vacuum_impl=MagicMock(return_value=1),
            StorageEngine=MagicMock(return_value=MagicMock()),
            ConsolidationScheduler=MagicMock(return_value=mock_sched),
            EmbeddingEngine=MagicMock(return_value=MagicMock()),
            Settings=MagicMock(return_value=SimpleNamespace(DB_PATH=str(db_dir))),
            configure_logging=MagicMock(),
            default_retention=MagicMock(return_value=3),
        ):
            mod.main(args)

        assert "nightly-post" in snap_labels, f"Post-backup label not found: {snap_labels}"


# ---------------------------------------------------------------------------
# Multiple failures: first failing step's code is returned
# ---------------------------------------------------------------------------


class TestMultipleFailures:
    def test_consolidation_and_vacuum_both_fail_exits_30(self, tmp_path: Path) -> None:
        """When consolidation (30) and vacuum (40) both fail, exit code is 30."""
        mod = _import_module()

        mock_sched = MagicMock()
        mock_sched.run_nightly_consolidation.side_effect = RuntimeError("OOM")

        db_dir = tmp_path / "surreal_db"
        db_dir.mkdir()
        args = _make_args(db_path=str(db_dir))

        with patch.multiple(
            _MODULE,
            _run_systemctl=MagicMock(),
            _maintenance_http=MagicMock(return_value=None),  # v5.50.3
            create_snapshot=MagicMock(return_value=tmp_path / "snap"),
            prune_snapshots=MagicMock(return_value=[]),
            cmd_vacuum_impl=MagicMock(return_value=1),
            StorageEngine=MagicMock(return_value=MagicMock()),
            ConsolidationScheduler=MagicMock(return_value=mock_sched),
            EmbeddingEngine=MagicMock(return_value=MagicMock()),
            Settings=MagicMock(return_value=SimpleNamespace(DB_PATH=str(db_dir))),
            configure_logging=MagicMock(),
            default_retention=MagicMock(return_value=3),
        ):
            code = mod.main(args)

        assert code == 30, f"Expected 30 (earliest failure), got {code}"

    def test_post_backup_failure_exits_50(self, tmp_path: Path) -> None:
        """Post-backup failure returns exit code 50."""
        mod = _import_module()

        mock_sched = MagicMock()
        mock_sched.force_consolidate.return_value = {}
        call_count = [0]

        def _snap(*a, **kw):
            call_count[0] += 1
            if call_count[0] == 2:
                raise RuntimeError("post-backup disk error")
            return tmp_path / "snap"

        db_dir = tmp_path / "surreal_db"
        db_dir.mkdir()
        args = _make_args(db_path=str(db_dir))

        with patch.multiple(
            _MODULE,
            _run_systemctl=MagicMock(),
            _maintenance_http=MagicMock(return_value=None),  # v5.50.3
            create_snapshot=_snap,
            prune_snapshots=MagicMock(return_value=[]),
            cmd_vacuum_impl=MagicMock(return_value=0),
            StorageEngine=MagicMock(return_value=MagicMock()),
            ConsolidationScheduler=MagicMock(return_value=mock_sched),
            EmbeddingEngine=MagicMock(return_value=MagicMock()),
            Settings=MagicMock(return_value=SimpleNamespace(DB_PATH=str(db_dir))),
            configure_logging=MagicMock(),
            default_retention=MagicMock(return_value=3),
        ):
            code = mod.main(args)

        assert code == 50, f"Expected 50, got {code}"


# ---------------------------------------------------------------------------
# Snapshot labels
# ---------------------------------------------------------------------------


class TestSnapshotLabels:
    def test_pre_and_post_labels_correct(self, tmp_path: Path) -> None:
        mod = _import_module()

        mock_sched = MagicMock()
        mock_sched.force_consolidate.return_value = {}
        labels = []

        def _snap(db_path, snapshot_dir=None, label="nightly", backend_url=None):
            labels.append(label)
            return tmp_path / f"snap-{label}"

        db_dir = tmp_path / "surreal_db"
        db_dir.mkdir()
        args = _make_args(db_path=str(db_dir))

        with patch.multiple(
            _MODULE,
            _run_systemctl=MagicMock(),
            _maintenance_http=MagicMock(return_value=None),  # v5.50.3
            create_snapshot=_snap,
            prune_snapshots=MagicMock(return_value=[]),
            cmd_vacuum_impl=MagicMock(return_value=0),
            StorageEngine=MagicMock(return_value=MagicMock()),
            ConsolidationScheduler=MagicMock(return_value=mock_sched),
            EmbeddingEngine=MagicMock(return_value=MagicMock()),
            Settings=MagicMock(return_value=SimpleNamespace(DB_PATH=str(db_dir))),
            configure_logging=MagicMock(),
            default_retention=MagicMock(return_value=3),
        ):
            mod.main(args)

        assert len(labels) == 2, f"Expected 2 snapshot calls, got: {labels}"
        assert labels[0] == "nightly-pre", f"First label must be 'nightly-pre', got: {labels}"
        assert labels[1] == "nightly-post", f"Second label must be 'nightly-post', got: {labels}"


# ---------------------------------------------------------------------------
# Post-backup quiesced approach: core stopped before post-backup
# ---------------------------------------------------------------------------


class TestPostBackupQuiesced:
    def test_core_in_maintenance_before_post_backup(self, tmp_path: Path) -> None:
        """Core must be in maintenance mode before post-backup snapshot.

        v5.50.3: Core stays UP but enters maintenance mode in step 1 (maintenance_enter).
        Post-backup (step 5) runs while maintenance is still active (exit is in finally,
        after steps 3-6). This replaces the old 'core stopped before post-backup' check.
        """
        mod = _import_module()

        call_order = []

        def _maint_http(action, url):
            call_order.append(("maintenance", action))

        snap_count = [0]

        def _snap(*a, **kw):
            snap_count[0] += 1
            call_order.append(("create_snapshot", snap_count[0]))
            return tmp_path / "snap"

        mock_sched = MagicMock()
        mock_sched.force_consolidate.return_value = {}

        db_dir = tmp_path / "surreal_db"
        db_dir.mkdir()
        args = _make_args(db_path=str(db_dir))

        with patch.multiple(
            _MODULE,
            _run_systemctl=MagicMock(),
            _maintenance_http=_maint_http,
            create_snapshot=_snap,
            prune_snapshots=MagicMock(return_value=[]),
            cmd_vacuum_impl=MagicMock(return_value=0),
            StorageEngine=MagicMock(return_value=MagicMock()),
            ConsolidationScheduler=MagicMock(return_value=mock_sched),
            EmbeddingEngine=MagicMock(return_value=MagicMock()),
            Settings=MagicMock(return_value=SimpleNamespace(DB_PATH=str(db_dir))),
            configure_logging=MagicMock(),
            default_retention=MagicMock(return_value=3),
        ):
            mod.main(args)

        # maintenance_enter must appear before post-backup (2nd snapshot)
        post_snap_idx = next(i for i, ev in enumerate(call_order) if ev == ("create_snapshot", 2))
        pre_post_events = call_order[:post_snap_idx]
        enter_events = [ev for ev in pre_post_events if ev == ("maintenance", "enter")]
        assert len(enter_events) >= 1, (
            f"Core must enter maintenance before post-backup. Events before post-snap: {pre_post_events}"
        )

    def test_core_exits_maintenance_after_post_backup(self, tmp_path: Path) -> None:
        """Core maintenance mode must exit (step 7) after post-backup.

        v5.50.3: maintenance_exit replaces systemctl start yadgar.
        """
        mod = _import_module()

        maint_calls = []

        def _maint_http(action, url):
            maint_calls.append(action)

        mock_sched = MagicMock()
        mock_sched.force_consolidate.return_value = {}

        db_dir = tmp_path / "surreal_db"
        db_dir.mkdir()
        args = _make_args(db_path=str(db_dir))

        with patch.multiple(
            _MODULE,
            _run_systemctl=MagicMock(),
            _maintenance_http=_maint_http,
            create_snapshot=MagicMock(return_value=tmp_path / "snap"),
            prune_snapshots=MagicMock(return_value=[]),
            cmd_vacuum_impl=MagicMock(return_value=0),
            StorageEngine=MagicMock(return_value=MagicMock()),
            ConsolidationScheduler=MagicMock(return_value=mock_sched),
            EmbeddingEngine=MagicMock(return_value=MagicMock()),
            Settings=MagicMock(return_value=SimpleNamespace(DB_PATH=str(db_dir))),
            configure_logging=MagicMock(),
            default_retention=MagicMock(return_value=3),
        ):
            code = mod.main(args)

        assert code == 0
        assert "exit" in maint_calls, f"Core must exit maintenance. Calls: {maint_calls}"
        assert maint_calls[-1] == "exit", f"Maintenance exit must be last: {maint_calls}"


# ---------------------------------------------------------------------------
# YADGAR_DB_URL stays SET (server mode) during consolidation (#51)
# ---------------------------------------------------------------------------


class TestEmbeddedModeGuard:
    def test_db_url_stays_set_during_consolidation_server_mode(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """#51: consolidation now runs in SERVER mode — YADGAR_DB_URL stays SET
        when StorageEngine is constructed (no embedded pop)."""
        import os

        mod = _import_module()

        monkeypatch.setenv("YADGAR_DB_URL", "http://127.0.0.1:8080")
        db_url_at_construction = []

        def _storage_factory(db_path, *a, **kw):
            db_url_at_construction.append(os.environ.get("YADGAR_DB_URL"))
            return MagicMock()

        mock_sched = MagicMock()
        mock_sched.force_consolidate.return_value = {}

        db_dir = tmp_path / "surreal_db"
        db_dir.mkdir()
        args = _make_args(db_path=str(db_dir))

        with patch.multiple(
            _MODULE,
            _run_systemctl=MagicMock(),
            _maintenance_http=MagicMock(return_value=None),
            create_snapshot=MagicMock(return_value=tmp_path / "snap"),
            prune_snapshots=MagicMock(return_value=[]),
            cmd_vacuum_impl=MagicMock(return_value=0),
            StorageEngine=_storage_factory,
            ConsolidationScheduler=MagicMock(return_value=mock_sched),
            EmbeddingEngine=MagicMock(return_value=MagicMock()),
            Settings=MagicMock(return_value=SimpleNamespace(DB_PATH=str(db_dir))),
            configure_logging=MagicMock(),
            default_retention=MagicMock(return_value=3),
        ):
            mod.main(args)

        assert db_url_at_construction, "StorageEngine must be constructed"
        assert db_url_at_construction[0] == "http://127.0.0.1:8080", (
            f"#51: YADGAR_DB_URL must STAY SET (server mode) when StorageEngine opens, "
            f"got: {db_url_at_construction[0]}"
        )

    def test_db_url_restored_after_consolidation(self, tmp_path: Path, monkeypatch) -> None:
        """YADGAR_DB_URL must be restored after consolidation completes."""
        import os

        mod = _import_module()

        monkeypatch.setenv("YADGAR_DB_URL", "http://127.0.0.1:8080")
        db_url_at_vacuum = []

        def _vacuum_factory(args):
            db_url_at_vacuum.append(os.environ.get("YADGAR_DB_URL"))
            return 0

        mock_sched = MagicMock()
        mock_sched.force_consolidate.return_value = {}

        db_dir = tmp_path / "surreal_db"
        db_dir.mkdir()
        args = _make_args(db_path=str(db_dir))

        with patch.multiple(
            _MODULE,
            _run_systemctl=MagicMock(),
            _maintenance_http=MagicMock(return_value=None),
            create_snapshot=MagicMock(return_value=tmp_path / "snap"),
            prune_snapshots=MagicMock(return_value=[]),
            cmd_vacuum_impl=_vacuum_factory,
            StorageEngine=MagicMock(return_value=MagicMock()),
            ConsolidationScheduler=MagicMock(return_value=mock_sched),
            EmbeddingEngine=MagicMock(return_value=MagicMock()),
            Settings=MagicMock(return_value=SimpleNamespace(DB_PATH=str(db_dir))),
            configure_logging=MagicMock(),
            default_retention=MagicMock(return_value=3),
        ):
            mod.main(args)

        assert db_url_at_vacuum, "cmd_vacuum_impl must be called"
        assert db_url_at_vacuum[0] == "http://127.0.0.1:8080", (
            f"YADGAR_DB_URL must be restored before vacuum, got: {db_url_at_vacuum[0]}"
        )
