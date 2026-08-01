"""P0 #37 items 3/5a/6 — vacuum split-brain fixes (RCA §4/§7).

The 07-09 incident: the nightly vacuum's check_invariants came back non-ok
(HTTP 404) and the swap's `.old` was RETAINED while the running backend kept
writing the ORIGINAL inode (= `.old`) for 16 h — a path/inode split-brain that
made `surreal_db` a stale decoy and turned the next deploy stop into a torn
manifest.

Three fixes under test:

  item 6 (root defect) — QUIESCENCE GATE: the Phase 2 backend stop
    (`svc.stop_backend()` since task:0111; `svc.stop()` before it) runs minutes
    before the swap (export + snapshot + side-build sit in between); nothing
    re-verified the backend was still down at swap time. The gate is
    BACKEND-scoped either way — it polls the SurrealDB port, so a live core does
    not trip it. `_atomic_swap` is now gated on
    `_assert_backend_quiesced` — a LIVE backend at swap time ABORTS the vacuum
    with the canonical untouched.

  item 3 — ROLLBACK on finalize failure: every finalize failure path
    (core-health timeout, check_invariants non-ok, check_invariants exception,
    inode-coherence violation) now ROLLS BACK the swap (`.old` promoted back to
    canonical) instead of retaining a half-swapped state. This deliberately
    REVERSES the v5.7.0 PR-2 warn-only policy: a vacuum that cannot be verified
    is discarded (compaction lost, data safe) rather than trusted.

  item 5a — INODE-COHERENCE check: after the post-swap backend start, the
    finalize asserts (via /proc fd scan) that no live surreal holds fds outside
    the canonical `surreal_db` dir; a violation is the 07-09 state and triggers
    the rollback.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

# ---------------------------------------------------------------------------
# item 6 — _assert_backend_quiesced
# ---------------------------------------------------------------------------


class TestQuiescenceGate:
    def test_unreachable_backend_is_quiesced(self):
        from yadgar.core.vacuum import _assert_backend_quiesced

        with patch(
            "yadgar.core.vacuum.httpx.get",
            side_effect=httpx.ConnectError("Connection refused"),
        ):
            assert _assert_backend_quiesced("http://127.0.0.1:8080") is True

    def test_live_backend_is_not_quiesced(self, capsys):
        from yadgar.core.vacuum import _assert_backend_quiesced

        with patch(
            "yadgar.core.vacuum.httpx.get",
            return_value=MagicMock(status_code=200),
        ):
            assert _assert_backend_quiesced("http://127.0.0.1:8080") is False
        assert "LIVE" in capsys.readouterr().err

    def test_any_http_answer_counts_as_live(self):
        """Even a 500 answer means SOMETHING has the port — do not swap."""
        from yadgar.core.vacuum import _assert_backend_quiesced

        with patch(
            "yadgar.core.vacuum.httpx.get",
            return_value=MagicMock(status_code=500),
        ):
            assert _assert_backend_quiesced("http://127.0.0.1:8080") is False


class TestSwapGatedOnQuiescence:
    def _run_swap(self, tmp_path, quiesced: bool):
        from yadgar.core.vacuum import _side_build_swap_and_start

        home = tmp_path
        db = home / "surreal_db"
        db.mkdir()
        (db / "original.marker").write_bytes(b"orig")
        filtered = home / "export.filtered.surql"
        filtered.write_bytes(b"-- surql")
        svc = MagicMock()

        def fake_side_build(backend_url, filtered_path, side_path, source_counts):
            side_path.mkdir(parents=True, exist_ok=True)
            (side_path / "compacted.marker").write_bytes(b"new")
            return True

        with (
            patch("yadgar.core.vacuum._build_and_verify_side_db", side_effect=fake_side_build),
            patch("yadgar.core.vacuum._assert_backend_quiesced", return_value=quiesced),
            patch("yadgar.core.vacuum._wait_for_health", return_value=True),
        ):
            result = _side_build_swap_and_start(
                "http://127.0.0.1:8080", filtered, db, home, {"memory": 1}, svc
            )
        return result, db, home

    def test_live_backend_aborts_swap_canonical_untouched(self, tmp_path):
        result, db, home = self._run_swap(tmp_path, quiesced=False)
        assert result is None, "swap must ABORT when the backend is live"
        assert (db / "original.marker").exists(), "canonical must be untouched on abort"
        assert not list(home.glob("surreal_db.old-*")), "no .old may be created on abort"

    def test_quiesced_backend_swap_proceeds(self, tmp_path):
        result, db, home = self._run_swap(tmp_path, quiesced=True)
        assert result is not None, "swap must proceed when the backend is quiesced"
        assert (db / "compacted.marker").exists(), "compacted side DB must be swapped in"
        assert (result / "original.marker").exists(), ".old must hold the previous canonical"


# ---------------------------------------------------------------------------
# item 3 — finalize failure paths ROLL BACK the swap (never retain half-swapped)
# ---------------------------------------------------------------------------


class TestFinalizeRollback:
    def _layout(self, tmp_path):
        home = tmp_path / "yadgar"
        home.mkdir()
        db = home / "surreal_db"
        db.mkdir()
        (db / "compacted.marker").write_bytes(b"new")
        old = home / "surreal_db.old-20260709_191332"
        old.mkdir()
        (old / "original.marker").write_bytes(b"orig")
        snap = home / "surreal_db.pre-vacuum-20260709_185900"
        snap.mkdir()
        return home, db, old, snap

    def _finalize(
        self,
        tmp_path,
        monkeypatch,
        *,
        health=True,
        ci_status=200,
        ci_ok=True,
        ci_raises=None,
        coherent=True,
    ):
        from yadgar.core.vacuum import _vacuum_finalize

        home, db, old, snap = self._layout(tmp_path)
        monkeypatch.setenv("YADGAR_PORT", "8765")
        svc = MagicMock()

        def fake_post(url, **kwargs):
            if ci_raises is not None:
                raise ci_raises
            return MagicMock(
                status_code=ci_status,
                json=lambda: {"ok": ci_ok},
                text=f"HTTP {ci_status}",
            )

        with (
            patch("yadgar.core.vacuum._wait_for_yadgar_health", return_value=health),
            patch("yadgar.core.vacuum.httpx.post", side_effect=fake_post),
            patch(
                "yadgar.core.vacuum._verify_live_store_coherence",
                return_value=(coherent, set() if coherent else {"surreal_db.old-20260709_191332"}),
            ),
        ):
            result = _vacuum_finalize(
                "http://127.0.0.1:8080", home, old, snap, svc, keep_n=3, db_path=db
            )
        return result, home, db, old, svc

    def _assert_rolled_back(self, result, home, db, old, svc):
        assert result is False
        assert (db / "original.marker").exists(), (
            ".old must be promoted back to the canonical path on rollback"
        )
        assert not old.exists(), "the retained .old must be GONE after rollback (re-converged)"
        assert not (db / "compacted.marker").exists(), "the unverified compacted DB is discarded"
        svc.stop_backend.assert_called_once()
        svc.start_backend.assert_called_once()

    def _assert_swap_retained(self, result, home, db, old, svc):
        """POLICY REVERSAL (task:0045 D2) — advisory check_invariants keeps the swap."""
        assert result is True
        assert (db / "compacted.marker").exists(), "the compacted DB stays canonical"
        assert not (db / "original.marker").exists(), "the original must not be promoted back"
        assert not old.exists(), ".old is retired so the space is reclaimed"
        svc.stop_backend.assert_not_called()

    # POLICY REVERSAL (task:0045 D2): the three check_invariants cases below
    # asserted rollback until this change.  The call targeted a route registered
    # nowhere, so the 404 branch fired on every production run; and even served
    # correctly, ok=false is the steady state on a host with a standing
    # data-model violation a vacuum neither causes nor fixes.  The gates that
    # actually detect a bad swap (core health, inode coherence, and the pre-swap
    # EXACT per-table count comparison) are untouched and asserted below.
    def test_ci_ok_false_is_advisory(self, tmp_path, monkeypatch):
        result, home, db, old, svc = self._finalize(tmp_path, monkeypatch, ci_ok=False)
        self._assert_swap_retained(result, home, db, old, svc)

    def test_ci_404_is_advisory(self, tmp_path, monkeypatch):
        result, home, db, old, svc = self._finalize(
            tmp_path, monkeypatch, ci_status=404, ci_ok=False
        )
        self._assert_swap_retained(result, home, db, old, svc)

    def test_ci_connection_error_is_advisory(self, tmp_path, monkeypatch):
        result, home, db, old, svc = self._finalize(
            tmp_path, monkeypatch, ci_raises=httpx.ConnectError("Connection refused")
        )
        self._assert_swap_retained(result, home, db, old, svc)

    def test_core_health_timeout_rolls_back(self, tmp_path, monkeypatch):
        result, home, db, old, svc = self._finalize(tmp_path, monkeypatch, health=False)
        self._assert_rolled_back(result, home, db, old, svc)

    def test_incoherent_store_rolls_back(self, tmp_path, monkeypatch):
        """5a wired: a live surreal holding .old fds (the 07-09 state) → rollback."""
        result, home, db, old, svc = self._finalize(tmp_path, monkeypatch, coherent=False)
        self._assert_rolled_back(result, home, db, old, svc)

    def test_rollback_is_loud(self, tmp_path, monkeypatch, capsys):
        self._finalize(tmp_path, monkeypatch, coherent=False)
        err = capsys.readouterr().err
        assert "CRITICAL" in err and "ROLLING BACK" in err

    def test_advisory_ci_failure_is_loud(self, tmp_path, monkeypatch, capsys):
        """Advisory is not silent — the operator must still see which check failed."""
        self._finalize(tmp_path, monkeypatch, ci_status=404, ci_ok=False)
        err = capsys.readouterr().err
        assert "ADVISORY" in err and "check_invariants" in err

    def test_ci_ok_true_retires_old_no_rollback(self, tmp_path, monkeypatch):
        result, home, db, old, svc = self._finalize(tmp_path, monkeypatch)
        assert result is True
        assert not old.exists(), ".old is retired on a verified vacuum"
        assert (db / "compacted.marker").exists(), "the compacted DB stays canonical"
        svc.stop_backend.assert_not_called()

    def test_no_half_swapped_state_survives_any_outcome(self, tmp_path, monkeypatch):
        """The core invariant: after finalize, .old-* never remains on disk."""
        for kwargs in (
            {},
            {"ci_ok": False},
            {"ci_status": 404, "ci_ok": False},
            {"ci_raises": httpx.ConnectError("boom")},
            {"health": False},
            {"coherent": False},
        ):
            result, home, db, old, svc = self._finalize(tmp_path, monkeypatch, **kwargs)
            assert not list(home.glob("surreal_db.old-*")), (
                f"half-swapped state (.old retained) after finalize({kwargs}) — "
                "this is exactly the 07-09 silent split-brain precondition"
            )
            import shutil

            shutil.rmtree(home)  # reset for next scenario


# ---------------------------------------------------------------------------
# item 5a — host-side /proc fd scan
# ---------------------------------------------------------------------------


def _fake_proc(tmp_path: Path, pid: int, argv: list[str], fd_targets: list[str]) -> Path:
    proc = tmp_path / "proc"
    pid_dir = proc / str(pid)
    (pid_dir / "fd").mkdir(parents=True)
    (pid_dir / "cmdline").write_bytes(b"\x00".join(a.encode() for a in argv) + b"\x00")
    for i, target in enumerate(fd_targets):
        os.symlink(target, pid_dir / "fd" / str(i))
    return proc


class TestStoreInodeCoherence:
    def test_surreal_fds_in_old_dir_detected(self, tmp_path):
        from yadgar.core.vacuum import _surreal_open_dir_names

        proc = _fake_proc(
            tmp_path,
            700783,
            ["surreal", "start", "surrealkv:///data/surreal_db"],
            [
                "/data/surreal_db.old-20260709_191332/vlog/00000000000000000002.vlog",
                "/data/surreal_db.old-20260709_191332/wal/00000000000000000009.wal",
            ],
        )
        assert _surreal_open_dir_names(proc) == {"surreal_db.old-20260709_191332"}

    def test_canonical_fds_are_coherent(self, tmp_path):
        from yadgar.core.vacuum import _verify_live_store_coherence

        proc = _fake_proc(
            tmp_path,
            123,
            ["surreal", "start", "surrealkv:///data/surreal_db"],
            ["/data/surreal_db/vlog/00000000000000000001.vlog"],
        )
        coherent, names = _verify_live_store_coherence(proc)
        assert coherent is True
        assert names == {"surreal_db"}

    def test_old_fds_are_incoherent(self, tmp_path):
        from yadgar.core.vacuum import _verify_live_store_coherence

        proc = _fake_proc(
            tmp_path,
            123,
            ["surreal", "start", "surrealkv:///data/surreal_db"],
            [
                "/data/surreal_db/manifest",
                "/data/surreal_db.old-20260709_191332/vlog/2.vlog",
            ],
        )
        coherent, names = _verify_live_store_coherence(proc)
        assert coherent is False
        assert "surreal_db.old-20260709_191332" in names

    def test_non_surreal_processes_ignored(self, tmp_path):
        from yadgar.core.vacuum import _surreal_open_dir_names

        proc = _fake_proc(
            tmp_path,
            999,
            ["python3", "-m", "something"],
            ["/data/surreal_db.old-20260709_191332/vlog/2.vlog"],
        )
        assert _surreal_open_dir_names(proc) == set()

    def test_no_surreal_processes_is_coherent(self, tmp_path):
        """Absence of a scannable surreal must NOT false-alarm."""
        from yadgar.core.vacuum import _verify_live_store_coherence

        (tmp_path / "proc").mkdir()
        coherent, names = _verify_live_store_coherence(tmp_path / "proc")
        assert coherent is True
        assert names == set()

    def test_unreadable_pid_dirs_skipped(self, tmp_path):
        from yadgar.core.vacuum import _surreal_open_dir_names

        proc = tmp_path / "proc"
        (proc / "42").mkdir(parents=True)  # no cmdline / fd — must not raise
        assert _surreal_open_dir_names(proc) == set()


# ---------------------------------------------------------------------------
# exit-code integration: rolled-back vacuum exits 2 (visible to systemd)
# ---------------------------------------------------------------------------


class TestExitCodeOnRollback:
    def test_finalize_false_maps_to_exit_2(self):
        """cmd_vacuum_impl returns 2 when finalize rolled back — the nightly
        unit goes RED instead of silently 'complete' (07-09 lesson: silence
        must be impossible).

        The old form of this test grepped for the literal ``0 if finalize_ok
        else 2``.  task:0045 replaced that expression with an explicit
        ``rolled_back`` flag (the report needs it too, to zero the saving), so
        the assertion now reads the source for the mapping rather than one
        spelling of it.  Behavioural coverage of the same mapping lives in
        test_vacuum_finalize_verification.py::TestRolledBackReportsZeroSaving.
        """
        import inspect

        from yadgar.core import vacuum

        src = inspect.getsource(vacuum._cmd_vacuum_body)
        assert "rolled_back = not finalize_ok" in src
        assert "exit_code = 2 if rolled_back else 0" in src
        assert "return exit_code" in src
