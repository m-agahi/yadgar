"""Vacuum finalize verification: real route, advisory CI, truthful report, live core.

Four defect classes are pinned here (task:0045 + task:0027a):

1. **Route existence.** ``vacuum/__init__.py`` POSTed ``{core}/api/check_invariants``
   for months against a route that was never registered anywhere.  Every existing
   vacuum test mocked that exact URL to 200, so the whole suite was green while the
   endpoint 404'd in production.  ``TestRouteExistenceGuard`` resolves the REAL
   route table (``mcp_server._custom_starlette_routes``, populated by the same
   import side-effects the daemon relies on) — no mock can satisfy it.
2. **Advisory check_invariants (user decision D2, vacuum-finalize path ONLY).**
   A non-ok ``check_invariants`` result must NOT roll back the swap.  It answers a
   third question (is the data model globally self-consistent) that a vacuum
   neither causes nor fixes, and it is ``ok=false`` on the production host today
   for a pre-existing reason.  The swap is still guarded — see the comment in
   ``_vacuum_finalize``.  ``check_invariants`` stays a HARD signal everywhere else
   (``consolidation/orchestrator.py`` still logs CRITICAL).
3. **Truthful report.** A rolled-back run reported the pre-rollback saving, so a
   fully reverted vacuum printed "complete … Saved: 2100 MB".  A rolled-back run
   must report ``saved_bytes == 0``.  This single assertion would have caught the
   live bug.
4. **Core restarted on every abort path (task:0027a).**  ``svc.stop()`` stops BOTH
   units; every phase-3 abort used to restart only the backend (the quiescence gate
   restarted nothing), leaving the memory engine down until a human noticed.
"""

from __future__ import annotations

import re
import tempfile
import types as _types
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

# ---------------------------------------------------------------------------
# Shared scaffolding (mirrors test_vacuum_exit_code.py)
# ---------------------------------------------------------------------------

_FAKE_SURQL = "-- TABLE DATA: memory ----\nUPSERT memory:1 CONTENT {};\n"


def _fake_db(td: str) -> Path:
    """Create a minimal fake surreal_db layout under td."""
    p = Path(td)
    db = p / "surreal_db"
    for sub in ("vlog", "sstables", "wal"):
        (db / sub).mkdir(parents=True)
    (db / "vlog" / "00001.vlog").write_bytes(b"x" * 100_000)
    return db


def _vacuum_args(db: Path) -> _types.SimpleNamespace:
    return _types.SimpleNamespace(
        backend_url="http://127.0.0.1:8080",
        service_mode="manual",
        db_path=str(db),
        yes=True,
    )


def _fake_get(url: str, **kwargs) -> MagicMock:
    m = MagicMock()
    m.status_code = 200
    m.text = _FAKE_SURQL if "/export" in url else ""
    return m


def _make_side_db(backend_url, filtered_path, side_path, source_counts):
    """Hermetic stand-in for the side-build (no surreal subprocess).

    The compacted stand-in is deliberately SMALLER than the fake canonical so a
    retained swap yields a positive saving and a rolled-back one does not.
    """
    side_path.mkdir(parents=True, exist_ok=True)
    (side_path / "compacted.marker").write_bytes(b"c")
    return True


def _patch_stack(stack: ExitStack) -> MagicMock:
    """Apply the standard vacuum mock patches; return the mocked ServiceController class."""
    stack.enter_context(patch("yadgar.core.vacuum._log_consolidation_row"))
    mock_sc = stack.enter_context(patch("yadgar.core.vacuum.ServiceController"))
    stack.enter_context(patch("yadgar.core.vacuum._wait_for_health", return_value=True))
    stack.enter_context(patch("yadgar.core.vacuum._wait_for_yadgar_health", return_value=True))
    stack.enter_context(patch("yadgar.core.vacuum._redefine_users_post_import"))
    stack.enter_context(patch("yadgar.core.vacuum._assert_backend_quiesced", return_value=True))
    stack.enter_context(
        patch("yadgar.core.vacuum._verify_live_store_coherence", return_value=(True, set()))
    )
    stack.enter_context(
        patch("yadgar.core.vacuum._capture_table_counts", return_value={"memory": 1})
    )
    stack.enter_context(
        patch("yadgar.core.vacuum._build_and_verify_side_db", side_effect=_make_side_db)
    )
    return mock_sc


