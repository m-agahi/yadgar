"""v5.7.0 PR-3 — 30-second readiness wait before check_invariants.

After PR-2's warn-only on 404, PR-3 adds a second _wait_for_yadgar_health
call (timeout_s=30.0) immediately before the check_invariants POST.  Goal:
give core time to finish registering API routes after /health already reports
200 from process-boot, so check_invariants actually gets a 2xx + report
instead of a transient 404 / connection-refused.

PR-2's warn-only handling stays in place as the fallback if the 30s window
is still not enough.
"""

from __future__ import annotations

import tempfile
import types as _types
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

# ---------------------------------------------------------------------------
# Shared scaffolding (mirrors test_vacuum_exit_code.py)
# ---------------------------------------------------------------------------


def _fake_db(td: str) -> Path:
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


def _fake_post_ok(url: str, **kwargs) -> MagicMock:
    m = MagicMock()
    m.status_code = 200
    m.text = "OK"
    m.json.return_value = {"ok": True}
    return m


def _make_side_db(backend_url, filtered_path, side_path, source_counts):
    """Hermetic stand-in for the P2 side-build (no surreal subprocess).

    Creates the side path so the REAL _atomic_swap renames it in; returns True.
    The live side-build is covered by the e2e suite.
    """
    side_path.mkdir(parents=True, exist_ok=True)
    (side_path / "compacted.marker").write_bytes(b"compacted")
    return True


def _patch_p2_side_build(stack: ExitStack) -> None:
    """Patch the two surreal-touching P2 side-build seams into an ExitStack."""
    stack.enter_context(patch("yadgar.vacuum._capture_table_counts", return_value={"memory": 1}))
    stack.enter_context(patch("yadgar.vacuum._build_and_verify_side_db", side_effect=_make_side_db))


# ---------------------------------------------------------------------------
# TestVacuumReadinessWait
# ---------------------------------------------------------------------------


