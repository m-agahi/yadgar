"""Side-build preflight + abort-path snapshot pruning (Car 0092).

THE BUG (two halves, both fixed here):

1. ``_build_and_verify_side_db`` spawns a throwaway ``surreal start`` HOST-side
   (``yadgar/core/_surreal_runner/_surreal_runner.py`` — a bare PATH-resolved
   ``subprocess.Popen(["surreal", ...])``).  On a container install that binary
   exists ONLY inside the ``yadgar-backend`` image.  There was no
   ``shutil.which("surreal")`` preflight anywhere in ``yadgar/core/vacuum/``, so
   the failure landed at the WORST possible moment: after the full ``/export``,
   after BOTH units were stopped, and after the full-size ``.pre-vacuum``
   ``copytree`` — where it was swallowed by ``_build_and_verify_side_db``'s broad
   ``except Exception`` and turned into a plain abort.

2. The ``.pre-vacuum-*`` prune (``_run_cleanup_script(..., keep_n)``) lived ONLY
   inside ``_vacuum_finalize``, which no abort path ever reaches.  So each failed
   night left one full-size DB copy on disk forever, until ``_has_free_space``
   started returning False — and THAT is a ``return 0`` SKIP, not a failure.
   End state: vacuum a permanent silent no-op reporting exit 0 with a green
   timer, and stale ``.pre-vacuum-*`` dirs parked on disk.

The fix is a preflight that answers "can I obtain a ``surreal`` that can build
this store?" BEFORE any destructive step, skipping loudly with a NAMED reason
that an operator can tell apart from the low-disk skip — plus a prune that runs
on the abort paths too, so the wedge does not survive the preflight for anyone
already carrying stale dirs.
"""

from __future__ import annotations

import os
import stat
import tempfile
import types as _types
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

# ---------------------------------------------------------------------------
# Scaffolding (mirrors test_vacuum_exit_code.py)
# ---------------------------------------------------------------------------


def _fake_db(td: str) -> Path:
    """Create a minimal fake surreal_db layout under td."""
    p = Path(td)
    db = p / "surreal_db"
    for sub in ("vlog", "sstables", "wal"):
        (db / sub).mkdir(parents=True)
    (db / "vlog" / "00001.vlog").write_bytes(b"x" * 1000)
    return db


def _vacuum_args(db: Path) -> _types.SimpleNamespace:
    return _types.SimpleNamespace(
        backend_url="http://127.0.0.1:8080",
        service_mode="manual",
        db_path=str(db),
        yes=True,
    )


_FAKE_SURQL = "-- TABLE DATA: memory ----\nUPSERT memory:1 CONTENT {};\n"


def _fake_get(url: str, **kwargs) -> MagicMock:
    m = MagicMock()
    m.status_code = 200
    m.text = _FAKE_SURQL if "/export" in url else ""
    return m


def _fake_post(url: str, **kwargs) -> MagicMock:
    m = MagicMock()
    m.status_code = 200
    m.text = "OK"
    m.json.return_value = {"ok": True}
    return m


def _write_fake_surreal(dirpath: Path, version_line: str = "surreal 3.1.5 for linux") -> Path:
    """Write an executable stub named `surreal` that prints *version_line*."""
    binary = dirpath / "surreal"
    binary.write_text(f"#!/bin/sh\necho '{version_line}'\n")
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return binary


class _Spies:
    """The destructive steps a preflight skip must happen BEFORE."""

    def __init__(self) -> None:
        self.export = MagicMock(name="_vacuum_export")
        self.snapshot = MagicMock(name="_vacuum_snapshot_and_drop")
        self.counts = MagicMock(name="_capture_table_counts", return_value={"memory": 1})
        # Return False so a run that gets this far ABORTS deterministically
        # (exit 1) without depending on any real subprocess behaviour — that is
        # the RED state this test distinguishes the skip from.
        self.side_build = MagicMock(name="_build_and_verify_side_db", return_value=False)

    def install(self, stack: ExitStack) -> None:
        stack.enter_context(patch("yadgar.core.vacuum._vacuum_export", self.export))
        stack.enter_context(patch("yadgar.core.vacuum._vacuum_snapshot_and_drop", self.snapshot))
        stack.enter_context(patch("yadgar.core.vacuum._capture_table_counts", self.counts))
        stack.enter_context(patch("yadgar.core.vacuum._build_and_verify_side_db", self.side_build))

    def assert_none_called(self) -> None:
        assert not self.counts.called, (
            "source-count capture ran — the preflight must skip before it"
        )
        assert not self.export.called, (
            "Phase 1 /export ran despite no usable `surreal` — the preflight must "
            "skip BEFORE the expensive/destructive steps"
        )
        assert not self.snapshot.called, (
            "Phase 2 stop-both-units + full-size copytree ran despite no usable "
            "`surreal` — this is the wedge itself"
        )
        assert not self.side_build.called, "the side build ran without a surreal binary"