def _ci_post_factory(ci_status: int | None, ci_ok: bool | None, violations=None):
    """Build an httpx.post fake that answers /api/check_invariants per the args."""

    def fake_post(url: str, **kwargs) -> MagicMock:
        m = MagicMock()
        if "/api/check_invariants" in url:
            m.status_code = ci_status
            m.text = f"HTTP {ci_status}"
            m.json.return_value = {
                "ok": ci_ok if ci_ok is not None else ci_status == 200,
                "violations": violations or [],
            }
            return m
        m.status_code = 200
        m.text = "OK"
        return m

    return fake_post


class _VacuumRun:
    """Result bundle from a driven vacuum run (tempdir state snapshotted)."""

    def __init__(self, exit_code, olds, canonical_is_original, compacted_retained, log_row, svc):
        self.exit_code = exit_code
        self.olds = olds
        self.canonical_is_original = canonical_is_original
        self.compacted_retained = compacted_retained
        self.log_row = log_row
        self.svc = svc


def _run_vacuum(monkeypatch, *, post=None, extra_patches=None) -> _VacuumRun:
    """Drive cmd_vacuum_impl end-to-end against a hermetic fake DB."""
    monkeypatch.setattr(httpx, "get", _fake_get)
    monkeypatch.setattr(httpx, "post", post or _ci_post_factory(200, True))
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
            mock_sc = _patch_stack(stack)
            for ctx in extra_patches or []:
                stack.enter_context(ctx)
            log_mock = stack.enter_context(patch("yadgar.core.vacuum._log_consolidation_row"))
            exit_code = cmd_vacuum_impl(_vacuum_args(db))
            log_row = log_mock.call_args[0][0] if log_mock.call_args else None
            svc = mock_sc.return_value

        olds = list(Path(td).glob("surreal_db.old-*"))
        canonical_is_original = (db / "vlog" / "00001.vlog").exists()
        compacted_retained = (db / "compacted.marker").exists()

    return _VacuumRun(exit_code, olds, canonical_is_original, compacted_retained, log_row, svc)


# ---------------------------------------------------------------------------
# 1. Route-existence guard — the class of bug no mock can catch
# ---------------------------------------------------------------------------

# Path fragments the vacuum POSTs at the CORE (not the backend).  Kept as a
# module-level regex so the scan cannot silently stop matching.
_CORE_URL_RE = re.compile(r'f"\{yadgar_url\}(?P<path>/[^"]*)"')


def _registered_routes() -> set[tuple[str, str]]:
    """Resolve the REAL route table the daemon serves.

    Importing ``yadgar.core.server`` runs exactly the side-effect imports the
    daemon runs (``http``, ``routes.*``, ``http_bookmarks``…), each of which
    registers its ``@mcp_server.custom_route`` handlers.  Anything absent here is
    absent in production — which is precisely how ``/api/check_invariants``
    404'd for months behind a fully green test suite.
    """
    import yadgar.core.server  # noqa: F401 — import side-effects register the routes
    from yadgar.core.server._app import mcp_server

    return {
        (route.path, method)
        for route in mcp_server._custom_starlette_routes
        for method in (route.methods or set())
    }


class TestRouteExistenceGuard:
    """No core URL the vacuum POSTs may be absent from the registered route table."""

    def test_check_invariants_route_is_registered(self) -> None:
        assert ("/api/check_invariants", "POST") in _registered_routes(), (
            "POST /api/check_invariants is not registered on the core app — the "
            "vacuum finalize verification would 404 in production (task:0045)"
        )

    def test_every_core_url_posted_by_vacuum_is_registered(self) -> None:
        """Drift catcher: scan vacuum/ for f'{yadgar_url}/…' paths, check each."""
        from yadgar.core import vacuum as _vac

        vacuum_dir = Path(_vac.__file__).parent
        found: dict[str, str] = {}
        for src_file in sorted(vacuum_dir.glob("*.py")):
            for match in _CORE_URL_RE.finditer(src_file.read_text()):
                found[match.group("path")] = src_file.name

        assert found, (
            "the core-URL scan matched nothing — the regex has drifted away from "
            f"the source in {vacuum_dir}; this guard would pass vacuously"
        )

        registered_paths = {path for path, _ in _registered_routes()}
        # /health is a custom_route too; anything else must be registered.
        missing = {p: f for p, f in found.items() if p not in registered_paths}
        assert not missing, (
            f"vacuum POSTs/GETs core paths that are served nowhere: {missing}. "
            "A URL written in one module and registered in none is the task:0045 bug."
        )


