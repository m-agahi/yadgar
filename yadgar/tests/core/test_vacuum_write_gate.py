"""task:0113 — the vacuum must QUIESCE writes before it snapshots the row counts.

THE BUG (two defects at one seam, one of them silent):

  T0  _capture_table_counts   — the exact-count baseline
  T1  _vacuum_export          — the side DB is built from THIS
  T2  phases.py svc.stop_backend()

A write landing in ``(T0, T1]`` is in the export but not in the baseline, so
``side_counts != source_counts`` and the run ABORTS spuriously (data safe).
A write landing in ``(T1, T2]`` is in NEITHER number — the exact-count gate is a
comparison of two PRE-stop snapshots, so it passes, the swap is retained, and the
row goes out with the ``rmtree`` of ``.old``.  That is SILENT WRITE LOSS, and no
gate in the file can see it by construction.

The fix quiesces first: engage the core ``_maintenance_mode`` write-gate, drain
the residual file queue (ADR-0139: the live drainer is BACKEND-side, so the nudge
must be a cross-process POST), and only then capture + export.  The gate is held
through the swap and released after finalize.

Mutation sensitivity (plan 0113 §4.4): ``test_maintenance_engaged_before_count_capture``,
``test_gate_not_released_when_already_engaged`` and the nightly-nesting test in
``yadgar/tests/scripts/test_nightly_maintenance.py`` are the three that must fail
if the enter/exit calls are deleted.  The rest are guardrails around them.
"""

from __future__ import annotations

import tempfile
import types as _types
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

# ---------------------------------------------------------------------------
# Scaffolding (mirrors test_vacuum_finalize_verification.py)
# ---------------------------------------------------------------------------

_FAKE_SURQL = "-- TABLE DATA: memory ----\nUPSERT memory:1 CONTENT {};\n"


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


def _fake_get(url: str, **kwargs) -> MagicMock:
    m = MagicMock()
    m.status_code = 200
    m.text = _FAKE_SURQL if "/export" in url else ""
    return m


def _fake_post(url: str, **kwargs) -> MagicMock:
    m = MagicMock()
    m.status_code = 200
    m.text = "OK"
    m.json.return_value = {"ok": True, "violations": [], "previous": False}
    return m


def _make_side_db(backend_url, filtered_path, side_path, source_counts):
    side_path.mkdir(parents=True, exist_ok=True)
    (side_path / "compacted.marker").write_bytes(b"compacted")
    return True


def _raise(*_args, **_kwargs):
    raise RuntimeError("induced abort")


# The abort set enumerated by test_vacuum_finalize_verification.py:372, plus a
# raising body — the gate must be released on EVERY one of them.
_ABORT_PATHS = {
    "snapshot-fail": [patch("yadgar.core.vacuum._vacuum_snapshot_and_drop", side_effect=_raise)],
    "side-build-fail": [patch("yadgar.core.vacuum._build_and_verify_side_db", return_value=False)],
    "quiescence-gate": [patch("yadgar.core.vacuum._assert_backend_quiesced", return_value=False)],
    "atomic-swap-fail": [patch("yadgar.core.vacuum._atomic_swap", side_effect=_raise)],
    "post-swap-backend-unhealthy": [
        patch("yadgar.core.vacuum._wait_for_health", return_value=False)
    ],
    "core-health-timeout": [
        patch("yadgar.core.vacuum._wait_for_yadgar_health", return_value=False)
    ],
    "body-raises": [patch("yadgar.core.vacuum._recover_interrupted_swap", side_effect=_raise)],
}


class _GateRun:
    """Result of one instrumented vacuum run."""

    def __init__(self, exit_code, calls, enter_calls, lock) -> None:
        self.exit_code = exit_code
        self.calls = calls
        self.enter_calls = enter_calls
        self.lock = lock


