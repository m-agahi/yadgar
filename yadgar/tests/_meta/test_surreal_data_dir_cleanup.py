"""Task 307 — the SurrealDB fixture data dirs must not leak.

The leak this covers is not a tidiness issue: `/tmp/surreal_session_*` stores
accumulated to 4838 dirs / 49GB between 2026-08-01 and 2026-08-27, and the
pre-push `make e2e` gate was OOM-killed (Error 137) with 1132 of them (12GB)
live.  `_surreal_url_reserve` reaped the PROCESS and left the store behind, and
`_ensure_surreal_alive` abandoned one more store per respawn.

Two halves are proved here, because either alone is insufficient:

  * TEARDOWN (`_teardown_surreal_handle`, `purge_registered_test_data_dirs`) —
    covers the paths where the interpreter survives long enough to run cleanup.
  * BACKSTOP (`sweep_orphan_surreal_data_dirs`, `scripts/reap-test-surreal.sh`)
    — covers the path that actually produced the leak, a SIGKILL, where no
    fixture finaliser, `pytest_sessionfinish` or atexit hook runs at all.

INTERRUPT-PATH COVERAGE — stated precisely rather than claimed wholesale:
  * ^C (SIGINT) and pytest-timeout unwind reach `atexit`, so
    `test_atexit_handler_purges_data_dirs` covers them: it exercises the exact
    registered handler, not a stand-in.
  * SIGTERM does NOT reach `atexit` (Python installs no handler for it) and is
    therefore NOT covered by the teardown half — only by the sweep.
  * SIGKILL / OOM reaches nothing in-process and is UNTESTABLE from inside the
    dying process.  It is covered only by `TestOrphanSweep` +
    `TestReapScriptDirSweep`, which simulate the debris rather than the kill.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from yadgar.tests import conftest as C
from yadgar.tests._surreal_helpers import (
    _SPAWNED_SURREAL_DATA_DIRS,
    _SPAWNED_SURREAL_PIDS,
    purge_registered_test_data_dirs,
    register_test_data_dir,
    remove_test_data_dir,
    sweep_orphan_surreal_data_dirs,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_REAP_SCRIPT = _REPO_ROOT / "scripts" / "reap-test-surreal.sh"


@pytest.fixture(autouse=True)
def _isolate_registries():
    """Swap the module-level registries out for the duration of each test.

    Without this a test calling `purge_registered_test_data_dirs()` would delete
    the LIVE session SurrealDB's data dir out from under every subsequent test
    on this worker — the failure this file exists to prevent, self-inflicted.
    """
    saved_dirs = list(_SPAWNED_SURREAL_DATA_DIRS)
    saved_pids = list(_SPAWNED_SURREAL_PIDS)
    _SPAWNED_SURREAL_DATA_DIRS.clear()
    _SPAWNED_SURREAL_PIDS.clear()
    try:
        yield
    finally:
        _SPAWNED_SURREAL_DATA_DIRS.clear()
        _SPAWNED_SURREAL_DATA_DIRS.extend(saved_dirs)
        _SPAWNED_SURREAL_PIDS.clear()
        _SPAWNED_SURREAL_PIDS.extend(saved_pids)


def _make_store(root: Path, name: str, *, age_s: float = 0.0) -> Path:
    """Create a fake surrealkv store dir with one file, optionally back-dated."""
    d = root / name
    d.mkdir()
    (d / "000001.kv").write_bytes(b"x" * 64)
    if age_s:
        old = time.time() - age_s
        os.utime(d, (old, old))
    return d


# ---------------------------------------------------------------------------
# 1. Fixture teardown removes its directory (the headline claim)
# ---------------------------------------------------------------------------


class TestFixtureTeardown:
    def test_teardown_removes_data_dir_and_reaps_proc(self, tmp_path):
        """_teardown_surreal_handle deletes the store AND terminates the server."""
        store = _make_store(tmp_path, "surreal_session_deadbeef")
        proc = MagicMock()
        handle = {"proc": proc, "port": 1, "data_dir": str(store), "respawns": 0}

        C._teardown_surreal_handle(handle, wait_timeout=0.01)

        assert not store.exists(), "fixture teardown left its surrealkv store on disk"
        assert proc.terminate.called, "fixture teardown did not reap the surreal process"

    def test_teardown_removes_data_dir_even_when_proc_reap_raises(self, tmp_path):
        """The failure path still cleans up — a happy-path-only teardown is the bug."""
        store = _make_store(tmp_path, "surreal_session_cafebabe")
        proc = MagicMock()
        proc.terminate.side_effect = RuntimeError("boom")
        proc.wait.side_effect = RuntimeError("boom")
        handle = {"proc": proc, "port": 1, "data_dir": str(store), "respawns": 0}

        C._teardown_surreal_handle(handle, wait_timeout=0.01)

        assert not store.exists()

    def test_teardown_tolerates_a_handle_with_no_proc(self, tmp_path):
        """A handle whose spawn never completed still gets its dir removed."""
        store = _make_store(tmp_path, "surreal_session_00000000")
        C._teardown_surreal_handle({"proc": None, "data_dir": str(store)}, wait_timeout=0.01)
        assert not store.exists()


# ---------------------------------------------------------------------------
# 2. Respawn no longer abandons the previous store
# ---------------------------------------------------------------------------


class TestRespawnDoesNotAbandonStore:
    def test_ensure_surreal_alive_removes_the_old_data_dir(self, tmp_path, monkeypatch):
        """Every respawn used to orphan one store with no way back to its path."""
        import tempfile

        old = _make_store(tmp_path, "surreal_session_11111111")
        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
        monkeypatch.setattr(C, "_wait_for_health", lambda *a, **k: None)

        dead = MagicMock()
        dead.poll.return_value = 137  # SIGKILL'd
        handle = {"proc": dead, "port": 1, "data_dir": str(old), "respawns": 0}

        import yadgar.core._surreal_runner._surreal_runner as R

        monkeypatch.setattr(R, "spawn_surreal", lambda **kw: MagicMock())
        monkeypatch.setattr(
            "yadgar.tests._surreal_helpers.spawn_surreal", lambda **kw: MagicMock(), raising=False
        )

        assert C._ensure_surreal_alive(handle) is True
        assert not old.exists(), "respawn abandoned the previous surrealkv store"
        assert handle["data_dir"] != str(old)
        # The replacement is registered, so atexit/sessionfinish can reach it.
        assert handle["data_dir"] in _SPAWNED_SURREAL_DATA_DIRS
        remove_test_data_dir(handle["data_dir"])


# ---------------------------------------------------------------------------
# 3. Registry purge — the ^C / pytest-timeout path
# ---------------------------------------------------------------------------


class TestRegistryPurge:
    def test_purge_removes_registered_dirs(self, tmp_path):
        a = _make_store(tmp_path, "surreal_session_aaaaaaaa")
        b = _make_store(tmp_path, "surreal_respawn_bbbbbbbb")
        register_test_data_dir(str(a))
        register_test_data_dir(str(b))

        assert purge_registered_test_data_dirs() == 2
        assert not a.exists() and not b.exists()
        assert _SPAWNED_SURREAL_DATA_DIRS == []

    def test_atexit_handler_purges_data_dirs(self, tmp_path):
        """Covers ^C (SIGINT) and pytest-timeout unwind — both reach atexit.

        Exercises the exact function registered with `atexit.register`, so this
        is the wiring under test, not a re-implementation of it.
        """
        from yadgar.core._surreal_runner._surreal_runner import (
            _kill_all_spawned_surreal_atexit,
        )

        store = _make_store(tmp_path, "surreal_session_cccccccc")
        register_test_data_dir(str(store))

        _kill_all_spawned_surreal_atexit()

        assert not store.exists()


# ---------------------------------------------------------------------------
# 4. Prefix gate — the production-safety property
# ---------------------------------------------------------------------------


class TestPrefixGate:
    @pytest.mark.parametrize(
        "name",
        [
            "surreal_db",  # what the production daemon's /data store is called
            "yadgar_bench_surreal_x",  # benchmark-owned, not ours to delete
            "side-backend",  # vacuum launcher's side path
        ],
    )
    def test_remove_refuses_a_non_fixture_directory(self, tmp_path, name):
        d = _make_store(tmp_path, name)
        assert remove_test_data_dir(str(d)) is False
        assert d.exists(), f"cleanup deleted {name}, which it does not own"

    def test_register_refuses_a_non_fixture_directory(self, tmp_path):
        assert register_test_data_dir(str(tmp_path / "surreal_db")) is False
        assert _SPAWNED_SURREAL_DATA_DIRS == []


# ---------------------------------------------------------------------------
# 5. Orphan sweep — the SIGKILL backstop
# ---------------------------------------------------------------------------


@pytest.fixture
def _tmp_root(tmp_path, monkeypatch):
    """Point BOTH sweep roots at an isolated dir so the real /tmp is untouched."""
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    return tmp_path


class TestOrphanSweep:
    def test_sweep_removes_an_aged_orphan(self, _tmp_root, monkeypatch):
        import yadgar.core._surreal_runner._surreal_runner as R

        monkeypatch.setattr(R, "_live_surreal_cmdlines", list)
        orphan = _make_store(_tmp_root, "surreal_session_dddddddd", age_s=3600)

        assert sweep_orphan_surreal_data_dirs() >= 1
        assert not orphan.exists()

    def test_sweep_skips_a_dir_a_live_surreal_is_serving(self, _tmp_root, monkeypatch):
        """The guard that makes a concurrent test run safe."""
        import yadgar.core._surreal_runner._surreal_runner as R

        live = _make_store(_tmp_root, "surreal_session_eeeeeeee", age_s=3600)
        monkeypatch.setattr(
            R,
            "_live_surreal_cmdlines",
            lambda: [f"surreal start --bind 127.0.0.1:1 surrealkv://{live}"],
        )

        sweep_orphan_surreal_data_dirs()
        assert live.exists(), "sweep deleted a database a live surreal was serving"

    def test_sweep_skips_a_freshly_created_dir(self, _tmp_root, monkeypatch):
        """Covers the mkdtemp -> spawn window, where the dir is in no cmdline yet."""
        import yadgar.core._surreal_runner._surreal_runner as R

        monkeypatch.setattr(R, "_live_surreal_cmdlines", list)
        fresh = _make_store(_tmp_root, "surreal_session_ffffffff")

        sweep_orphan_surreal_data_dirs()
        assert fresh.exists()

    def test_sweep_ignores_directories_it_does_not_own(self, _tmp_root, monkeypatch):
        import yadgar.core._surreal_runner._surreal_runner as R

        monkeypatch.setattr(R, "_live_surreal_cmdlines", list)
        keep = _make_store(_tmp_root, "surreal_db", age_s=3600)
        bench = _make_store(_tmp_root, "yadgar_bench_surreal_1", age_s=3600)

        sweep_orphan_surreal_data_dirs()
        assert keep.exists() and bench.exists()


# ---------------------------------------------------------------------------
# 6. scripts/reap-test-surreal.sh — the operator-side backstop
# ---------------------------------------------------------------------------


def _run_reaper(tmp_root: Path) -> subprocess.CompletedProcess:
    """Run the script's DIRECTORY half only.

    `--dirs-only` on purpose: the script's other half SIGKILLs every
    `/tmp/pytest` surreal on the box, which would take out a concurrent test
    run's database.  A test for the dir sweep must not do that.
    """
    env = os.environ.copy()
    env["TMPDIR"] = str(tmp_root)
    return subprocess.run(
        ["bash", str(_REAP_SCRIPT), "--dirs-only"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


class TestReapScriptDirSweep:
    """No skip guard: the script is checked into this repo, so its absence is a
    failure to report, not a reason to go quiet."""

    def test_script_is_present(self):
        assert _REAP_SCRIPT.is_file(), f"{_REAP_SCRIPT} is missing"

    def test_script_removes_aged_orphans(self, tmp_path):
        a = _make_store(tmp_path, "surreal_session_99999999", age_s=3600)
        b = _make_store(tmp_path, "surreal_respawn_88888888", age_s=3600)

        result = _run_reaper(tmp_path)

        assert result.returncode == 0, result.stderr
        assert not a.exists() and not b.exists()

    def test_script_leaves_fresh_and_foreign_dirs_alone(self, tmp_path):
        fresh = _make_store(tmp_path, "surreal_session_77777777")
        prod = _make_store(tmp_path, "surreal_db", age_s=3600)
        bench = _make_store(tmp_path, "yadgar_bench_surreal_2", age_s=3600)

        result = _run_reaper(tmp_path)

        assert result.returncode == 0, result.stderr
        assert fresh.exists(), "reaper deleted a dir inside the mkdtemp->spawn window"
        assert prod.exists(), "reaper deleted a non-fixture store"
        assert bench.exists()

    def test_script_exits_zero_on_an_empty_tmp_root(self, tmp_path):
        """Unmatched globs must not make best-effort cleanup a failure."""
        assert _run_reaper(tmp_path).returncode == 0

    def test_script_sweeps_top_of_tmp_even_when_tmpdir_points_elsewhere(self, tmp_path):
        """`$TMPDIR` alone would miss the root the 4838-dir backlog actually sat in.

        Uses a uniquely-named, aged, unserved dir directly in /tmp, so removing
        it is correct by definition and cannot disturb a concurrent run.
        """
        stray = _make_store(Path("/tmp"), f"surreal_session_zz{os.getpid()}", age_s=3600)
        try:
            result = _run_reaper(tmp_path)  # TMPDIR is tmp_path, NOT /tmp
            assert result.returncode == 0, result.stderr
            assert not stray.exists(), "sweep only looked at $TMPDIR, missing /tmp"
        finally:
            if stray.exists():
                import shutil

                shutil.rmtree(stray, ignore_errors=True)
