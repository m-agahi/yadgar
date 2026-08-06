"""Guards for the host-ops script tests (``yadgar/core/scripts``).

WHY THIS EXISTS
---------------
``nightly_cycle.main()`` reaches the CORE process over HTTP — the maintenance
write-gate lives there, not in the backend. Engine-#2 car F added a step
(``_step_cross_engine_backup``) that asserts that gate, so a test which calls
``main()`` without stubbing the step will POST ``/api/control/maintenance/enter``
to whatever core is listening on ``127.0.0.1:8765``. On a developer host that is
the LIVE daemon: the test would gate every MCP tool, and only the driver's own
release-on-abort belt would un-gate it.

That is not hypothetical — it happened while building car F. The fix is a
default, not a convention: point ``YADGAR_CORE_URL`` at a port nothing listens
on, so an unstubbed reach fails fast and locally instead of silently mutating
the developer's running engine.

A test that genuinely wants to exercise the HTTP path sets the variable itself;
``monkeypatch.setenv`` inside the test overrides this fixture's value.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_live_core(monkeypatch):
    """Point host-ops scripts at a dead core so no test can gate the live daemon."""
    monkeypatch.setenv("YADGAR_CORE_URL", "http://127.0.0.1:9")