def _run_vacuum(  # noqa: PLR0913 — test harness knobs, one per failure mode under test
    monkeypatch,
    *,
    previous: bool = False,
    enter_side_effect=None,
    exit_side_effect=None,
    drain_side_effect=None,
    extra_patches=None,
    capsys=None,
) -> _GateRun:
    """Drive cmd_vacuum_impl with an ORDERED call recorder on the gate seams."""
    monkeypatch.setattr(httpx, "get", _fake_get)
    monkeypatch.setattr(httpx, "post", _fake_post)
    monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", "test-token")

    from yadgar.core import vacuum as _v

    calls: list[str] = []
    enter_calls: list[dict] = []

    def _rec(name, ret=None, side_effect=None):
        def _inner(*args, **kwargs):
            calls.append(name)
            if side_effect is not None:
                raise side_effect
            return ret

        return _inner

    def _enter(*args, **kwargs):
        calls.append("enter")
        enter_calls.append({"args": args, "kwargs": kwargs})
        if enter_side_effect is not None:
            raise enter_side_effect
        return previous

    # ``yadgar.core.sensitive_lock`` is a PEP-562 re-export package: the name
    # ``sensitive_lock`` resolves to the SUBMODULE, so the patch targets its
    # module-level acquire/release, not an attribute on the package.
    lock = _types.SimpleNamespace(acquire=MagicMock(return_value=True), release=MagicMock())

    with tempfile.TemporaryDirectory() as td:
        db = _fake_db(td)
        monkeypatch.setenv("YADGAR_HOME", td)
        script = Path(td) / "cleanup-backups.sh"
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(0o755)
        monkeypatch.setenv("YADGAR_CLEANUP_SCRIPT", str(script))

        def _export(backend_url, yadgar_home):
            calls.append("export")
            raw = Path(yadgar_home) / "vacuum_export_raw.surql"
            filtered = Path(yadgar_home) / "vacuum_export_filtered.surql"
            raw.write_text(_FAKE_SURQL)
            filtered.write_text(_FAKE_SURQL)
            return raw, filtered

        with ExitStack() as stack:
            stack.enter_context(
                patch("yadgar.core.sensitive_lock.sensitive_lock.acquire", lock.acquire)
            )
            stack.enter_context(
                patch("yadgar.core.sensitive_lock.sensitive_lock.release", lock.release)
            )
            stack.enter_context(patch.object(_v, "_maintenance_enter", _enter))
            stack.enter_context(
                patch.object(_v, "_maintenance_exit", _rec("exit", side_effect=exit_side_effect))
            )
            stack.enter_context(
                patch.object(
                    _v, "_drain_backend_queue", _rec("drain", side_effect=drain_side_effect)
                )
            )
            stack.enter_context(
                patch.object(_v, "_capture_table_counts", _rec("capture", ret={"memory": 1}))
            )
            stack.enter_context(patch.object(_v, "_vacuum_export", _export))
            stack.enter_context(patch.object(_v, "_log_consolidation_row"))
            stack.enter_context(patch.object(_v, "ServiceController"))
            stack.enter_context(patch.object(_v, "_wait_for_health", return_value=True))
            stack.enter_context(patch.object(_v, "_wait_for_yadgar_health", return_value=True))
            stack.enter_context(patch.object(_v, "_redefine_users_post_import"))
            stack.enter_context(patch.object(_v, "_assert_backend_quiesced", return_value=True))
            stack.enter_context(patch.object(_v, "_has_surreal_binary", return_value=True))
            stack.enter_context(
                patch.object(_v, "_verify_live_store_coherence", return_value=(True, set()))
            )
            stack.enter_context(
                patch.object(_v, "_build_and_verify_side_db", side_effect=_make_side_db)
            )

            real_finalize = _v._vacuum_finalize

            def _finalize(*args, **kwargs):
                out = real_finalize(*args, **kwargs)
                calls.append("finalize")
                return out

            stack.enter_context(patch.object(_v, "_vacuum_finalize", _finalize))
            for ctx in extra_patches or []:
                stack.enter_context(ctx)
            exit_code = _v.cmd_vacuum_impl(_vacuum_args(db))

    return _GateRun(exit_code, calls, enter_calls, lock)


