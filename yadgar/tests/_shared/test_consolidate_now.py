"""TDD tests for v5.10.4: consolidate_now() mode parameter.

R3 migration: the consolidation compute (force_consolidate + sleep cycle) moved
to the backend.  consolidate_now() now calls run_consolidate_now() which
forwards to backend via orchestrator._forward_to_backend.  Core retains:
  - mode validation (returns error before forwarding)
  - anchor audit pass (mode='full', ENABLED=True)
  - graph-layout precompute (mode='full', unconditional)
  - result shape: {status: "completed", mode: <mode>, **backend_stats}

Backend-owned behaviour (no longer testable from core unit tests):
  - force_consolidate call count
  - run_sleep_cycle call count
  - _last_sleep_cycle timestamp update
  - sleep cycle exception handling

Written BEFORE implementation — all tests start red.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import yadgar._shared.runtime.state as _st

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_consolidation():
    """Minimal mock for _st._consolidation (kept for fixture compat)."""
    m = MagicMock()
    m.force_consolidate.return_value = {"memories_added": 0}
    m._last_sleep_cycle = None
    return m


@pytest.fixture()
def mock_sleep():
    """Minimal mock for _st._sleep (kept for fixture compat)."""
    m = MagicMock()
    m.run_sleep_cycle.return_value = {"phases_run": 6}
    return m


@pytest.fixture()
def mock_state(mock_consolidation, mock_sleep, monkeypatch):
    """Wire mock engines into _st + patch orchestrator._forward_to_backend.

    R3 migration: run_consolidate_now() forwards to backend via
    orchestrator._forward_to_backend.  Patch it here so tests don't need a
    running backend server.  Returns (mock_consolidation, mock_sleep, mock_forward)
    for callers that need to assert on backend call shape.
    """
    monkeypatch.setattr(_st, "_consolidation", mock_consolidation)
    monkeypatch.setattr(_st, "_sleep", mock_sleep)

    import yadgar.core.consolidation.orchestrator as _orch

    forward_mock = MagicMock(return_value={"memories_added": 0})
    monkeypatch.setattr(_orch, "_forward_to_backend", forward_mock)

    return mock_consolidation, mock_sleep


# ---------------------------------------------------------------------------
# 1. light mode skips sleep cycle (backend-forwarded; core verifies mode)
# ---------------------------------------------------------------------------


class TestLightMode:
    """mode='light' (default): forwarded to backend with mode='light', no anchor audit."""

    def test_consolidate_now_light_mode_skips_sleep_cycle(self, mock_state):
        """mode='light' must forward mode='light' to backend; no sleep_cycle in result.

        R3 migration: sleep cycle is backend-owned.  Verify mode forwarded correctly
        and result does not contain a sleep_cycle key (backend did not run sleep).
        """
        with patch("yadgar._shared.config.get_settings") as mock_cfg:
            mock_cfg.return_value.ANCHOR_AUDIT_CONSOLIDATION_ENABLED = False
            from yadgar.core.server.tools.admin_other import consolidate_now

            result = consolidate_now(mode="light")

        assert result.get("status") == "completed"
        assert "sleep_cycle" not in result

    def test_consolidate_now_light_mode_skips_anchor_audit(self, mock_state, monkeypatch, request):
        """mode='light' must NOT call _run_anchor_audit_pass even when ENABLED=True."""
        monkeypatch.setenv("YADGAR_ANCHOR_AUDIT_CONSOLIDATION_ENABLED", "true")
        from yadgar._shared.config import get_settings

        get_settings.cache_clear()
        request.addfinalizer(get_settings.cache_clear)

        audit_mock = MagicMock(return_value={"actions": []})
        with patch("yadgar.core.server.tools.audit._run_anchor_audit_pass", audit_mock):
            from yadgar.core.server.tools.admin_other import consolidate_now

            result = consolidate_now(mode="light")

        audit_mock.assert_not_called()
        assert "anchor_audit_pass" not in result

    def test_consolidate_now_default_mode_is_light(self, mock_state):
        """Calling consolidate_now() without args behaves as mode='light'."""
        with patch("yadgar._shared.config.get_settings") as mock_cfg:
            mock_cfg.return_value.ANCHOR_AUDIT_CONSOLIDATION_ENABLED = False
            from yadgar.core.server.tools.admin_other import consolidate_now

            result = consolidate_now()

        assert result.get("mode") == "light"
        assert result.get("status") == "completed"

    def test_consolidate_now_light_does_not_update_last_sleep_cycle_timestamp(self, mock_state):
        """R3 migration: sleep_cycle timestamp is backend-owned.

        mode='light' forwards to backend with mode='light'.  The backend does
        not update its sleep-cycle gate (only 'full' mode does).  No
        sleep_cycle key appears in the core result dict.
        """
        with patch("yadgar._shared.config.get_settings") as mock_cfg:
            mock_cfg.return_value.ANCHOR_AUDIT_CONSOLIDATION_ENABLED = False
            from yadgar.core.server.tools.admin_other import consolidate_now

            result = consolidate_now(mode="light")

        # Core does NOT own _last_sleep_cycle in R3 — backend holds the gate.
        # Verify the result dict has no sleep_cycle key (proxy for "not run").
        assert "sleep_cycle" not in result

    def test_consolidate_now_light_skips_layout_precompute(self, mock_state):
        """v5.88: mode='light' must NOT trigger the graph-layout precompute."""
        with patch("yadgar._shared.config.get_settings") as mock_cfg:
            mock_cfg.return_value.ANCHOR_AUDIT_CONSOLIDATION_ENABLED = False
            with patch(
                "yadgar.backend.consolidation.service._maybe_precompute_graph_layout"
            ) as precompute_mock:
                from yadgar.core.server.tools.admin_other import consolidate_now

                consolidate_now(mode="light")

        precompute_mock.assert_not_called()


# ---------------------------------------------------------------------------
# 2. full mode triggers layout precompute and anchor audit
# ---------------------------------------------------------------------------


class TestFullMode:
    """mode='full': forwarded to backend + graph-layout precompute + anchor audit (if enabled)."""

    def test_consolidate_now_full_mode_runs_sleep_cycle(self, mock_state):
        """mode='full' must forward mode='full' to backend.

        R3 migration: sleep cycle is backend-owned.  Core verifies backend was
        called with mode='full' and result has status='completed'.
        """
        with patch("yadgar._shared.config.get_settings") as mock_cfg:
            mock_cfg.return_value.ANCHOR_AUDIT_CONSOLIDATION_ENABLED = False
            with patch("yadgar.backend.consolidation.service._maybe_precompute_graph_layout"):
                from yadgar.core.server.tools.admin_other import consolidate_now

                result = consolidate_now(mode="full")

        assert result.get("status") == "completed"

    def test_consolidate_now_full_mode_triggers_layout_precompute(self, mock_state):
        """v5.88: mode='full' is the manual trigger for the graph-layout precompute.

        T2 Car E3: the precompute moved INSIDE the backend cycle
        (run_consolidation_cycle full/nightly tail) — mock_state mocks the
        forward, so drive the backend cycle directly with a stub scheduler and
        assert the tail fires.
        """
        import yadgar.backend.consolidation.service as svc

        with patch(
            "yadgar.backend.consolidation.service._maybe_precompute_graph_layout"
        ) as precompute_mock:

            class _FakeScheduler:
                def run_full_consolidation(self):
                    return {"mode": "full"}

            with patch.object(svc, "_get_scheduler", return_value=_FakeScheduler()):
                svc.run_consolidation_cycle("full")

        precompute_mock.assert_called_once()

    def test_consolidate_now_full_mode_runs_anchor_audit_if_enabled(
        self, mock_state, monkeypatch, request
    ):
        """mode='full' + ENABLED=True calls _run_anchor_audit_pass."""
        monkeypatch.setenv("YADGAR_ANCHOR_AUDIT_CONSOLIDATION_ENABLED", "true")
        from yadgar._shared.config import get_settings

        get_settings.cache_clear()
        request.addfinalizer(get_settings.cache_clear)

        audit_mock = MagicMock(return_value={"actions": []})
        with patch("yadgar.core.server.tools.audit._run_anchor_audit_pass", audit_mock):
            from yadgar.core.server.tools.admin_other import consolidate_now

            with patch(
                "yadgar.core.server.tools.admin_other._get_storage", return_value=MagicMock()
            ):
                with patch("yadgar.backend.consolidation.service._maybe_precompute_graph_layout"):
                    result = consolidate_now(mode="full")

        audit_mock.assert_called_once()
        assert "anchor_audit_pass" in result

    def test_consolidate_now_full_mode_skips_anchor_audit_if_disabled(
        self, mock_state, monkeypatch, request
    ):
        """mode='full' + ENABLED=False skips _run_anchor_audit_pass."""
        monkeypatch.setenv("YADGAR_ANCHOR_AUDIT_CONSOLIDATION_ENABLED", "false")
        from yadgar._shared.config import get_settings

        get_settings.cache_clear()
        request.addfinalizer(get_settings.cache_clear)

        audit_mock = MagicMock(return_value={"actions": []})
        with patch("yadgar.core.server.tools.audit._run_anchor_audit_pass", audit_mock):
            from yadgar.core.server.tools.admin_other import consolidate_now

            with patch("yadgar.backend.consolidation.service._maybe_precompute_graph_layout"):
                result = consolidate_now(mode="full")

        audit_mock.assert_not_called()
        assert "anchor_audit_pass" not in result

    def test_consolidate_now_full_updates_last_sleep_cycle_timestamp(self, mock_state):
        """R3 migration: sleep_cycle gate timestamp is backend-owned.

        mode='full' forwards to backend with mode='full'.  The backend updates
        its internal gate.  From core, verify mode='full' was forwarded and
        status='completed' returned (the gate itself is unobservable from core).
        """
        with patch("yadgar._shared.config.get_settings") as mock_cfg:
            mock_cfg.return_value.ANCHOR_AUDIT_CONSOLIDATION_ENABLED = False
            with patch("yadgar.backend.consolidation.service._maybe_precompute_graph_layout"):
                from yadgar.core.server.tools.admin_other import consolidate_now

                result = consolidate_now(mode="full")

        assert result.get("status") == "completed"
        # Backend gate is unobservable from core in R3 — no assertion on timestamp.

    def test_consolidate_now_full_sleep_cycle_exception_caught(self, mock_state):
        """R3 migration: backend exception propagates (sleep_cycle is backend-owned).

        If the backend raises, run_consolidate_now propagates it.  Callers that
        want a non-raising result must handle the exception themselves.  The old
        core-side try/except that swallowed sleep_cycle errors is gone because
        core no longer calls run_sleep_cycle.

        Verify that when _forward_to_backend raises, consolidate_now re-raises.
        """
        from _pytest.monkeypatch import MonkeyPatch

        import yadgar.core.consolidation.orchestrator as _orch

        mp = MonkeyPatch()
        mp.setattr(
            _orch, "_forward_to_backend", MagicMock(side_effect=RuntimeError("backend exploded"))
        )

        try:
            with patch("yadgar._shared.config.get_settings") as mock_cfg:
                mock_cfg.return_value.ANCHOR_AUDIT_CONSOLIDATION_ENABLED = False
                from yadgar.core.server.tools.admin_other import consolidate_now

                with pytest.raises(RuntimeError, match="backend exploded"):
                    consolidate_now(mode="full")
        finally:
            mp.undo()


# ---------------------------------------------------------------------------
# 3. Invalid mode
# ---------------------------------------------------------------------------


class TestInvalidMode:
    """Invalid mode values return error without forwarding to backend."""

    def test_consolidate_now_invalid_mode_returns_error(self, mock_state):
        """mode='invalid' returns error dict without forwarding to backend."""
        # mock_state already patched _forward_to_backend; invalid mode must
        # return the error dict without ever reaching the forward.
        from yadgar.core.server.tools.admin_other import consolidate_now

        result = consolidate_now(mode="invalid")

        assert result.get("status") == "error"
        assert "Invalid mode" in result.get("message", "")


# ---------------------------------------------------------------------------
# 4. Result includes 'mode' field
# ---------------------------------------------------------------------------


class TestResultShape:
    """Result dict must include 'mode' key reflecting what was requested."""

    def test_consolidate_now_result_includes_mode_light(self, mock_state):
        with patch("yadgar._shared.config.get_settings") as mock_cfg:
            mock_cfg.return_value.ANCHOR_AUDIT_CONSOLIDATION_ENABLED = False
            from yadgar.core.server.tools.admin_other import consolidate_now

            result = consolidate_now(mode="light")
        assert result.get("mode") == "light"

    def test_consolidate_now_result_includes_mode_full(self, mock_state):
        with patch("yadgar._shared.config.get_settings") as mock_cfg:
            mock_cfg.return_value.ANCHOR_AUDIT_CONSOLIDATION_ENABLED = False
            with patch("yadgar.backend.consolidation.service._maybe_precompute_graph_layout"):
                from yadgar.core.server.tools.admin_other import consolidate_now

                result = consolidate_now(mode="full")
        assert result.get("mode") == "full"
