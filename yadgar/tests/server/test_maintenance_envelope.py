"""Car 1 (2026-08-20 train) — the maintenance envelope an instance can act on.

Three properties, all of them evidence-driven (see the car's design note):

1. **The envelope carries enough to decide what to do.**  The pre-car payload was
   ``{"error": "maintenance", "message": "yadgar maintenance in progress (vacuum);
   retry shortly"}``.  "Shortly" was measured at >= 7 minutes (gate 21:00:01 ->
   ``.old-`` dirs cleared 21:07:04) plus a further ~30s of backend warm-up AFTER
   the gate lifts.  Worse, the four checks an instance naturally runs to confirm a
   vacuum is live ALL say no vacuum is running: ``/health`` reads ``ok``, the
   ``yadgar-vacuum`` unit reads ``inactive (dead)``, ``list-timers`` points days
   away, and the triggers dir is empty once consumed.  The message has to pre-empt
   that or the instance files a false "stuck gate" bug — which is exactly what
   nearly happened on 2026-08-20.

2. **Every registered tool must be able to DELIVER the envelope.**  The gate
   returns a bare dict, and the MCP SDK derives an output model from each tool's
   RETURN ANNOTATION.  A ``-> list[dict]`` tool is wrapped as
   ``{"result": list[dict]}`` and the envelope dies in pydantic with
   ``type=list_type``; a ``-> str`` tool dies with ``type=string_type``.  Nine
   tools are ``-> list[dict]`` and one is ``-> str``, so ten tools answered a
   maintenance window with a schema crash instead of a retry signal.  These tests
   drive off the REAL registered tool metadata so a new tool with a new annotation
   cannot re-open the class silently.

3. **/health must stop contradicting the tools** — it carries a ``maintenance``
   block while gated.  Additive ONLY: ``status`` stays ``"ok"``, because the
   handler 503s on any non-ok status and P0 watches this endpoint.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import yadgar._shared.runtime.state as _st


@pytest.fixture(autouse=True)
def _reset_maintenance_state():
    """The gate vars are process-global — reset around every test."""
    from yadgar._shared.runtime.maintenance import reset_maintenance_state

    reset_maintenance_state()
    yield
    reset_maintenance_state()


def _engage(*, operation: str | None = "vacuum", phase: str | None = None, elapsed: float = 0.0):
    """Open a window as the enter handler would, `elapsed` seconds ago."""
    import time

    _st._maintenance_mode = True
    _st._maintenance_entered_at = time.monotonic() - elapsed
    _st._maintenance_operation = operation
    _st._maintenance_phase = phase


# ---------------------------------------------------------------------------
# 1. The envelope
# ---------------------------------------------------------------------------

_REQUIRED_KEYS = {
    "error",
    "operation",
    "phase",
    "started_at",
    "elapsed_seconds",
    "typical_duration_seconds",
    "expected_done_by",
    "looks_stuck",
    "retry_after_seconds",
    "writes_were_rejected_not_queued",
    "message",
    "resume",
}


class TestEnvelopeShape:
    def test_every_field_is_present(self) -> None:
        from yadgar._shared.runtime.maintenance import build_maintenance_envelope

        _engage(operation="vacuum", phase="reimport", elapsed=372)
        env = build_maintenance_envelope()
        assert set(env) == _REQUIRED_KEYS, "the envelope contract drifted"
        assert env["error"] == "maintenance"
        assert env["operation"] == "vacuum"
        assert env["phase"] == "reimport"
        assert env["elapsed_seconds"] == 372
        assert env["writes_were_rejected_not_queued"] is True

    def test_started_at_and_expected_done_by_are_iso_utc(self) -> None:
        from datetime import datetime

        from yadgar._shared.runtime.maintenance import build_maintenance_envelope

        _engage(elapsed=60)
        env = build_maintenance_envelope()
        started = datetime.fromisoformat(env["started_at"])
        done_by = datetime.fromisoformat(env["expected_done_by"])
        assert started.tzinfo is not None, "started_at must be tz-aware UTC"
        assert (done_by - started).total_seconds() == env["typical_duration_seconds"]

    def test_elapsed_degrades_to_none_when_entered_at_is_unknown(self) -> None:
        """A flag flipped directly (tests, a legacy caller) must not crash the gate."""
        from yadgar._shared.runtime.maintenance import build_maintenance_envelope

        _st._maintenance_mode = True
        _st._maintenance_entered_at = None
        env = build_maintenance_envelope()
        assert env["elapsed_seconds"] is None
        assert env["started_at"] is None
        assert env["expected_done_by"] is None
        assert env["looks_stuck"] is False, "unknown elapsed is not evidence of a stall"
        assert env["retry_after_seconds"] >= 60

    def test_operation_falls_back_rather_than_claiming_vacuum(self) -> None:
        """The old copy hardcoded '(vacuum)'. An unlabelled window must not lie."""
        from yadgar._shared.runtime.maintenance import build_maintenance_envelope

        _engage(operation=None, elapsed=5)
        assert build_maintenance_envelope()["operation"] == "maintenance"

    def test_looks_stuck_only_past_three_typical_durations(self) -> None:
        from yadgar._shared.runtime.maintenance import (
            STUCK_MULTIPLIER,
            TYPICAL_DURATION_SECONDS,
            build_maintenance_envelope,
        )

        _engage(operation="vacuum", elapsed=TYPICAL_DURATION_SECONDS * STUCK_MULTIPLIER - 5)
        assert build_maintenance_envelope()["looks_stuck"] is False
        _engage(operation="vacuum", elapsed=TYPICAL_DURATION_SECONDS * STUCK_MULTIPLIER + 5)
        assert build_maintenance_envelope()["looks_stuck"] is True

    def test_a_healthy_nightly_is_never_reported_as_stuck(self) -> None:
        """Nightly holds the gate across steps 1-7 — hours, not minutes.

        ``_maintenance_entered_at`` is stamped by the OUTER enter, so elapsed
        spans the WHOLE cycle. Under one flat 10-minute typical, a healthy night
        would read ``looks_stuck: true`` half an hour in and ``resume`` would open
        by telling the caller to stop and report a broken system — reintroducing,
        from inside the envelope, exactly the false signal it exists to delete.
        """
        from yadgar._shared.runtime.maintenance import build_maintenance_envelope

        _engage(operation="nightly", elapsed=2000)  # 33 min into a 6h-TTL window
        env = build_maintenance_envelope()
        assert env["looks_stuck"] is False
        assert env["typical_duration_seconds"] == 3600
        assert "STOP retrying" not in env["resume"]
        assert "~60m" in env["message"], "the prose must quote the SAME typical"

    def test_an_unknown_operation_falls_back_to_the_default_typical(self) -> None:
        from yadgar._shared.runtime.maintenance import (
            TYPICAL_DURATION_SECONDS,
            build_maintenance_envelope,
        )

        _engage(operation="some-future-job", elapsed=5)
        assert build_maintenance_envelope()["typical_duration_seconds"] == (
            TYPICAL_DURATION_SECONDS
        )

    def test_expected_done_by_uses_the_operations_own_typical(self) -> None:
        from datetime import datetime

        from yadgar._shared.runtime.maintenance import build_maintenance_envelope

        _engage(operation="nightly", elapsed=60)
        env = build_maintenance_envelope()
        started = datetime.fromisoformat(env["started_at"])
        done_by = datetime.fromisoformat(env["expected_done_by"])
        assert (done_by - started).total_seconds() == 3600

    def test_retry_after_clears_the_post_gate_backend_warmup(self) -> None:
        """The backend needs ~30s AFTER the gate lifts. A tighter retry lands in it."""
        from yadgar._shared.runtime.maintenance import build_maintenance_envelope

        for elapsed in (0, 10, 400, 5000):
            _engage(elapsed=elapsed)
            assert build_maintenance_envelope()["retry_after_seconds"] >= 60


class TestEnvelopeProse:
    """The message exists to stop the instance chasing four lying signals."""

    def _message(self) -> str:
        from yadgar._shared.runtime.maintenance import build_maintenance_envelope

        _engage(elapsed=372)
        return build_maintenance_envelope()["message"]

    @pytest.mark.parametrize(
        "needle",
        [
            "/health",  # says status: ok throughout
            "yadgar-vacuum",  # the unit reads inactive (dead)
            "list-timers",  # points days away
            "triggers",  # dir is empty once consumed
        ],
    )
    def test_message_names_each_misleading_signal(self, needle: str) -> None:
        assert needle in self._message(), (
            f"the message does not pre-empt {needle!r} — an instance will check it, "
            f"read 'no vacuum running', and file a false stuck-gate bug"
        )

    def test_message_says_do_not_go_looking_for_evidence(self) -> None:
        assert "DO NOT" in self._message()

    def test_message_names_the_only_reliable_signal(self) -> None:
        msg = self._message()
        assert "retry_after_seconds" in msg
        assert "succeed" in msg.lower()

    def test_resume_says_writes_were_rejected_not_queued(self) -> None:
        """CONFIRMED empirically: a task_write in the window consumed no id."""
        from yadgar._shared.runtime.maintenance import build_maintenance_envelope

        _engage(elapsed=10)
        resume = build_maintenance_envelope()["resume"]
        assert "REJECTED" in resume
        assert "not queued" in resume
        assert "re-issue" in resume.lower(), "the instance needs the recovery recipe"

    def test_message_pins_the_on_disk_artifact_signal(self) -> None:
        """Step 5 of the plan — on-disk artifacts (``surreal_db.pre-vacuum-*``)
        are the fourth lying signal the plan names explicitly. The
        parametrized test above covers three by substring; this pins the
        fourth literally because the prose names the file globs, and a future
        edit that drops one of those globs reads as "vacuum isn't running" to
        an instance greppping for it (task 278, C3)."""
        msg = self._message()
        assert "surreal_db.pre-vacuum-" in msg, (
            "the on-disk artifacts bullet names the pre-vacuum db glob verbatim"
        )
        assert "vacuum_export_" in msg, (
            "the on-disk artifacts bullet names the export glob verbatim"
        )

    def test_resume_pins_all_four_recipe_steps(self) -> None:
        """Step 5 of the plan — the four-step recipe in ``_resume`` is the part a
        caller acts on, so each step must be greppable. ``test_resume_says_writes_
        were_rejected_not_queued`` covers the 'REJECTED, not queued' preamble but
        not the four steps themselves (task 278, C3)."""
        from yadgar._shared.runtime.maintenance import build_maintenance_envelope

        _engage(elapsed=10)
        resume = build_maintenance_envelope()["resume"]
        for needle in (
            "Sleep `retry_after_seconds`, retry the failed call",
            "Re-issue every write",
            "Verify each one by RE-READING",
            "If `elapsed_seconds` exceeds 3x `typical_duration_seconds`",
        ):
            assert needle in resume, (
                f"recipe step {needle!r} drifted from resume prose — callers grep for it"
            )

    def test_stuck_window_resume_says_stop_and_report(self) -> None:
        """Step 6 of the plan — ``_resume(looks_stuck=True)`` prepends a STOP
        clause; ``test_looks_stuck_only_past_three_typical_durations`` proves
        the BOOLEAN flips but not the literal prose that flips a healthy "wait
        a minute" into a hard "report the bug" (task 278, C3)."""
        from yadgar._shared.runtime.maintenance import (
            STUCK_MULTIPLIER,
            TYPICAL_DURATION_SECONDS,
            build_maintenance_envelope,
        )

        _engage(operation="vacuum", elapsed=TYPICAL_DURATION_SECONDS * STUCK_MULTIPLIER + 5)
        resume = build_maintenance_envelope()["resume"]
        assert "STOP retrying and report" in resume, (
            "the stuck clause must contain the literal phrase — instances grep for it"
        )
        assert "`looks_stuck: true`" in resume, (
            "the stuck clause must quote the boolean key so a grep on the envelope finds it"
        )

    def test_message_pins_the_post_gate_warmup_floor_verbatim(self) -> None:
        """Step 7 of the plan — the second-window explanation names the 60s
        floor verbatim. ``test_retry_after_clears_the_post_gate_backend_warmup``
        pins the BOUND (``>= 60``) but not the literal in the prose, and the
        constant could be tightened below the floor in the future without
        tripping the bound test (task 278, C3)."""
        from yadgar._shared.runtime.maintenance import (
            RETRY_AFTER_SECONDS,
            build_maintenance_envelope,
        )

        _engage(elapsed=10)
        msg = build_maintenance_envelope()["message"]
        assert "`retry_after_seconds` (60s)" in msg, (
            "the second-window explanation must quote the floor verbatim"
        )
        assert RETRY_AFTER_SECONDS == 60, (
            f"RETRY_AFTER_SECONDS must stay at 60 — the post-gate warm-up floor. "
            f"got {RETRY_AFTER_SECONDS}"
        )


