"""viz-render-perf (Car A): backend-startup layout-cache bootstrap.

When the graph_layout_cache is empty on backend boot, kick a one-shot precompute
in a background thread so a fresh deploy warms itself (no manual cold-load-1).
Non-blocking, non-fatal. When a cache already exists, the bootstrap is a no-op.

The decision (`cache empty?`) is split from the threading so the test can drive
the synchronous helper without racing a daemon thread.
"""

from __future__ import annotations

# Import the canonical submodule FIRST so its bottom-of-file reload chain finishes
# before we touch the lifecycle sibling directly (importing the sibling first
# triggers a partially-initialized circular import via `import ... embed_service`).
import yadgar.backend.embed_service.embed_service  # noqa: F401
from yadgar.backend.embed_service.embed_service_lifecycle import (
    _bootstrap_graph_layout_if_empty,
    _run_layout_bootstrap,
)


class _FakeStorage:
    def __init__(self, cache):
        self._cache = cache
        self.set_calls = 0

    def get_graph_layout_cache(self):
        return self._cache


def test_bootstrap_noop_when_cache_present():
    """A populated cache → the precompute is NOT invoked."""
    calls: list[str] = []
    storage = _FakeStorage({"signature": "s", "positions": {"a": [1, 2, 3]}})
    _run_layout_bootstrap(storage, object(), precompute=lambda s, se: calls.append("ran"))
    assert calls == []


def test_bootstrap_runs_when_cache_empty():
    """An empty cache → the precompute IS invoked once."""
    calls: list[str] = []
    storage = _FakeStorage(None)
    _run_layout_bootstrap(storage, object(), precompute=lambda s, se: calls.append("ran"))
    assert calls == ["ran"]


def test_bootstrap_swallows_precompute_errors():
    """A precompute exception must not propagate (non-fatal boot step)."""

    def _boom(_s, _se):
        raise RuntimeError("precompute blew up")

    storage = _FakeStorage(None)
    # Must not raise.
    _run_layout_bootstrap(storage, object(), precompute=_boom)


def test_bootstrap_entrypoint_starts_daemon_thread(monkeypatch):
    """The public entrypoint spawns a daemon thread and returns immediately."""
    started: list[bool] = []

    class _FakeThread:
        def __init__(self, target=None, args=(), name=None, daemon=False):
            self._target = target
            self._args = args
            self.daemon = daemon
            self.name = name

        def start(self):
            started.append(self.daemon)

    monkeypatch.setattr(
        "yadgar.backend.embed_service.embed_service_lifecycle.threading.Thread",
        _FakeThread,
    )
    _bootstrap_graph_layout_if_empty(_FakeStorage(None))
    assert started == [True]  # exactly one daemon thread launched