# ---------------------------------------------------------------------------
# 1. Ordering — the whole car in three assertions
# ---------------------------------------------------------------------------


class TestQuiesceOrdering:
    def test_maintenance_engaged_before_count_capture(self, monkeypatch) -> None:
        """THE test: enter → capture → export, in that order.

        RED before task:0113 — the vacuum never engaged the gate at all, so the
        backend accepted writes for the whole (T0, T2] window.
        """
        run = _run_vacuum(monkeypatch)
        assert "enter" in run.calls, "the vacuum never engaged the maintenance write-gate"
        assert run.calls.index("enter") < run.calls.index("capture"), (
            "maintenance was engaged AFTER the count baseline — the (T0, T1] "
            "spurious-abort window is still open"
        )
        assert run.calls.index("capture") < run.calls.index("export"), (
            "count capture must precede the export (unchanged pre-existing order)"
        )

    def test_drain_nudge_precedes_count_capture(self, monkeypatch) -> None:
        """The residual queue is flushed between the gate and the baseline.

        The gate stops NEW MCP calls enqueuing; it does not stop the backend
        drainer applying files already on disk (ADR-0139).
        """
        run = _run_vacuum(monkeypatch)
        assert "drain" in run.calls, "no queue-drain nudge before the count baseline"
        assert run.calls.index("enter") < run.calls.index("drain") < run.calls.index("capture")

    def test_gate_released_after_finalize_on_success(self, monkeypatch) -> None:
        run = _run_vacuum(monkeypatch)
        assert run.exit_code == 0
        assert "exit" in run.calls, "the write-gate was never released"
        assert run.calls.index("finalize") < run.calls.index("exit"), (
            "the gate was released before finalize — writes could land on a DB "
            "whose swap had not yet been verified"
        )


# ---------------------------------------------------------------------------
# 2. Release on every exit path
# ---------------------------------------------------------------------------


class TestGateAlwaysReleased:
    @pytest.mark.parametrize("abort_id", sorted(_ABORT_PATHS))
    def test_gate_released_on_every_abort_path(self, monkeypatch, abort_id) -> None:
        run = _run_vacuum(monkeypatch, extra_patches=_ABORT_PATHS[abort_id])
        assert run.exit_code != 0, f"{abort_id} must not report success"
        assert "exit" in run.calls, (
            f"abort path {abort_id!r} left the core WEDGED in maintenance — "
            "every MCP tool fast-fails until an operator POSTs /maintenance/exit"
        )
        assert run.lock.release.called, f"abort path {abort_id!r} leaked the sensitive-job lock"

    def test_gate_not_released_when_already_engaged(self, monkeypatch) -> None:
        """Nightly nesting guard: enter reports previous=True → we do NOT exit.

        nightly_cycle engages at step 1 and exits at step 7, AFTER the post-backup
        snapshot and prune.  A vacuum that unconditionally exits at the end of
        step 4 would un-gate the engine while the nightly still has DB work left.
        """
        run = _run_vacuum(monkeypatch, previous=True)
        assert run.exit_code == 0
        assert "enter" in run.calls
        assert "exit" not in run.calls, (
            "the vacuum released a maintenance window it did not open — the "
            "nightly's own gate is now un-wedged mid-cycle"
        )
        assert run.lock.release.called


# ---------------------------------------------------------------------------
# 3. Failure modes (plan §2.4)
# ---------------------------------------------------------------------------


