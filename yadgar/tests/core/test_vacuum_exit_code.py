"""P0 #37 item 3 — check_invariants failure ROLLS BACK the swap and exits 2.

POLICY REVERSAL (deliberate). v5.7.0 PR-2 made check_invariants 404/non-2xx/
exception warn-only (exit 0) after the 2026-05-23 incident where a fully
successful vacuum was reported failed because core hadn't booted yet.

The 2026-07-09 incident showed the warn-only path's true cost: check_invariants
404 → `.old` RETAINED while the running backend kept writing the ORIGINAL inode
(= `.old`) for 16 h — silent path/inode split-brain that turned the next deploy
stop into a torn manifest (RCA docs/plans/surrealkv-safe-stop-2026-07-10.md §4).

New policy: an UNVERIFIED swap is a discarded swap. Any check_invariants
non-verification (non-2xx, ok=false, connection error) ROLLS BACK the swap
(`.old` promoted back to canonical, unverified compacted DB discarded) and the
vacuum exits 2 so the nightly unit goes red — silence must be impossible. The
cost (a good compaction discarded when core boots slowly) is bounded by the
180s boot wait + 30s readiness wait; correctness beats compaction.
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


class TestCheckInvariantsRollsBack:
    """P0 #37 item 3: post-restart check_invariants failure = rollback + exit 2.

    Every non-verification outcome (404 while core boots, 503, connection
    error, 200+ok=false) must (a) promote `.old` back to canonical so the
    path and the live store re-converge, and (b) exit 2 so systemd reports
    the nightly as failed instead of the 07-09 silent 'complete'.
    """

    def _run_with_ci(  # noqa: C901 - cohesive: single helper drives all CI variants
        self,
        monkeypatch,
        ci_status: int | None = None,
        ci_raises: Exception | None = None,
        ci_ok: bool | None = None,
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
        assert result == 2, (
            f"an unverified vacuum must exit 2 (rolled back, nightly goes red); got {result}"
        )
        assert self._canonical_is_original, (
            "rollback must promote .old (the original DB) back to the canonical path"
        )
        assert not self._compacted_retained, "the unverified compacted DB must be discarded"
        assert self._olds == [], (
            f"no half-swapped .old may remain after finalize (07-09 guard); got {self._olds}"
        )

    def test_check_invariants_404_rolls_back_exit_2(self, monkeypatch):
        """The 07-09 incident branch: 404 used to warn+retain — now rollback."""
        result, _ = self._run_with_ci(monkeypatch, ci_status=404)
        self._assert_rolled_back(result)

    def test_check_invariants_non2xx_rolls_back_exit_2(self, monkeypatch):
        result, _ = self._run_with_ci(monkeypatch, ci_status=503)
        self._assert_rolled_back(result)

    def test_check_invariants_connection_error_rolls_back_exit_2(self, monkeypatch):
        result, _ = self._run_with_ci(monkeypatch, ci_raises=httpx.ConnectError("refused"))
        self._assert_rolled_back(result)

    def test_check_invariants_ok_false_rolls_back_exit_2(self, monkeypatch):
        result, _ = self._run_with_ci(monkeypatch, ci_status=200, ci_ok=False)
        self._assert_rolled_back(result)

    def test_check_invariants_ok_true_exits_0_and_retires_old(self, monkeypatch):
        result, _ = self._run_with_ci(monkeypatch, ci_status=200, ci_ok=True)
        assert result == 0
        assert self._olds == [], ".old must be retired on a verified vacuum"
        assert self._compacted_retained, "the verified compacted DB stays canonical"

    def test_rollback_is_loud(self, monkeypatch, capsys):
        self._run_with_ci(monkeypatch, ci_status=404)
        err = capsys.readouterr().err
        assert "CRITICAL" in err and "ROLLING BACK" in err, (
            f"rollback must be loud (07-09 was silent); stderr={err!r}"
        )