# ---------------------------------------------------------------------------
# 2. Delivery — the output-contract class
# ---------------------------------------------------------------------------


def _registered_tools():
    from yadgar.core.server._app import mcp_server

    return mcp_server._tool_manager.list_tools()


def test_the_class_is_real_returning_the_envelope_breaks_output_models() -> None:
    """Document WHY the gate raises instead of returning.

    Not a hypothetical: run the real registered tools' output models over the
    envelope and count the ones that reject it.  If this list is ever empty the
    raise-based delivery could be revisited — until then, returning the dict is a
    schema crash for every tool in it.
    """
    from yadgar._shared.runtime.maintenance import build_maintenance_envelope

    _engage(elapsed=1)
    env = build_maintenance_envelope()
    rejected = []
    for tool in _registered_tools():
        meta = tool.fn_metadata
        if meta.output_schema is None:
            continue
        try:
            meta.convert_result(env)
        # The REJECTION is the assertion. `convert_result` is fastmcp's
        # pydantic-backed validator and the class it raises is an
        # implementation detail of that stack, not of this test.
        except Exception:  # noqa: BLE001 — any rejection is the signal being counted
            rejected.append(tool.name)
    assert rejected, "no tool rejects the envelope — re-evaluate the raise-based gate"
    for name in ("recall", "agent_dispatch_prelude"):
        assert name in rejected, (
            f"{name} used to crash its own output contract during a maintenance "
            f"window; this test must keep proving the return-path is unusable"
        )