# ---------------------------------------------------------------------------
# 2. check_invariants is ADVISORY in the vacuum finalize path (D2)
# ---------------------------------------------------------------------------


class TestCheckInvariantsAdvisory:
    """A non-ok check_invariants must not discard a correctly-swapped compaction.

    Scoped to ``_vacuum_finalize`` ONLY.  The hard gates that actually protect
    the swap (pre-swap EXACT per-table counts, post-swap inode coherence, core
    health) are asserted elsewhere in this file and are untouched.
    """

    @pytest.mark.parametrize(
        ("ci_status", "ci_ok"),
        [
            pytest.param(404, None, id="404-route-missing"),
            pytest.param(503, None, id="503-unavailable"),
            pytest.param(200, False, id="200-ok-false"),
        ],
    )
    def test_non_ok_check_invariants_keeps_the_swap(self, monkeypatch, ci_status, ci_ok) -> None:
        run = _run_vacuum(monkeypatch, post=_ci_post_factory(ci_status, ci_ok))
        assert run.exit_code == 0, (
            "check_invariants is ADVISORY in the vacuum finalize path (D2): a "
            f"non-ok result must not fail the run; got exit {run.exit_code}"
        )
        assert run.compacted_retained, "the compacted DB must stay canonical"
        assert not run.canonical_is_original, "the original must not be promoted back"
        assert run.olds == [], "the previous canonical must be retired so space is reclaimed"

    def test_advisory_failure_is_loud_and_names_the_violations(self, monkeypatch, capsys) -> None:
        _run_vacuum(
            monkeypatch,
            post=_ci_post_factory(200, False, violations=["dangling relationship rows"]),
        )
        err = capsys.readouterr().err
        assert "check_invariants" in err and "ADVISORY" in err, (
            f"an advisory check_invariants failure must be logged loudly; stderr={err!r}"
        )
        assert "dangling relationship rows" in err, (
            f"the advisory log must name WHICH invariants failed; stderr={err!r}"
        )

    def test_connection_error_keeps_the_swap(self, monkeypatch) -> None:
        def fake_post(url: str, **kwargs):
            if "/api/check_invariants" in url:
                raise httpx.ConnectError("refused")
            m = MagicMock()
            m.status_code = 200
            m.text = "OK"
            return m

        run = _run_vacuum(monkeypatch, post=fake_post)
        assert run.exit_code == 0
        assert run.compacted_retained

    def test_ok_true_still_succeeds(self, monkeypatch) -> None:
        run = _run_vacuum(monkeypatch, post=_ci_post_factory(200, True))
        assert run.exit_code == 0
        assert run.compacted_retained
        assert run.olds == []


# ---------------------------------------------------------------------------
# 3. A rolled-back run may never report a positive saving
# ---------------------------------------------------------------------------


class TestRolledBackReportsZeroSaving:
    """The assertion that would have caught the live bug on day one."""

    def _rollback_run(self, monkeypatch) -> _VacuumRun:
        # Inode split-brain is a HARD gate — still rolls back (never weakened).
        return _run_vacuum(
            monkeypatch,
            extra_patches=[
                patch(
                    "yadgar.core.vacuum._verify_live_store_coherence",
                    return_value=(False, {"surreal_db.old-20260729_000000"}),
                )
            ],
        )

    def test_rolled_back_run_reports_zero_saved(self, monkeypatch) -> None:
        run = self._rollback_run(monkeypatch)
        assert run.exit_code == 2, "a rolled-back swap must exit 2"
        assert run.log_row is not None, "the consolidation_log row must still be written"
        assert run.log_row["saved_bytes"] == 0, (
            "a rolled-back vacuum reclaimed NOTHING — reporting the pre-rollback "
            f"figure is the task:0045 lie; got {run.log_row['saved_bytes']}"
        )
        assert run.log_row["saved_pct"] == 0

    def test_rolled_back_row_carries_rolled_back_and_exit_code(self, monkeypatch) -> None:
        run = self._rollback_run(monkeypatch)
        assert run.log_row["rolled_back"] is True
        assert run.log_row["exit_code"] == 2

    def test_retained_run_row_carries_rolled_back_false(self, monkeypatch) -> None:
        run = _run_vacuum(monkeypatch, post=_ci_post_factory(200, True))
        assert run.log_row["rolled_back"] is False
        assert run.log_row["exit_code"] == 0

    def test_rollback_report_does_not_claim_completion(self, monkeypatch, capsys) -> None:
        self._rollback_run(monkeypatch)
        out = capsys.readouterr().out
        assert "ROLLED BACK" in out, (
            f"the report for a rolled-back run must say so, not 'complete'; stdout={out!r}"
        )

    def test_inode_split_brain_still_rolls_back(self, monkeypatch) -> None:
        """R3: the post-swap coherence gate stays HARD — D2 narrows nothing here."""
        run = self._rollback_run(monkeypatch)
        assert run.canonical_is_original, "the original must be promoted back"
        assert not run.compacted_retained, "the unverified compacted DB must be discarded"

    def test_core_health_timeout_still_rolls_back(self, monkeypatch) -> None:
        run = _run_vacuum(
            monkeypatch,
            extra_patches=[patch("yadgar.core.vacuum._wait_for_yadgar_health", return_value=False)],
        )
        assert run.exit_code == 2
        assert run.log_row["saved_bytes"] == 0
        assert run.canonical_is_original