def _base_patches(stack: ExitStack) -> MagicMock:
    """Patch the service/health seams; return the consolidation_log row mock."""
    row_log = stack.enter_context(patch("yadgar.core.vacuum._log_consolidation_row"))
    stack.enter_context(patch("yadgar.core.vacuum.ServiceController"))
    stack.enter_context(patch("yadgar.core.vacuum._wait_for_health", return_value=True))
    stack.enter_context(patch("yadgar.core.vacuum._wait_for_yadgar_health", return_value=True))
    stack.enter_context(patch("yadgar.core.vacuum._redefine_users_post_import"))
    stack.enter_context(patch("yadgar.core.vacuum._assert_backend_quiesced", return_value=True))
    stack.enter_context(
        patch("yadgar.core.vacuum._verify_live_store_coherence", return_value=(True, set()))
    )
    return row_log


def _run_vacuum(monkeypatch, td: str, db: Path, extra) -> tuple[int, MagicMock, _Spies]:
    """Drive cmd_vacuum_impl inside *td*; return (exit_code, row_log, spies)."""
    monkeypatch.setattr(httpx, "get", _fake_get)
    monkeypatch.setattr(httpx, "post", _fake_post)
    monkeypatch.setenv("YADGAR_HOME", td)
    monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", "test-token")

    from yadgar.core.vacuum import cmd_vacuum_impl

    spies = _Spies()
    with ExitStack() as stack:
        row_log = _base_patches(stack)
        spies.install(stack)
        extra(stack)
        result = cmd_vacuum_impl(_vacuum_args(db))
    return result, row_log, spies


def _skip_reason_of(row_log: MagicMock) -> str | None:
    """Extract skip_reason from the consolidation_log row, if any."""
    if not row_log.call_args_list:
        return None
    return row_log.call_args_list[-1].args[0].get("skip_reason")


# ---------------------------------------------------------------------------
# (a) skip BEFORE any destructive step
# ---------------------------------------------------------------------------


class TestSurrealBinaryPreflight:
    def test_no_surreal_on_path_skips_before_export_stop_and_copytree(self, monkeypatch, capsys):
        """No `surreal` resolvable → SKIP before /export, svc.stop() and copytree.

        This is the container-install case: the binary lives only inside the
        backend image.  Before the preflight this ran the full export, stopped
        BOTH units and made a full-size copy, and only THEN failed.
        """
        with tempfile.TemporaryDirectory() as td:
            db = _fake_db(td)
            empty_bin = Path(td) / "empty-bin"
            empty_bin.mkdir()
            monkeypatch.setenv("PATH", str(empty_bin))

            result, _row_log, spies = _run_vacuum(monkeypatch, td, db, lambda _s: None)

        assert result == 0, (
            f"a missing surreal binary is a SKIP (no destructive op performed), "
            f"not a failure; got exit {result}"
        )
        spies.assert_none_called()
        err = capsys.readouterr().err
        assert "SKIP" in err and "surreal" in err, (
            f"the skip must be LOUD and name the binary; stderr was:\n{err}"
        )

    def test_resolved_binary_and_version_are_logged(self, monkeypatch, capsys):
        """The preflight records WHICH surreal it resolved, and its version.

        Two binaries coexist on the reference workstation (nix 3.1.5 wins PATH,
        ~/.local/bin 3.0.5 is shadowed) — the log must say which one a run used.
        """
        with tempfile.TemporaryDirectory() as td:
            db = _fake_db(td)
            fake_bin = Path(td) / "bin"
            fake_bin.mkdir()
            binary = _write_fake_surreal(fake_bin, "surreal 3.1.5 for linux on x86_64")
            monkeypatch.setenv("PATH", str(fake_bin))

            _result, _row_log, _spies = _run_vacuum(monkeypatch, td, db, lambda _s: None)

        out = capsys.readouterr().out
        assert str(binary) in out, f"the resolved surreal path must be logged; stdout:\n{out}"
        assert "3.1.5" in out, f"the resolved surreal version must be logged; stdout:\n{out}"