class TestVacuumReadinessWait:
    """PR-3: _wait_for_yadgar_health(timeout_s=30.0) called before check_invariants.

    We patch _wait_for_yadgar_health at module level so tests do not sleep.
    The existing 180s boot-wait call (first) and the new 30s API-layer call
    (second) are both intercepted.
    """

    def _run(
        self,
        monkeypatch,
        health_side_effects,  # list[bool]: return values per call in order
        ci_status: int = 200,
        ci_raises: Exception | None = None,
    ) -> tuple[int, MagicMock]:
        """Run cmd_vacuum_impl, return (exit_code, wait_mock)."""

        def fake_post(url: str, **kwargs) -> MagicMock:
            if "/api/check_invariants" in url:
                if ci_raises is not None:
                    raise ci_raises
                m = MagicMock()
                m.status_code = ci_status
                m.text = f"HTTP {ci_status}"
                m.json.return_value = {"ok": ci_status == 200}
                return m
            return _fake_post_ok(url, **kwargs)

        monkeypatch.setattr(httpx, "get", _fake_get)
        monkeypatch.setattr(httpx, "post", fake_post)
        monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", "test-token")

        from yadgar.vacuum import cmd_vacuum_impl

        wait_mock = MagicMock(side_effect=health_side_effects)

        with tempfile.TemporaryDirectory() as td:
            db = _fake_db(td)
            monkeypatch.setenv("YADGAR_HOME", td)
            script = Path(td) / "cleanup-backups.sh"
            script.write_text("#!/bin/sh\nexit 0\n")
            script.chmod(0o755)
            monkeypatch.setenv("YADGAR_CLEANUP_SCRIPT", str(script))

            with ExitStack() as stack:
                stack.enter_context(patch("yadgar.vacuum._log_consolidation_row"))
                stack.enter_context(patch("yadgar.vacuum.ServiceController"))
                stack.enter_context(patch("yadgar.vacuum._wait_for_health", return_value=True))
                stack.enter_context(patch("yadgar.vacuum._wait_for_yadgar_health", wait_mock))
                stack.enter_context(patch("yadgar.vacuum._redefine_users_post_import"))
                _patch_p2_side_build(stack)
                result = cmd_vacuum_impl(_vacuum_args(db))

        return result, wait_mock

    # ------------------------------------------------------------------
    # Test 1: happy path — both health waits succeed
    # ------------------------------------------------------------------

    def test_readiness_wait_called_before_check_invariants_happy_path(self, monkeypatch):
        """PR-3: _wait_for_yadgar_health called twice — boot (180s) then readiness (30s)."""
        # Two calls expected: first=boot wait (180s), second=readiness wait (30s)
        result, wait_mock = self._run(
            monkeypatch,
            health_side_effects=[True, True],  # both succeed
            ci_status=200,
        )
        assert result == 0
        assert wait_mock.call_count == 2, (
            f"Expected _wait_for_yadgar_health called 2 times (boot + readiness); "
            f"got {wait_mock.call_count}. PR-3 adds a second call before check_invariants."
        )
        # Second call must use timeout_s=30.0
        second_call_kwargs = wait_mock.call_args_list[1]
        kwargs = second_call_kwargs.kwargs if second_call_kwargs.kwargs else {}
        args = second_call_kwargs.args if second_call_kwargs.args else ()
        timeout_val = kwargs.get("timeout_s", args[1] if len(args) > 1 else None)
        assert timeout_val == 30.0, (
            f"Second _wait_for_yadgar_health call must use timeout_s=30.0; got {timeout_val!r}."
        )

    # ------------------------------------------------------------------
    # Test 2: readiness wait fails (never returns 200 in 30s) — must WARN + continue
    # ------------------------------------------------------------------

    def test_readiness_timeout_logs_warn_and_continues(self, monkeypatch, capsys):
        """PR-3: when 30s readiness wait times out, log WARN and proceed to check_invariants.

        PR-2's warn-only handling is the fallback; vacuum must not exit 2.
        """
        # First (boot) wait succeeds; second (readiness) times out
        result, wait_mock = self._run(
            monkeypatch,
            health_side_effects=[True, False],  # boot ok, readiness timeout
            ci_status=200,  # CI succeeds anyway (core did eventually come up)
        )
        assert result == 0, (
            f"Readiness timeout must not fail vacuum; got exit {result}. "
            "PR-2 warn-only is the safety net."
        )
        captured = capsys.readouterr()
        warn_text = captured.err.lower() + captured.out.lower()
        assert "warning" in warn_text or "warn" in warn_text, (
            f"Expected a warning when readiness wait times out; stderr={captured.err!r}"
        )
        assert "30s" in captured.err or "30" in captured.err, (
            f"Warning should mention 30s timeout; stderr={captured.err!r}"
        )

    # ------------------------------------------------------------------
    # Test 3: readiness timeout + check_invariants also fails — still exit 0 (PR-2 applies)
    # ------------------------------------------------------------------

    def test_readiness_timeout_plus_ci_404_still_exits_0(self, monkeypatch):
        """PR-3+PR-2: both readiness timeout AND check_invariants 404 → exit 0."""
        result, _ = self._run(
            monkeypatch,
            health_side_effects=[True, False],
            ci_status=404,
        )
        assert result == 0, (
            f"Readiness timeout + CI 404 must not fail vacuum; got exit {result}. "
            "PR-2's warn-only on CI errors must still apply."
        )

    # ------------------------------------------------------------------
    # Test 4: call order — readiness wait happens BEFORE check_invariants POST
    # ------------------------------------------------------------------

    def test_readiness_wait_precedes_check_invariants_call(self, monkeypatch):
        """PR-3: verify call ordering — health ready before CI is posted."""
        call_log: list[str] = []

        def fake_post(url: str, **kwargs) -> MagicMock:
            if "/api/check_invariants" in url:
                call_log.append("check_invariants")
            m = MagicMock()
            m.status_code = 200
            m.text = "OK"
            m.json.return_value = {"ok": True}
            return m

        monkeypatch.setattr(httpx, "get", _fake_get)
        monkeypatch.setattr(httpx, "post", fake_post)
        monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", "test-token")

        from yadgar.vacuum import cmd_vacuum_impl

        def health_side_effect(*args, **kwargs):
            call_log.append(
                f"health_wait(timeout={kwargs.get('timeout_s', args[1] if len(args) > 1 else '?')})"
            )
            return True

        with tempfile.TemporaryDirectory() as td:
            db = _fake_db(td)
            monkeypatch.setenv("YADGAR_HOME", td)
            script = Path(td) / "cleanup-backups.sh"
            script.write_text("#!/bin/sh\nexit 0\n")
            script.chmod(0o755)
            monkeypatch.setenv("YADGAR_CLEANUP_SCRIPT", str(script))

            with ExitStack() as stack:
                stack.enter_context(patch("yadgar.vacuum._log_consolidation_row"))
                stack.enter_context(patch("yadgar.vacuum.ServiceController"))
                stack.enter_context(patch("yadgar.vacuum._wait_for_health", return_value=True))
                stack.enter_context(
                    patch("yadgar.vacuum._wait_for_yadgar_health", side_effect=health_side_effect)
                )
                stack.enter_context(patch("yadgar.vacuum._redefine_users_post_import"))
                _patch_p2_side_build(stack)
                cmd_vacuum_impl(_vacuum_args(db))

        # call_log should contain: [..., "health_wait(timeout=30.0)", "check_invariants"]
        ci_idx = next((i for i, e in enumerate(call_log) if e == "check_invariants"), None)
        readiness_idx = next((i for i, e in enumerate(call_log) if "30.0" in e), None)
        assert readiness_idx is not None, (
            f"No health_wait(timeout=30.0) call found; call_log={call_log}. "
            "PR-3 must add _wait_for_yadgar_health(timeout_s=30.0) before check_invariants."
        )
        assert ci_idx is not None, f"check_invariants not called; call_log={call_log}"
        assert readiness_idx < ci_idx, (
            f"Readiness wait (idx={readiness_idx}) must precede check_invariants (idx={ci_idx}); "
            f"call_log={call_log}"
        )