# ---------------------------------------------------------------------------
# 4. Every abort path leaves core RUNNING (task:0027a)
# ---------------------------------------------------------------------------


def _raise(*_args, **_kwargs):
    raise RuntimeError("induced abort")


_ABORT_PATHS = {
    "snapshot-fail": [patch("yadgar.core.vacuum._vacuum_snapshot_and_drop", side_effect=_raise)],
    "side-build-fail": [patch("yadgar.core.vacuum._build_and_verify_side_db", return_value=False)],
    "quiescence-gate": [patch("yadgar.core.vacuum._assert_backend_quiesced", return_value=False)],
    "atomic-swap-fail": [patch("yadgar.core.vacuum._atomic_swap", side_effect=_raise)],
    "post-swap-backend-unhealthy": [
        patch("yadgar.core.vacuum._wait_for_health", return_value=False)
    ],
}


class TestAbortPathsRestartCore:
    """``svc.stop()`` stopped BOTH units; an explicit systemd stop never self-heals.

    Every abort between that stop and finalize must therefore start core again —
    backend first, then core.  The quiescence gate previously restarted nothing
    at all.
    """

    @pytest.mark.parametrize("abort_id", sorted(_ABORT_PATHS))
    def test_abort_path_restarts_core(self, monkeypatch, abort_id) -> None:
        run = _run_vacuum(monkeypatch, extra_patches=_ABORT_PATHS[abort_id])
        assert run.exit_code != 0, f"{abort_id} must not report success"
        assert run.svc.start_yadgar.called, (
            f"abort path {abort_id!r} left yadgar CORE stopped — svc.stop() stopped "
            "both units and systemd will not auto-recover an explicit stop (task:0027a)"
        )

    @pytest.mark.parametrize("abort_id", sorted(_ABORT_PATHS))
    def test_abort_path_restarts_backend_before_core(self, monkeypatch, abort_id) -> None:
        run = _run_vacuum(monkeypatch, extra_patches=_ABORT_PATHS[abort_id])
        names = [c[0] for c in run.svc.method_calls]
        assert "start_backend" in names, f"abort path {abort_id!r} left the backend stopped"
        assert names.index("start_backend") < names.index("start_yadgar"), (
            f"abort path {abort_id!r} started core before the backend (R4 ordering)"
        )

    def test_core_restart_survives_a_failing_backend_restart(self, monkeypatch) -> None:
        """Backend start raising must not swallow the core start (separate try/except)."""
        svc_holder: dict[str, MagicMock] = {}

        def _configure(mock_sc):
            inst = mock_sc.return_value
            inst.start_backend.side_effect = RuntimeError("backend unit masked")
            svc_holder["svc"] = inst

        monkeypatch.setattr(httpx, "get", _fake_get)
        monkeypatch.setattr(httpx, "post", _ci_post_factory(200, True))
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
                mock_sc = _patch_stack(stack)
                _configure(mock_sc)
                stack.enter_context(
                    patch("yadgar.core.vacuum._assert_backend_quiesced", return_value=False)
                )
                cmd_vacuum_impl(_vacuum_args(db))

        assert svc_holder["svc"].start_yadgar.called, (
            "a raising start_backend() must not prevent the core restart — the "
            "whole point of task:0027a is that core comes back"
        )