def test_every_registered_tool_delivers_the_envelope_under_maintenance() -> None:
    """The whole registered surface, not the five tools someone remembered.

    Driven off the live registry so a NEW tool with a new return annotation is
    covered the day it lands.
    """
    from yadgar._shared.runtime.maintenance import MaintenanceGateError

    _engage(operation="vacuum", elapsed=42)
    tools = _registered_tools()
    assert len(tools) > 50, "the registry looks unpopulated — the sweep proves nothing"
    for tool in tools:
        with pytest.raises(MaintenanceGateError) as exc:
            asyncio.run(tool.fn())
        payload = exc.value.envelope
        assert payload["error"] == "maintenance", tool.name
        assert "maintenance" in str(exc.value), tool.name
        assert json.loads(str(exc.value).split("\n\n")[-1])["error"] == "maintenance", (
            f"{tool.name}: the structured envelope must survive as parseable text — "
            f"a ToolError only carries str(exc) to the caller"
        )


def test_the_tool_body_never_runs_when_the_gate_raises() -> None:
    from yadgar._shared.runtime.maintenance import MaintenanceGateError
    from yadgar.core.server._app import _build_tool_wrappers

    ran = []

    def _tool() -> list[dict]:
        ran.append(1)
        return []

    _sync, _async = _build_tool_wrappers(_tool, _tool, lambda _r: 0)
    _engage(elapsed=1)
    with pytest.raises(MaintenanceGateError):
        asyncio.run(_async())
    assert ran == [], "the tool body ran during maintenance"


