"""Exit codes for the vacuum finalize gates — which failures roll the swap back.

POLICY HISTORY (three moves, this file records the third):

1. v5.7.0 PR-2 made a check_invariants 404/non-2xx/exception warn-only (exit 0)
   after the 2026-05-23 incident where a successful vacuum was reported failed
   because core had not finished booting.
2. P0 #37 reversed that to a hard rollback after the 2026-07-09 split-brain
   (`.old` RETAINED while the backend kept writing the ORIGINAL inode for 16 h —
   RCA docs/plans/surrealkv-safe-stop-2026-07-10.md §4).
3. task:0045 (this change) narrows move 2 back off check_invariants ALONE.  The
   verification POSTed `{core}/api/check_invariants`, a route registered nowhere
   — it 404'd on every run for a month, so every vacuum rolled back while
   reporting a ~2 GB saving.  The route now exists
   (`yadgar/core/server/routes/admin_ops.py`), and the call is ADVISORY in the
   vacuum finalize path because it also returns ok=false on a healthy host with a
   pre-existing data-model violation a vacuum neither causes nor fixes.

What still rolls back and exits 2 (unchanged, asserted below): core does not
become healthy on the swapped-in DB, and post-swap inode incoherence.  Those are
the gates that detect a bad swap.  The EXACT per-table count comparison already
runs PRE-swap, so a partial import can never be swapped in at all.

check_invariants remains a HARD signal outside this path — the consolidation
tail still logs CRITICAL on violations.
"""

from __future__ import annotations

import tempfile
import types as _types
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

# ---------------------------------------------------------------------------
# Shared scaffolding
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


def _make_side_db(backend_url, filtered_path, side_path, source_counts):
    """Hermetic stand-in for the P2 side-build (no surreal subprocess).

    Creates the side path so the REAL _atomic_swap can rename it in, and returns
    True (verified).  The live side-build is covered by the e2e suite.
    """
    side_path.mkdir(parents=True, exist_ok=True)
    (side_path / "compacted.marker").write_bytes(b"compacted")
    return True


def _patch_stack(stack: ExitStack, monkeypatch) -> None:
    """Apply the standard vacuum mock patches via an ExitStack."""
    stack.enter_context(patch("yadgar.core.vacuum._log_consolidation_row"))
    stack.enter_context(patch("yadgar.core.vacuum.ServiceController"))
    stack.enter_context(patch("yadgar.core.vacuum._wait_for_health", return_value=True))
    stack.enter_context(patch("yadgar.core.vacuum._wait_for_yadgar_health", return_value=True))
    stack.enter_context(patch("yadgar.core.vacuum._redefine_users_post_import"))
    # P0 #37: the backend is quiesced at swap time (tests patch httpx.get to a
    # 200-for-everything fake, which the gate would read as LIVE) and the
    # post-swap store is inode-coherent (hermetic — no real /proc dependency).
    stack.enter_context(patch("yadgar.core.vacuum._assert_backend_quiesced", return_value=True))
    stack.enter_context(
        patch("yadgar.core.vacuum._verify_live_store_coherence", return_value=(True, set()))
    )
    # P2 side-build seams: capture a fixed source count + build/verify the side DB
    # hermetically (no surreal). The real export/strip/swap/finalize still run.
    stack.enter_context(
        patch("yadgar.core.vacuum._capture_table_counts", return_value={"memory": 1})
    )
    stack.enter_context(
        patch("yadgar.core.vacuum._build_and_verify_side_db", side_effect=_make_side_db)
    )


# ---------------------------------------------------------------------------
# TestCheckInvariantsRollsBack
# ---------------------------------------------------------------------------


