"""Tests for nightly_cycle maintenance-mode integration (TDD — v5.50.3).

Verifies that _step_stop_core / _step_start_core call HTTP maintenance
endpoints instead of systemctl stop/start for the core unit.

Coverage:
  1. _step_stop_core calls _maintenance_http("enter", ...) — NOT _run_systemctl stop yadgar.
  2. _step_start_core calls _maintenance_http("exit", ...) — NOT _run_systemctl start yadgar.
  3. maintenance exit runs in finally even when a mid-cycle step raises.
  4. _step_stop_core returns 10 (FATAL) when maintenance enter HTTP call fails.
  5. _step_start_core returns 70 when maintenance exit HTTP call fails.
  6. Backend systemctl calls (e.g. _start_service for backend in vacuum step)
     are NOT affected — only CORE stop/start changes.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_MODULE = "yadgar.core.scripts.nightly_cycle"


# ---------------------------------------------------------------------------
# Helpers (mirrors test_nightly_cycle.py pattern)
# ---------------------------------------------------------------------------


def _make_args(**kwargs):
    defaults = {
        "db_path": "/fake/surreal_db",
        "backend_url": "http://127.0.0.1:8080",
        "service_mode": "systemd",
        "retention": 3,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _import_module():
    import yadgar.core.scripts.nightly_cycle as _mod

    importlib.reload(_mod)
    return _mod


def _run_with_maintenance_mocks(
    tmp_path: Path,
    *,
    maint_enter_side_effect=None,
    maint_exit_side_effect=None,
    consolidation_side_effect=None,
    vacuum_return=0,
    extra_patches=None,
):
    """Run main() with _maintenance_http mocked. Returns (exit_code, module, mocks)."""
    mod = _import_module()

    db_dir = tmp_path / "surreal_db"
    db_dir.mkdir(exist_ok=True)
    args = _make_args(db_path=str(db_dir))

    mock_enter = MagicMock(return_value=None)
    if maint_enter_side_effect is not None:
        mock_enter.side_effect = maint_enter_side_effect

    mock_exit = MagicMock(return_value=None)
    if maint_exit_side_effect is not None:
        mock_exit.side_effect = maint_exit_side_effect

    mock_ctl = MagicMock(return_value=None)  # _run_systemctl — must NOT be called for core
    mock_sched = MagicMock()
    mock_sched.run_nightly_consolidation.return_value = {"merged": 0}
    if consolidation_side_effect is not None:
        mock_sched.run_nightly_consolidation.side_effect = consolidation_side_effect

    snap_mock = MagicMock(return_value=tmp_path / "snap")
    mock_prune = MagicMock(return_value=[])
    mock_vac = MagicMock(return_value=vacuum_return)

    base = dict(
        _run_systemctl=mock_ctl,
        _maintenance_http=MagicMock(
            side_effect=lambda action, url, **_kw: (
                mock_enter(action, url) if action == "enter" else mock_exit(action, url)
            )
        ),
        create_snapshot=snap_mock,
        prune_snapshots=mock_prune,
        cmd_vacuum_impl=mock_vac,
        StorageEngine=MagicMock(return_value=MagicMock()),
        # R3 Car 1 D3: nightly no longer constructs ConsolidationScheduler /
        # EmbeddingEngine — patch run_nightly_consolidation instead.
        run_nightly_consolidation=mock_sched.run_nightly_consolidation,
        Settings=MagicMock(return_value=SimpleNamespace(DB_PATH=str(db_dir))),
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
            "ctl": mock_ctl,
            "enter": mock_enter,
            "exit": mock_exit,
            "snap": snap_mock,
            "prune": mock_prune,
            "vac": mock_vac,
            "sched": mock_sched,
        },
    )


# ---------------------------------------------------------------------------
# 1 — _step_stop_core calls maintenance enter, NOT systemctl stop yadgar
# ---------------------------------------------------------------------------


class TestStepStopCoreUsesMaintenance:
    def test_stop_core_calls_maintenance_enter_not_systemctl(self, tmp_path):
        """_step_stop_core must call _maintenance_http('enter', ...) not _run_systemctl stop."""
        mod = _import_module()

        maintenance_calls = []
        systemctl_calls = []

        def _fake_maintenance_http(action, url, **_kw):
            maintenance_calls.append((action, url))

        def _fake_ctl(action, unit):
            systemctl_calls.append((action, unit))

        db_dir = tmp_path / "surreal_db"
        db_dir.mkdir()
        args = _make_args(db_path=str(db_dir))

        with patch.multiple(
            _MODULE,
            _run_systemctl=_fake_ctl,
            _maintenance_http=_fake_maintenance_http,
            create_snapshot=MagicMock(return_value=tmp_path / "snap"),
            prune_snapshots=MagicMock(return_value=[]),
            cmd_vacuum_impl=MagicMock(return_value=0),
            StorageEngine=MagicMock(return_value=MagicMock()),
            run_nightly_consolidation=MagicMock(return_value={"merged": 0}),
            Settings=MagicMock(return_value=SimpleNamespace(DB_PATH=str(db_dir))),
            configure_logging=MagicMock(),
            default_retention=MagicMock(return_value=3),
        ):
            mod.main(args)

        # maintenance enter must have been called
        enter_calls = [c for c in maintenance_calls if c[0] == "enter"]
        assert enter_calls, (
            f"_maintenance_http('enter', ...) must be called. Calls: {maintenance_calls}"
        )

        # systemctl stop yadgar (core) must NOT be called
        core_stop_calls = [c for c in systemctl_calls if c == ("stop", "yadgar")]
        assert not core_stop_calls, (
            f"_run_systemctl('stop', 'yadgar') must NOT be called; got: {systemctl_calls}"
        )

    def test_stop_core_returns_zero_on_success(self, tmp_path):
        """_step_stop_core returns 0 when maintenance enter succeeds."""
        mod = _import_module()

        db_dir = tmp_path / "surreal_db"
        db_dir.mkdir()

        with patch.multiple(
            _MODULE,
            _run_systemctl=MagicMock(),
            _maintenance_http=MagicMock(return_value=None),
            create_snapshot=MagicMock(return_value=tmp_path / "snap"),
            prune_snapshots=MagicMock(return_value=[]),
            cmd_vacuum_impl=MagicMock(return_value=0),
            StorageEngine=MagicMock(return_value=MagicMock()),
            run_nightly_consolidation=MagicMock(return_value={"merged": 0}),
            Settings=MagicMock(return_value=SimpleNamespace(DB_PATH=str(db_dir))),
            configure_logging=MagicMock(),
            default_retention=MagicMock(return_value=3),
        ):
            result = mod._step_stop_core()

        assert result == 0, f"Expected 0 on success, got {result}"

    def test_stop_core_nonfatal_when_enter_fails(self, tmp_path):
        """_step_stop_core returns 0 (BEST-EFFORT) when _maintenance_http enter raises.

        v5.72 (#62): entering maintenance is best-effort — a down/old core (e.g.
        404, no /maintenance route) must NOT abort the nightly; it has no external
        writers to gate. The nightly proceeds with a warning.
        """
        mod = _import_module()

        db_dir = tmp_path / "surreal_db"
        db_dir.mkdir()

        def _failing_enter(action, url):
            if action == "enter":
                raise ConnectionError("core unreachable")

        with patch.multiple(
            _MODULE,
            _run_systemctl=MagicMock(),
            _maintenance_http=_failing_enter,
            create_snapshot=MagicMock(return_value=tmp_path / "snap"),
            prune_snapshots=MagicMock(return_value=[]),
            cmd_vacuum_impl=MagicMock(return_value=0),
            StorageEngine=MagicMock(return_value=MagicMock()),
            run_nightly_consolidation=MagicMock(return_value={"merged": 0}),
            Settings=MagicMock(return_value=SimpleNamespace(DB_PATH=str(db_dir))),
            configure_logging=MagicMock(),
            default_retention=MagicMock(return_value=3),
        ):
            result = mod._step_stop_core()

        assert result == 0, f"Expected 0 (best-effort, non-fatal) when enter fails, got {result}"


# ---------------------------------------------------------------------------
# 2 — _step_start_core calls maintenance exit, NOT systemctl start yadgar
# ---------------------------------------------------------------------------


class TestStepStartCoreUsesMaintenance:
    def test_start_core_calls_maintenance_exit_not_systemctl(self, tmp_path):
        """_step_start_core must call _maintenance_http('exit', ...) not _run_systemctl start."""
        mod = _import_module()

        maintenance_calls = []
        systemctl_calls = []

        def _fake_maintenance_http(action, url, **_kw):
            maintenance_calls.append((action, url))

        def _fake_ctl(action, unit):
            systemctl_calls.append((action, unit))

        db_dir = tmp_path / "surreal_db"
        db_dir.mkdir()
        args = _make_args(db_path=str(db_dir))

        with patch.multiple(
            _MODULE,
            _run_systemctl=_fake_ctl,
            _maintenance_http=_fake_maintenance_http,
            create_snapshot=MagicMock(return_value=tmp_path / "snap"),
            prune_snapshots=MagicMock(return_value=[]),
            cmd_vacuum_impl=MagicMock(return_value=0),
            StorageEngine=MagicMock(return_value=MagicMock()),
            run_nightly_consolidation=MagicMock(return_value={"merged": 0}),
            Settings=MagicMock(return_value=SimpleNamespace(DB_PATH=str(db_dir))),
            configure_logging=MagicMock(),
            default_retention=MagicMock(return_value=3),
        ):
            mod.main(args)

        exit_calls = [c for c in maintenance_calls if c[0] == "exit"]
        assert exit_calls, (
            f"_maintenance_http('exit', ...) must be called. Calls: {maintenance_calls}"
        )

        core_start_calls = [c for c in systemctl_calls if c == ("start", "yadgar")]
        assert not core_start_calls, (
            f"_run_systemctl('start', 'yadgar') must NOT be called; got: {systemctl_calls}"
        )

    def test_start_core_returns_zero_on_success(self, tmp_path):
        """_step_start_core returns 0 when maintenance exit succeeds."""
        mod = _import_module()

        db_dir = tmp_path / "surreal_db"
        db_dir.mkdir()

        with patch.multiple(
            _MODULE,
            _run_systemctl=MagicMock(),
            _maintenance_http=MagicMock(return_value=None),
            create_snapshot=MagicMock(return_value=tmp_path / "snap"),
            prune_snapshots=MagicMock(return_value=[]),
            cmd_vacuum_impl=MagicMock(return_value=0),
            StorageEngine=MagicMock(return_value=MagicMock()),
            run_nightly_consolidation=MagicMock(return_value={"merged": 0}),
            Settings=MagicMock(return_value=SimpleNamespace(DB_PATH=str(db_dir))),
            configure_logging=MagicMock(),
            default_retention=MagicMock(return_value=3),
        ):
            result = mod._step_start_core()

        assert result == 0, f"Expected 0 on success, got {result}"

    def test_start_core_nonfatal_when_exit_fails(self, tmp_path):
        """_step_start_core returns 0 (BEST-EFFORT) when _maintenance_http exit raises.

        v5.72 (#62): exiting maintenance is best-effort — a failed exit must not
        mask the real cycle outcome, and if maintenance was never entered (core
        down/old) there is nothing to clear. Warns and returns 0.
        """
        mod = _import_module()

        db_dir = tmp_path / "surreal_db"
        db_dir.mkdir()

        def _failing_exit(action, url):
            if action == "exit":
                raise ConnectionError("core unreachable after cycle")

        with patch.multiple(
            _MODULE,
            _run_systemctl=MagicMock(),
            _maintenance_http=_failing_exit,
            create_snapshot=MagicMock(return_value=tmp_path / "snap"),
            prune_snapshots=MagicMock(return_value=[]),
            cmd_vacuum_impl=MagicMock(return_value=0),
            StorageEngine=MagicMock(return_value=MagicMock()),
            run_nightly_consolidation=MagicMock(return_value={"merged": 0}),
            Settings=MagicMock(return_value=SimpleNamespace(DB_PATH=str(db_dir))),
            configure_logging=MagicMock(),
            default_retention=MagicMock(return_value=3),
        ):
            result = mod._step_start_core()

        assert result == 0, f"Expected 0 (best-effort, non-fatal) when exit fails, got {result}"


# ---------------------------------------------------------------------------
# 3 — Maintenance exit runs in finally (even when mid-cycle step raises)
# ---------------------------------------------------------------------------


class TestMaintenanceExitInFinally:
    def test_exit_called_even_when_consolidation_raises(self, tmp_path):
        """maintenance exit must be called even if consolidation raises unexpectedly."""
        mod = _import_module()

        maintenance_calls = []

        def _fake_maintenance_http(action, url, **_kw):
            maintenance_calls.append(action)

        def _failing_consolidation(db_path, settings):
            raise RuntimeError("consolidation exploded — unexpected exception")

        db_dir = tmp_path / "surreal_db"
        db_dir.mkdir()
        args = _make_args(db_path=str(db_dir))

        with patch.multiple(
            _MODULE,
            _run_systemctl=MagicMock(),
            _maintenance_http=_fake_maintenance_http,
            _step_consolidation=_failing_consolidation,
            create_snapshot=MagicMock(return_value=tmp_path / "snap"),
            prune_snapshots=MagicMock(return_value=[]),
            cmd_vacuum_impl=MagicMock(return_value=0),
            StorageEngine=MagicMock(return_value=MagicMock()),
            run_nightly_consolidation=MagicMock(return_value={"merged": 0}),
            Settings=MagicMock(return_value=SimpleNamespace(DB_PATH=str(db_dir))),
            configure_logging=MagicMock(),
            default_retention=MagicMock(return_value=3),
        ):
            # main() must not propagate the exception; it catches it inside
            # _step_consolidation (returns 30 there).  But we're patching
            # _step_consolidation directly so it raises unconditionally.
            # main() must still reach _step_start_core → maintenance exit.
            try:
                mod.main(args)
            # `_step_consolidation` is patched to raise unconditionally; whether
            # main() swallows it is the thing under test, so both outcomes must
            # reach the assertion below.
            except Exception:  # noqa: BLE001 — a patched step raises unconditionally
                pass  # if main() propagates, we still check below

        assert "exit" in maintenance_calls, (
            f"maintenance 'exit' must be called even when consolidation raises. "
            f"Got calls: {maintenance_calls}"
        )

    def test_exit_called_even_when_pre_backup_fails(self, tmp_path):
        """maintenance exit must be called even when pre-backup fails (step 20, FATAL).

        With the new design, enter is called first; if pre-backup then fails,
        exit must still fire via finally to un-wedge maintenance mode.
        """
        mod = _import_module()

        maintenance_calls = []

        def _fake_maintenance_http(action, url, **_kw):
            maintenance_calls.append(action)

        db_dir = tmp_path / "surreal_db"
        db_dir.mkdir()
        args = _make_args(db_path=str(db_dir))

        with patch.multiple(
            _MODULE,
            _run_systemctl=MagicMock(),
            _maintenance_http=_fake_maintenance_http,
            create_snapshot=MagicMock(side_effect=RuntimeError("disk full")),
            prune_snapshots=MagicMock(return_value=[]),
            cmd_vacuum_impl=MagicMock(return_value=0),
            StorageEngine=MagicMock(return_value=MagicMock()),
            run_nightly_consolidation=MagicMock(return_value={"merged": 0}),
            Settings=MagicMock(return_value=SimpleNamespace(DB_PATH=str(db_dir))),
            configure_logging=MagicMock(),
            default_retention=MagicMock(return_value=3),
        ):
            code = mod.main(args)

        assert code == 20, f"Expected 20 (pre-backup fail), got {code}"
        assert "enter" in maintenance_calls, "maintenance enter must be called"
        assert "exit" in maintenance_calls, (
            f"maintenance exit must be called even after pre-backup FATAL failure. "
            f"Calls: {maintenance_calls}"
        )

    def test_exit_called_even_when_vacuum_fails(self, tmp_path):
        """maintenance exit runs even when vacuum step fails (non-fatal, returns 40)."""
        mod = _import_module()

        maintenance_calls = []

        def _fake_maintenance_http(action, url, **_kw):
            maintenance_calls.append(action)

        db_dir = tmp_path / "surreal_db"
        db_dir.mkdir()
        args = _make_args(db_path=str(db_dir))

        with patch.multiple(
            _MODULE,
            _run_systemctl=MagicMock(),
            _maintenance_http=_fake_maintenance_http,
            create_snapshot=MagicMock(return_value=tmp_path / "snap"),
            prune_snapshots=MagicMock(return_value=[]),
            cmd_vacuum_impl=MagicMock(return_value=1),  # non-zero = vacuum fail
            StorageEngine=MagicMock(return_value=MagicMock()),
            run_nightly_consolidation=MagicMock(return_value={"merged": 0}),
            Settings=MagicMock(return_value=SimpleNamespace(DB_PATH=str(db_dir))),
            configure_logging=MagicMock(),
            default_retention=MagicMock(return_value=3),
        ):
            code = mod.main(args)

        assert code == 40, f"Expected 40 (vacuum fail), got {code}"
        assert "exit" in maintenance_calls, (
            f"maintenance exit must be called after vacuum failure. Calls: {maintenance_calls}"
        )


# ---------------------------------------------------------------------------
# 6 — Backend systemctl calls NOT affected
# ---------------------------------------------------------------------------


class TestBackendSystemctlUnchanged:
    def test_backend_start_still_uses_systemctl(self, tmp_path):
        """_run_systemctl is still called for backend (step 4 vacuum safety start).

        Only core stop/start changes — backend operations are untouched.
        """
        mod = _import_module()

        maintenance_calls = []
        systemctl_calls = []

        def _fake_maintenance_http(action, url, **_kw):
            maintenance_calls.append((action, url))

        def _fake_ctl(action, unit):
            systemctl_calls.append((action, unit))

        db_dir = tmp_path / "surreal_db"
        db_dir.mkdir()
        args = _make_args(db_path=str(db_dir))

        with patch.multiple(
            _MODULE,
            _run_systemctl=_fake_ctl,
            _maintenance_http=_fake_maintenance_http,
            create_snapshot=MagicMock(return_value=tmp_path / "snap"),
            prune_snapshots=MagicMock(return_value=[]),
            cmd_vacuum_impl=MagicMock(return_value=0),
            StorageEngine=MagicMock(return_value=MagicMock()),
            run_nightly_consolidation=MagicMock(return_value={"merged": 0}),
            Settings=MagicMock(return_value=SimpleNamespace(DB_PATH=str(db_dir))),
            configure_logging=MagicMock(),
            default_retention=MagicMock(return_value=3),
        ):
            mod.main(args)

        # Backend start (step 4 vacuum safety no-op) must use _run_systemctl, not maintenance
        backend_starts = [c for c in systemctl_calls if c == ("start", "yadgar-backend")]
        assert backend_starts, (
            f"_run_systemctl('start', 'yadgar-backend') must still be called. "
            f"Got systemctl calls: {systemctl_calls}"
        )

        # Core must NOT be in systemctl calls
        core_via_ctl = [c for c in systemctl_calls if c[1] == "yadgar"]
        assert not core_via_ctl, f"Core must NOT go through _run_systemctl. Got: {core_via_ctl}"


# ---------------------------------------------------------------------------
# 7 — task:0113: the step-4 vacuum must NOT un-wedge the nightly's own gate
# ---------------------------------------------------------------------------


def _maintenance_request(body: dict | None):
    """Minimal stand-in for a starlette Request with an async .json()."""

    async def _json():
        if body is None:
            raise ValueError("no body")
        return body

    return SimpleNamespace(json=_json)


def _call_maintenance(action: str, body: dict | None = None):
    """Invoke the REAL control-route handler; return (status, decoded JSON body)."""
    import asyncio
    import json as _json

    from yadgar.core.server.routes.control import (
        maintenance_enter_handler,
        maintenance_exit_handler,
    )

    handler = maintenance_enter_handler if action == "enter" else maintenance_exit_handler
    resp = asyncio.run(handler(_maintenance_request(body)))
    return resp.status_code, _json.loads(bytes(resp.body))


class TestNightlyVacuumNesting:
    """task:0113 — the vacuum now engages the same flag the nightly holds.

    nightly_cycle enters at step 1 and exits at step 7, AFTER step 5 (post-backup
    snapshot) and step 6 (prune).  The naive implementation of the vacuum
    write-gate exits unconditionally at the end of step 4 and un-gates the engine
    while the nightly still has DB work to do.  This test runs the REAL
    ``cmd_vacuum_impl`` against the REAL control handlers and a real in-process
    flag, so a missing previous-state guard is RED here and nowhere else.
    """

    def test_nightly_vacuum_does_not_unwedge_the_nightly_gate(self, tmp_path, monkeypatch):
        import httpx

        import yadgar._shared.runtime.state as _st
        from yadgar.core import vacuum as _v

        _st._maintenance_mode = False
        _st._maintenance_deadline = None
        mod = _import_module()

        def _routed_post(url, **kwargs):
            assert "/api/control/maintenance/" in url, f"unexpected POST during vacuum: {url}"
            action = "enter" if url.endswith("/maintenance/enter") else "exit"
            status, body = _call_maintenance(action, kwargs.get("json") or {})
            m = MagicMock()
            m.status_code = status
            m.text = ""
            m.json.return_value = body
            return m

        monkeypatch.setattr(httpx, "post", _routed_post)

        def _nightly_maintenance(action, url, **kwargs):
            _call_maintenance(action, {"ttl_seconds": kwargs.get("ttl_seconds")})

        def _vacuum(vac_args):
            with (
                patch(
                    "yadgar.core.sensitive_lock.sensitive_lock.acquire",
                    MagicMock(return_value=True),
                ),
                patch("yadgar.core.sensitive_lock.sensitive_lock.release", MagicMock()),
                patch.object(_v, "_cmd_vacuum_body", return_value=0),
            ):
                return _v.cmd_vacuum_impl(
                    SimpleNamespace(
                        backend_url="http://127.0.0.1:8080",
                        service_mode="manual",
                        db_path=str(tmp_path / "surreal_db"),
                        yes=True,
                    )
                )

        flag_at_snapshot: list[bool] = []

        def _snapshot(*_a, **_kw):
            flag_at_snapshot.append(bool(_st._maintenance_mode))
            return tmp_path / "snap"

        db_dir = tmp_path / "surreal_db"
        db_dir.mkdir(exist_ok=True)
        args = _make_args(db_path=str(db_dir))

        try:
            with patch.multiple(
                _MODULE,
                _run_systemctl=MagicMock(return_value=None),
                _maintenance_http=_nightly_maintenance,
                create_snapshot=_snapshot,
                prune_snapshots=MagicMock(return_value=[]),
                cmd_vacuum_impl=_vacuum,
                StorageEngine=MagicMock(return_value=MagicMock()),
                run_nightly_consolidation=MagicMock(return_value={"merged": 0}),
                Settings=MagicMock(return_value=SimpleNamespace(DB_PATH=str(db_dir))),
                configure_logging=MagicMock(),
                default_retention=MagicMock(return_value=3),
            ):
                mod.main(args)

            assert len(flag_at_snapshot) == 2, (
                f"expected pre-backup (step 2) + post-backup (step 5) snapshots, "
                f"got {len(flag_at_snapshot)}"
            )
            assert flag_at_snapshot[0] is True, "step 2 ran without the nightly's gate engaged"
            assert flag_at_snapshot[1] is True, (
                "step 5 (post-backup) ran with the gate OFF — the step-4 vacuum "
                "exited a maintenance window it did not open"
            )
            assert _st._maintenance_mode is False, "step 7 did not clear the flag"
        finally:
            _st._maintenance_mode = False
            _st._maintenance_deadline = None