def test_sync_wrapper_keeps_returning_the_envelope_dict() -> None:
    """Direct-call contract (internal/test callers) is unchanged — it returns."""
    from yadgar.core.server._app import _build_tool_wrappers

    def _tool() -> list[dict]:
        return []

    sync_wrapper, _async = _build_tool_wrappers(_tool, _tool, lambda _r: 0)
    _engage(elapsed=1)
    out = sync_wrapper()
    assert out["error"] == "maintenance"
    assert out["resume"], "the direct-call path gets the full envelope too"


# ---------------------------------------------------------------------------
# 3. /health
# ---------------------------------------------------------------------------


class TestHealthCarriesMaintenance:
    def test_health_reports_the_window(self) -> None:
        from yadgar._shared.runtime.maintenance import apply_maintenance_health

        _engage(operation="vacuum", phase="reimport", elapsed=120)
        payload = {"status": "ok"}
        apply_maintenance_health(payload)
        assert payload["maintenance"] == {
            "active": True,
            "operation": "vacuum",
            "phase": "reimport",
            "elapsed_seconds": 120,
        }

    def test_health_status_stays_ok_while_gated(self) -> None:
        """LOUD: the handler 503s on any non-ok status and P0 watches /health.

        A maintenance window must never flip the status, or every nightly becomes
        a 503 and a potential health-kill.
        """
        from yadgar._shared.runtime.maintenance import apply_maintenance_health

        _engage(elapsed=5)
        payload = {"status": "ok"}
        apply_maintenance_health(payload)
        assert payload["status"] == "ok"

    def test_no_maintenance_key_when_not_gated(self) -> None:
        from yadgar._shared.runtime.maintenance import apply_maintenance_health

        payload = {"status": "ok"}
        apply_maintenance_health(payload)
        assert "maintenance" not in payload

    def test_expired_ttl_is_not_reported_as_an_active_window(self) -> None:
        """An expired deadline means the gate is already open — health must agree."""
        import time

        from yadgar._shared.runtime.maintenance import apply_maintenance_health

        _engage(elapsed=10)
        _st._maintenance_deadline = time.monotonic() - 1.0
        payload = {"status": "ok"}
        apply_maintenance_health(payload)
        assert "maintenance" not in payload

    def test_the_real_health_payload_carries_the_block(self, monkeypatch) -> None:
        """Call the REAL builder, not a source grep.

        Deliberately not ``inspect.getsource`` string-matching: that is the
        mechanism this car just retired from ``test_real_gate_payload_matches_
        this_copy`` for being brittle, and it proves the call site exists without
        proving it runs in the right place.
        """
        import yadgar.core.server.http as srv_http

        monkeypatch.delenv("YADGAR_DB_URL", raising=False)
        monkeypatch.delenv("YADGAR_EMBED_URL", raising=False)
        _engage(operation="vacuum", elapsed=90)
        payload = asyncio.run(srv_http._build_health_payload())
        assert payload["maintenance"]["active"] is True
        assert payload["maintenance"]["operation"] == "vacuum"
        assert payload["status"] == "ok"

    def test_the_timeout_fallback_payload_carries_the_block(self) -> None:
        """The handler builds a FRESH dict on timeout — it misses the block.

        More likely during a window, not less: the backend is stopped, so a
        dependency probe is exactly what exhausts the handler budget.
        """
        import inspect

        import yadgar.core.server.http as srv_http

        source = inspect.getsource(srv_http.health_check)
        head = source.split("# C1 (obs-train)")[0]
        assert "apply_maintenance_health(payload)" in head, (
            "the TimeoutError fallback returns a payload with no maintenance "
            "block — /health goes back to contradicting the gated tools on "
            "exactly the path a maintenance window makes most likely"
        )


