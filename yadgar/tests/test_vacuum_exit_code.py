"""v5.7.0 PR-2 — check_invariants 404/non-2xx/exception must be warn-only.

Real incident 2026-05-23: DB shrunk 962 MB → 79.5 MB (91% reclaim, 21s,
18s daemon downtime) — vacuum fully succeeded. Post-restart check_invariants
returned 404 because yadgar core hadn't finished booting yet. Script exited 2;
systemd reported failure on a successful run.

Fix: non-2xx response OR connection exception → WARN + exit 0.
PR-3 (independent) will add a 30-second readiness wait so the 404 stops
occurring in practice.
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


def _patch_stack(stack: ExitStack, monkeypatch) -> None:
    """Apply the standard vacuum mock patches via an ExitStack."""
    stack.enter_context(patch("yadgar.vacuum._log_consolidation_row"))
    stack.enter_context(patch("yadgar.vacuum.ServiceController"))
    stack.enter_context(patch("yadgar.vacuum._wait_for_health", return_value=True))
    stack.enter_context(patch("yadgar.vacuum._wait_for_yadgar_health", return_value=True))
    stack.enter_context(patch("yadgar.vacuum._redefine_users_post_import"))


# ---------------------------------------------------------------------------
# TestCheckInvariantsWarnOnly
# ---------------------------------------------------------------------------


class TestCheckInvariantsWarnOnly:
    """v5.7.0 PR-2: post-restart check_invariants must not poison exit code.

    Real incident 2026-05-23: DB shrunk 962 MB → 79.5 MB (91% reclaim, 21s).
    Vacuum fully succeeded but check_invariants returned 404 because yadgar
    core hadn't finished booting.  Script exited 2; systemd reported failure.

    Fix: 404 / any non-2xx / connection error → WARN + continue (exit 0).
    PR-3 will add a 30s readiness wait that makes the 404 stop happening.
    """

    def _run_with_ci(  # noqa: C901 - cohesive: single helper drives all CI variants
        self,
        monkeypatch,
        ci_status: int | None = None,
        ci_raises: Exception | None = None,
    ) -> int:
        """Drive cmd_vacuum_impl end-to-end; mock check_invariants per args."""

        def fake_post(url: str, **kwargs) -> MagicMock:
            if "/api/check_invariants" in url:
                if ci_raises is not None:
                    raise ci_raises
                m = MagicMock()
                m.status_code = ci_status
                m.text = f"HTTP {ci_status}"
                m.json.return_value = {"ok": ci_status == 200}
                return m
            m = MagicMock()
            m.status_code = 200
            m.text = "OK"
            return m

        monkeypatch.setattr(httpx, "get", _fake_get)
        monkeypatch.setattr(httpx, "post", fake_post)
        monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", "test-token")

        from yadgar.vacuum import cmd_vacuum_impl

        with tempfile.TemporaryDirectory() as td:
            db = _fake_db(td)
            monkeypatch.setenv("YADGAR_HOME", td)
            script = Path(td) / "cleanup-backups.sh"
            script.write_text("#!/bin/sh\nexit 0\n")
            script.chmod(0o755)
            monkeypatch.setenv("YADGAR_CLEANUP_SCRIPT", str(script))

            with ExitStack() as stack:
                _patch_stack(stack, monkeypatch)
                return cmd_vacuum_impl(_vacuum_args(db))

    def test_check_invariants_404_exits_0(self, monkeypatch):
        """PR-2: check_invariants 404 (core not ready post-restart) → exit 0.

        2026-05-23 incident root cause: this case exited 2, wasting 91% reclaim.
        """
        result = self._run_with_ci(monkeypatch, ci_status=404)
        assert result == 0, (
            f"check_invariants 404 must not fail vacuum (exit 0); got exit {result}. "
            "Core may not be fully ready post-restart — PR-3 will add readiness wait."
        )

    def test_check_invariants_non2xx_exits_0(self, monkeypatch):
        """PR-2: check_invariants 503 (or any non-2xx) → warn + exit 0."""
        result = self._run_with_ci(monkeypatch, ci_status=503)
        assert result == 0, (
            f"check_invariants 503 must not fail vacuum (exit 0); got exit {result}."
        )

    def test_check_invariants_connection_error_exits_0(self, monkeypatch):
        """PR-2: check_invariants connection-refused / timeout → warn + exit 0."""
        result = self._run_with_ci(monkeypatch, ci_raises=httpx.ConnectError("Connection refused"))
        assert result == 0, (
            f"check_invariants ConnectError must not fail vacuum (exit 0); got exit {result}."
        )

    def test_check_invariants_warn_printed_on_404(self, monkeypatch, capsys):
        """PR-2: a WARN message is printed to stderr on 404."""
        self._run_with_ci(monkeypatch, ci_status=404)
        captured = capsys.readouterr()
        assert "WARNING" in captured.err or "warn" in captured.err.lower(), (
            f"Expected a warning in stderr on 404; got: {captured.err!r}"
        )

    def test_check_invariants_ok_false_exits_0(self, monkeypatch):
        """PR-2: 200 but ok=false → warn + exit 0 (invariant violation is informational)."""

        def fake_post_violation(url: str, **kwargs) -> MagicMock:
            if "/api/check_invariants" in url:
                m = MagicMock()
                m.status_code = 200
                m.json.return_value = {"ok": False, "violations": ["I1: test violation"]}
                m.text = '{"ok": false}'
                return m
            m = MagicMock()
            m.status_code = 200
            m.text = "OK"
            return m

        monkeypatch.setattr(httpx, "get", _fake_get)
        monkeypatch.setattr(httpx, "post", fake_post_violation)
        monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", "test-token")

        from yadgar.vacuum import cmd_vacuum_impl

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

        assert result == 0, (
            f"check_invariants 200+ok=false must not fail vacuum (exit 0); got exit {result}. "
            "Invariant violations are informational; separate PR will harden this."
        )
