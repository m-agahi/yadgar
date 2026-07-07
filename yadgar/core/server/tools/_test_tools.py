"""Test-only MCP tools — registered ONLY when YADGAR_TEST_TOOLS=1.

These exist to exercise the Fix A (daemon-offload-A) dispatch boundary from a
REAL daemon over HTTP: a deterministic blocking body (`time.sleep`) and a
thread-identity probe. They are NEVER registered in production (the env flag is
unset there) — the conditional import in tools/__init__.py gates them.

Why a sleep tool and not a real recall: `time.sleep` releases the GIL and blocks
the loop ONLY by occupying the loop thread when run inline — which is precisely
what offload fixes — so it is a clean, flake-free probe of the dispatch boundary
(no racing a git cache-bucket boundary).
"""

from __future__ import annotations

import os
import threading
import time

from yadgar._shared.observability.observe import observe
from yadgar.core.server._app import _tool


@observe(tier="stage", metric="tools.test_tools.register_test_tools")
def register_test_tools() -> None:
    """Register the gated test tools. No-op unless YADGAR_TEST_TOOLS=1."""
    if os.environ.get("YADGAR_TEST_TOOLS", "0").strip().lower() not in (
        "1",
        "true",
        "yes",
        "on",
    ):
        return

    @_tool()
    def _test_sleep(seconds: float = 2.0) -> dict:
        """Block the calling thread for `seconds` (test-only).

        Run inline (offload OFF) this occupies the loop thread → /health starves.
        Offloaded (offload ON) it runs on a worker → the loop stays free.
        """
        time.sleep(seconds)
        return {"slept": seconds, "thread": threading.current_thread().name}

    @_tool()
    def _test_thread_id() -> dict:
        """Return the executing thread's identity (test-only).

        Offload ON → ident is a worker thread (not the loop thread).
        Offload OFF → ident is the loop thread.
        """
        return {
            "ident": threading.get_ident(),
            "name": threading.current_thread().name,
        }


# Importing this module triggers registration (gated on YADGAR_TEST_TOOLS).
register_test_tools()
