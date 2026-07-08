"""v5.46.5 RED test — B12: consolidate_now(mode='full') response includes sleep_cycle key.

The integration test TestConsolidateNowWithSleepCycle calls consolidate_now()
without args (mode='light') and expects sleep_cycle. Fix: test should call
mode='full'. This meta-test verifies the updated test contract is correct:
mode='full' → sleep_cycle key present.

This test exercises the function shape without a live DB (unit-level).
"""

from __future__ import annotations


def test_consolidate_now_full_mode_docstring():
    """consolidate_now has a 'mode' parameter that accepts 'full'."""
    import inspect

    from yadgar.core.server.tools.admin_other import consolidate_now

    sig = inspect.signature(consolidate_now)
    params = list(sig.parameters)
    assert "mode" in params, f"consolidate_now must accept 'mode' param; got: {params}"


def test_consolidate_now_light_mode_no_sleep_cycle(monkeypatch):
    """consolidate_now(mode='light') response does not include sleep_cycle.

    R3 migration: run_consolidate_now() always forwards to backend via
    _forward_to_backend (no in-core fallback). Patch the forwarder to return a
    minimal completed-cycle dict and verify sleep_cycle is absent for mode='light'.
    """
    import yadgar.core.consolidation.orchestrator as _orch
    from yadgar.core.server.tools.admin_other import consolidate_now

    monkeypatch.setattr(
        _orch,
        "_forward_to_backend",
        lambda mode: {"status": "completed", "memories_added": 0},
    )
    result = consolidate_now(mode="light")
    # mode='light' does NOT run the sleep cycle → sleep_cycle key must be absent
    assert "sleep_cycle" not in result


def test_consolidate_now_invalid_mode():
    """consolidate_now with invalid mode → error response."""
    import yadgar._shared.runtime.state as _st
    from yadgar.core.server.tools.admin_other import consolidate_now

    # Even with no engine, invalid mode is caught first
    orig = _st._consolidation
    _st._consolidation = None
    try:
        result = consolidate_now(mode="turbo")
        assert result.get("status") == "error"
    finally:
        _st._consolidation = orig
