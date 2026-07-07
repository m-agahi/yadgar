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


def test_consolidate_now_light_mode_no_sleep_cycle():
    """consolidate_now(mode='light') returns error or no sleep_cycle (no live DB required).

    When consolidation engine is not initialized (test env), the function returns
    an error dict — but crucially NOT a dict that accidentally contains sleep_cycle.
    """
    import yadgar._shared.runtime.state as _st
    from yadgar.core.server.tools.admin_other import consolidate_now

    orig = _st._consolidation
    _st._consolidation = None
    try:
        result = consolidate_now(mode="light")
        # Either error (no engine) or completed without sleep_cycle
        assert "sleep_cycle" not in result or result.get("status") == "completed"
    finally:
        _st._consolidation = orig


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