# ---------------------------------------------------------------------------
# 4. State hygiene
# ---------------------------------------------------------------------------


class TestStateHygiene:
    def test_enter_records_operation_and_phase(self) -> None:
        from yadgar.core.server.routes.control import maintenance_enter_handler

        from .test_maintenance_gate import _request  # noqa: PLC2701

        asyncio.run(maintenance_enter_handler(_request({"operation": "backup", "phase": "dump"})))
        assert _st._maintenance_operation == "backup"
        assert _st._maintenance_phase == "dump"

    def test_nested_enter_does_not_blank_the_outer_label(self) -> None:
        from yadgar.core.server.routes.control import maintenance_enter_handler

        from .test_maintenance_gate import _request  # noqa: PLC2701

        asyncio.run(maintenance_enter_handler(_request({"operation": "nightly"})))
        asyncio.run(maintenance_enter_handler(_request({})))
        assert _st._maintenance_operation == "nightly"

    def test_nested_enter_does_not_relabel_the_outer_window(self) -> None:
        """Nightly holds the gate across steps 1-7; its step-4 vacuum nests.

        ``operation`` names the WINDOW, and the window the caller is waiting on
        is the outer one — a nested relabel would report a 7-minute vacuum while
        the real wait is nightly's multi-hour cycle.
        """
        from yadgar.core.server.routes.control import maintenance_enter_handler

        from .test_maintenance_gate import _request  # noqa: PLC2701

        asyncio.run(maintenance_enter_handler(_request({"operation": "nightly"})))
        asyncio.run(maintenance_enter_handler(_request({"operation": "vacuum"})))
        assert _st._maintenance_operation == "nightly"

    def test_nested_enter_advances_the_phase(self) -> None:
        """Re-entering is the phase channel — no new route, no route-literal churn."""
        from yadgar.core.server.routes.control import maintenance_enter_handler

        from .test_maintenance_gate import _request  # noqa: PLC2701

        asyncio.run(maintenance_enter_handler(_request({"operation": "vacuum", "phase": "export"})))
        asyncio.run(maintenance_enter_handler(_request({"phase": "reimport"})))
        assert _st._maintenance_operation == "vacuum"
        assert _st._maintenance_phase == "reimport"

    def test_exit_clears_the_label(self) -> None:
        from yadgar.core.server.routes.control import (
            maintenance_enter_handler,
            maintenance_exit_handler,
        )

        from .test_maintenance_gate import _request  # noqa: PLC2701

        asyncio.run(maintenance_enter_handler(_request({"operation": "vacuum"})))
        asyncio.run(maintenance_exit_handler(_request({})))
        assert _st._maintenance_operation is None
        assert _st._maintenance_phase is None

    def test_ttl_self_heal_clears_the_label_too(self) -> None:
        """A stale label surviving into the next window mislabels the next envelope."""
        import time

        from yadgar.core.server._app import _build_tool_wrappers

        def _tool() -> dict:
            return {"ran": True}

        sync_wrapper, _async = _build_tool_wrappers(_tool, _tool, lambda _r: 0)
        _engage(operation="vacuum", phase="reimport", elapsed=10)
        _st._maintenance_deadline = time.monotonic() - 1.0
        assert sync_wrapper() == {"ran": True}
        assert _st._maintenance_operation is None
        assert _st._maintenance_phase is None