# ---------------------------------------------------------------------------
# (b) the two skip reasons must not look identical to an operator
# ---------------------------------------------------------------------------


class TestSkipReasonsAreDistinct:
    def _run_missing_binary(self, monkeypatch) -> tuple[int, str | None, str]:
        with tempfile.TemporaryDirectory() as td:
            db = _fake_db(td)
            empty_bin = Path(td) / "empty-bin"
            empty_bin.mkdir()
            monkeypatch.setenv("PATH", str(empty_bin))
            result, row_log, _ = _run_vacuum(monkeypatch, td, db, lambda _s: None)
        return result, _skip_reason_of(row_log), ""

    def _run_low_disk(self, monkeypatch) -> tuple[int, str | None, str]:
        def _extra(stack: ExitStack) -> None:
            stack.enter_context(patch("yadgar.core.vacuum._has_surreal_binary", return_value=True))
            stack.enter_context(
                patch(
                    "yadgar.core.vacuum.shutil.disk_usage",
                    return_value=_types.SimpleNamespace(total=10_000, used=9_999, free=1),
                )
            )

        with tempfile.TemporaryDirectory() as td:
            db = _fake_db(td)
            result, row_log, _ = _run_vacuum(monkeypatch, td, db, _extra)
        return result, _skip_reason_of(row_log), ""

    def test_both_skips_exit_zero(self, monkeypatch):
        assert self._run_missing_binary(monkeypatch)[0] == 0
        assert self._run_low_disk(monkeypatch)[0] == 0

    def test_skip_reasons_are_named_and_different(self, monkeypatch):
        """ "cannot vacuum, no surreal binary" must not read like "low disk"."""
        _, missing_reason, _ = self._run_missing_binary(monkeypatch)
        _, low_disk_reason, _ = self._run_low_disk(monkeypatch)

        assert missing_reason, (
            "a skipped vacuum must write a consolidation_log row carrying a NAMED "
            "skip_reason — otherwise every skip reads as 'ran, saved 0 bytes'"
        )
        assert low_disk_reason, "the low-disk skip must also name its reason"
        assert missing_reason != low_disk_reason, (
            f"the two skip reasons are indistinguishable: both are {missing_reason!r}"
        )
        assert "surreal" in missing_reason
        assert "disk" in low_disk_reason or "space" in low_disk_reason

    def test_skip_row_reports_no_saving(self, monkeypatch):
        """A skip reclaimed nothing — the row must not imply otherwise."""
        with tempfile.TemporaryDirectory() as td:
            db = _fake_db(td)
            empty_bin = Path(td) / "empty-bin"
            empty_bin.mkdir()
            monkeypatch.setenv("PATH", str(empty_bin))
            _result, row_log, _ = _run_vacuum(monkeypatch, td, db, lambda _s: None)

        row = row_log.call_args_list[-1].args[0]
        assert row["saved_bytes"] == 0
        assert row["exit_code"] == 0
        assert row["rolled_back"] is False