class TestGateFailureModes:
    def test_enter_failure_proceeds_with_warning(self, monkeypatch, capsys) -> None:
        """(A) Gate unreachable → WARN and PROCEED (precedent: nightly_cycle.py:248)."""
        run = _run_vacuum(monkeypatch, enter_side_effect=ConnectionError("core down"))
        assert run.exit_code == 0, "an unreachable write-gate must not fail the vacuum"
        assert "capture" in run.calls, "the vacuum stopped instead of proceeding degraded"
        assert "exit" not in run.calls, "exited a gate that was never entered"
        assert "proceeding without write-gate" in capsys.readouterr().err

    def test_drain_failure_proceeds(self, monkeypatch, capsys) -> None:
        """(B) Drain nudge failure → WARN and PROCEED; the exact-count gate remains."""
        run = _run_vacuum(monkeypatch, drain_side_effect=RuntimeError("no YADGAR_EMBED_URL"))
        assert run.exit_code == 0
        assert run.calls.index("enter") < run.calls.index("capture")
        assert "exit" in run.calls
        assert "drain nudge failed" in capsys.readouterr().err

    def test_exit_failure_does_not_mask_a_successful_vacuum(self, monkeypatch, capsys) -> None:
        """(C) common case: core reachable at enter, unreachable at exit.

        The un-gating failure must not be reported as a failed compaction, the
        sensitive-job lock must still be released, and the CRITICAL must name the
        TTL — the only thing that will clear the flag now.
        """
        run = _run_vacuum(monkeypatch, exit_side_effect=ConnectionError("core down"))
        assert run.exit_code == 0, (
            "a failed gate RELEASE was reported as a failed vacuum — the swap was "
            "verified and retained; the exit code must reflect the run"
        )
        assert run.lock.release.called, (
            "a raising _maintenance_exit() skipped sensitive_lock.release() — the "
            "host can never vacuum again (per-step try/except, plan §2.1)"
        )
        err = capsys.readouterr().err
        assert "CRITICAL" in err
        assert "MAINTENANCE_TTL_SEC" in err, (
            "the operator was not told the TTL is the backstop that clears the flag"
        )


# ---------------------------------------------------------------------------
# 4. The helpers themselves
# ---------------------------------------------------------------------------


class TestMaintenanceHelpers:
    def test_enter_posts_to_the_core_and_returns_previous(self, monkeypatch) -> None:
        from yadgar.core import vacuum as _v

        seen: dict = {}

        def _post(url, **kwargs):
            seen["url"] = url
            seen["json"] = kwargs.get("json")
            seen["headers"] = kwargs.get("headers")
            m = MagicMock()
            m.status_code = 200
            m.json.return_value = {"maintenance_mode": True, "previous": True}
            return m

        monkeypatch.setenv("YADGAR_PORT", "9999")
        monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", "tok")
        monkeypatch.setattr(httpx, "post", _post)
        assert _v._maintenance_enter(1234.0) is True
        assert seen["url"] == "http://127.0.0.1:9999/api/control/maintenance/enter"
        # Car 1 (2026-08-20 train): the enter body now NAMES the window so the
        # gate envelope stops hardcoding "(vacuum)" for nightly and backup too.
        assert seen["json"] == {"ttl_seconds": 1234.0, "operation": "vacuum"}
        assert seen["headers"]["Authorization"] == "Bearer tok"

    def test_enter_raises_on_non_2xx(self, monkeypatch) -> None:
        from yadgar.core import vacuum as _v

        def _post(url, **kwargs):
            m = MagicMock()
            m.status_code = 404
            m.text = "not found"
            return m

        monkeypatch.setattr(httpx, "post", _post)
        with pytest.raises(RuntimeError):
            _v._maintenance_enter(60.0)

    def test_drain_targets_the_backend_admin_op_not_surrealdb(self, monkeypatch) -> None:
        """ADR-0139 + ADR-0078: the LIVE drainer is backend-side.

        A core-side drain_now() is a production no-op and SurrealDB serves no
        /admin op — the nudge must be the backend forwarder.
        """
        from yadgar.core import vacuum as _v

        seen: dict = {}

        def _fwd(op, payload, **kwargs):
            seen["op"] = op
            return {"drained": True, "items_processed": 3}

        monkeypatch.setattr("yadgar.core.forward._forward_admin", _fwd)
        _v._drain_backend_queue()
        assert seen["op"] == "drain_now"