class TestFinalizeGates:
    """Which finalize outcome rolls the swap back, and which one merely warns.

    HARD (rollback + exit 2): post-swap inode incoherence — a live surreal
    holding open fds outside the canonical path is the 07-09 split-brain itself.
    Core-health timeout is the other hard gate (covered in
    test_vacuum_finalize_verification.py).

    ADVISORY (keep the swap, exit 0): every check_invariants outcome.  The route
    did not exist until task:0045, so this branch fired on every run for a month;
    and served correctly it still returns ok=false on a host carrying a standing
    data-model violation, which would make the gate permanently unsatisfiable.
    """

    def _run_with_ci(  # noqa: C901 - cohesive: single helper drives all CI variants
        self,
        monkeypatch,
        ci_status: int | None = None,
        ci_raises: Exception | None = None,
        ci_ok: bool | None = None,
        coherent: bool = True,
    ) -> tuple[int, Path]:
        """Drive cmd_vacuum_impl end-to-end; mock check_invariants per args."""

        def fake_post(url: str, **kwargs) -> MagicMock:
            if "/api/check_invariants" in url:
                if ci_raises is not None:
                    raise ci_raises
                m = MagicMock()
                m.status_code = ci_status
                m.text = f"HTTP {ci_status}"
                m.json.return_value = {"ok": ci_ok if ci_ok is not None else ci_status == 200}
                return m
            m = MagicMock()
            m.status_code = 200
            m.text = "OK"
            return m

        monkeypatch.setattr(httpx, "get", _fake_get)
        monkeypatch.setattr(httpx, "post", fake_post)
        monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", "test-token")

        from yadgar.core.vacuum import cmd_vacuum_impl

        with tempfile.TemporaryDirectory() as td:
            db = _fake_db(td)
            monkeypatch.setenv("YADGAR_HOME", td)
            script = Path(td) / "cleanup-backups.sh"
            script.write_text("#!/bin/sh\nexit 0\n")
            script.chmod(0o755)
            monkeypatch.setenv("YADGAR_CLEANUP_SCRIPT", str(script))

            with ExitStack() as stack:
                _patch_stack(stack, monkeypatch)
                if not coherent:
                    stack.enter_context(
                        patch(
                            "yadgar.core.vacuum._verify_live_store_coherence",
                            return_value=(False, {"surreal_db.old-20260709_190000"}),
                        )
                    )
                result = cmd_vacuum_impl(_vacuum_args(db))

            # Snapshot rollback-relevant state BEFORE the tempdir vanishes.
            olds = list(Path(td).glob("surreal_db.old-*"))
            canonical_is_original = (db / "vlog" / "00001.vlog").exists()
            compacted_retained = (db / "compacted.marker").exists()

        self._olds = olds
        self._canonical_is_original = canonical_is_original
        self._compacted_retained = compacted_retained
        return result, db

    def _assert_rolled_back(self, result: int) -> None:
        assert result == 2, f"a rolled-back vacuum must exit 2 (nightly goes red); got {result}"
        assert self._canonical_is_original, (
            "rollback must promote .old (the original DB) back to the canonical path"
        )
        assert not self._compacted_retained, "the unverified compacted DB must be discarded"
        assert self._olds == [], (
            f"no half-swapped .old may remain after finalize (07-09 guard); got {self._olds}"
        )

    def _assert_swap_retained(self, result: int) -> None:
        assert result == 0, (
            f"an advisory check_invariants outcome must not fail the run; got {result}"
        )
        assert self._compacted_retained, "the compacted DB stays canonical"
        assert not self._canonical_is_original, "the original must not be promoted back"
        assert self._olds == [], ".old must be retired so the space is actually reclaimed"

    def test_inode_split_brain_rolls_back_exit_2(self, monkeypatch):
        """The HARD gate: a live store outside the canonical path is the 07-09 bug."""
        result, _ = self._run_with_ci(monkeypatch, ci_status=200, ci_ok=True, coherent=False)
        self._assert_rolled_back(result)

    # POLICY REVERSAL (task:0045 D2): the four cases below asserted rollback+exit 2
    # until this change.  The 404 case is the one that actually fired in
    # production — against a route registered nowhere — discarding seven good
    # compactions while reporting a ~2 GB saving each time.
    def test_check_invariants_404_is_advisory(self, monkeypatch):
        result, _ = self._run_with_ci(monkeypatch, ci_status=404)
        self._assert_swap_retained(result)

    def test_check_invariants_non2xx_is_advisory(self, monkeypatch):
        result, _ = self._run_with_ci(monkeypatch, ci_status=503)
        self._assert_swap_retained(result)

    def test_check_invariants_connection_error_is_advisory(self, monkeypatch):
        result, _ = self._run_with_ci(monkeypatch, ci_raises=httpx.ConnectError("refused"))
        self._assert_swap_retained(result)

    def test_check_invariants_ok_false_is_advisory(self, monkeypatch):
        result, _ = self._run_with_ci(monkeypatch, ci_status=200, ci_ok=False)
        self._assert_swap_retained(result)

    def test_check_invariants_ok_true_exits_0_and_retires_old(self, monkeypatch):
        result, _ = self._run_with_ci(monkeypatch, ci_status=200, ci_ok=True)
        assert result == 0
        assert self._olds == [], ".old must be retired on a verified vacuum"
        assert self._compacted_retained, "the verified compacted DB stays canonical"

    def test_rollback_is_loud(self, monkeypatch, capsys):
        self._run_with_ci(monkeypatch, ci_status=200, ci_ok=True, coherent=False)
        err = capsys.readouterr().err
        assert "CRITICAL" in err and "ROLLING BACK" in err, (
            f"rollback must be loud (07-09 was silent); stderr={err!r}"
        )

    def test_advisory_failure_is_loud_too(self, monkeypatch, capsys):
        """Advisory must not mean quiet — a non-ok result still needs an operator."""
        self._run_with_ci(monkeypatch, ci_status=404)
        err = capsys.readouterr().err
        assert "ADVISORY" in err and "check_invariants" in err, (
            f"an advisory check_invariants failure must still be logged; stderr={err!r}"
        )