class TestSkipReasonReachesTheDatabase:
    """`_log_consolidation_row` ENUMERATES its fields — a key added to the row
    dict and NOT added to the INSERT statement is silently dropped, and every
    other test patches the function so none of them would ever notice.  This
    test exercises the REAL function against a fake HTTP client.
    """

    def test_skip_reason_appears_in_the_insert_statement(self):
        from yadgar.core.vacuum import _log_consolidation_row

        captured: dict = {}

        class _FakeClient:
            headers: dict = {}

            def post(self, path, content=None, headers=None, params=None):
                captured["content"] = content
                captured["params"] = params
                return MagicMock(status_code=200)

            def close(self):
                pass

        with patch("yadgar.core.vacuum._build_http_client", return_value=_FakeClient()):
            _log_consolidation_row(
                {
                    "_backend_url": "http://127.0.0.1:8080",
                    "kind": "vacuum",
                    "started_at": "t0",
                    "finished_at": "t1",
                    "duration_seconds": 0.1,
                    "before_bytes": 1000,
                    "after_bytes": 1000,
                    "saved_bytes": 0,
                    "saved_pct": 0,
                    "rolled_back": False,
                    "exit_code": 0,
                    "skip_reason": "no_surreal_binary",
                }
            )

        stmt = captured.get("content") or ""
        assert "skip_reason" in stmt, (
            "skip_reason was dropped on the floor by the enumerated INSERT — the "
            "row dict says one thing and the DB records another"
        )
        assert captured["params"].get("skip_reason") == "no_surreal_binary"

    def test_normal_rows_carry_no_skip_fields(self):
        """A real (non-skipped) vacuum row must be unchanged by this addition."""
        from yadgar.core.vacuum import _log_consolidation_row

        captured: dict = {}

        class _FakeClient:
            headers: dict = {}

            def post(self, path, content=None, headers=None, params=None):
                captured["content"] = content
                return MagicMock(status_code=200)

            def close(self):
                pass

        with patch("yadgar.core.vacuum._build_http_client", return_value=_FakeClient()):
            _log_consolidation_row(
                {
                    "_backend_url": "http://127.0.0.1:8080",
                    "kind": "vacuum",
                    "started_at": "t0",
                    "finished_at": "t1",
                    "duration_seconds": 1.0,
                    "before_bytes": 2000,
                    "after_bytes": 1000,
                    "saved_bytes": 1000,
                    "saved_pct": 50,
                    "rolled_back": False,
                    "exit_code": 0,
                }
            )

        assert "skip_reason" not in (captured.get("content") or "")


# ---------------------------------------------------------------------------
# (c) .pre-vacuum-* pruned on the ABORT path
# ---------------------------------------------------------------------------


class TestPreVacuumSnapshotsPrunedOnAbort:
    def test_abort_path_prunes_stale_pre_vacuum_snapshots(self, monkeypatch):
        """An aborted run must not leave a full-size DB copy behind forever.

        The prune used to live ONLY in ``_vacuum_finalize``, which the abort path
        never reaches — so every failed night added one full-size copy until
        ``_has_free_space`` turned vacuum into a permanent exit-0 no-op.
        """
        keep_n = 3

        def _extra(stack: ExitStack) -> None:
            # Preflight passes; the side build is what fails (the real abort shape).
            stack.enter_context(patch("yadgar.core.vacuum._has_surreal_binary", return_value=True))

        with tempfile.TemporaryDirectory() as td:
            db = _fake_db(td)
            # Four stale snapshots from prior failed runs, oldest first.
            stale = []
            for i in range(4):
                snap = Path(td) / f"surreal_db.pre-vacuum-2026072{i}_000000"
                snap.mkdir()
                (snap / "vlog").mkdir()
                os.utime(snap, (1_000_000 + i * 1000, 1_000_000 + i * 1000))
                stale.append(snap)

            monkeypatch.setattr(httpx, "get", _fake_get)
            monkeypatch.setattr(httpx, "post", _fake_post)
            monkeypatch.setenv("YADGAR_HOME", td)
            monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", "test-token")
            monkeypatch.setenv("YADGAR_VACUUM_SNAPSHOT_RETENTION", str(keep_n))

            from yadgar.core.vacuum import cmd_vacuum_impl

            with ExitStack() as stack:
                _base_patches(stack)
                stack.enter_context(
                    patch("yadgar.core.vacuum._capture_table_counts", return_value={"memory": 1})
                )
                # The real _vacuum_export / _vacuum_snapshot_and_drop run: the
                # snapshot copytree is what creates this run's own .pre-vacuum dir.
                stack.enter_context(
                    patch("yadgar.core.vacuum._build_and_verify_side_db", return_value=False)
                )
                _extra(stack)
                result = cmd_vacuum_impl(_vacuum_args(db))

            remaining = sorted(Path(td).glob("surreal_db.pre-vacuum-*"))
            names = [p.name for p in remaining]

        assert result == 1, "a failed side build is an abort (exit 1), not a skip"
        assert len(remaining) == keep_n, (
            f"the abort path must prune .pre-vacuum-* down to keep_n={keep_n}; "
            f"{len(remaining)} left behind: {names}"
        )
        # The oldest stale dirs are the ones that go; this run's own snapshot
        # (newest) survives for forensics.
        assert stale[0].name not in names, "the OLDEST stale snapshot must be pruned first"
