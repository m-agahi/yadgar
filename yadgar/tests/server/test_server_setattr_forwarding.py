"""Regression test for v5.1 server-split __setattr__ infinite recursion.

The B-06 server split introduced a ``_ServerModule`` subclass with
``__setattr__`` that forwarded writes to ``yadgar.server._state``.
The original implementation called ``setattr(_st, name, value)`` which
recursed infinitely under at least one test path (monkeypatch.setattr on
the yadgar.server module triggered the fix loop). This file pins the
non-recursive behavior and confirms that writes propagate to ``_state``.
"""

from __future__ import annotations

import yadgar._shared.runtime.state as _state
import yadgar.core.server as _server


def test_setattr_on_server_module_does_not_recurse() -> None:
    """Direct setattr on yadgar.server must complete without RecursionError."""
    # If __setattr__ recurses, this raises RecursionError instead of returning.
    _server._file_queue = None
    assert _server._file_queue is None


def test_setattr_propagates_to_state_module() -> None:
    """When _state owns the attribute, both modules must see the new value."""
    sentinel = object()
    # _file_queue is a known _state-owned global.
    original = _state._file_queue
    try:
        _server._file_queue = sentinel
        assert _server._file_queue is sentinel
        assert _state._file_queue is sentinel
    finally:
        _state.__dict__["_file_queue"] = original
        _server.__dict__["_file_queue"] = original


def test_setattr_on_unknown_attr_only_writes_module_dict() -> None:
    """Attrs not in _state must only land on the module, not be mirrored."""
    name = "_v5_1_test_attr_should_not_appear_in_state"
    assert name not in _state.__dict__
    try:
        setattr(_server, name, 42)
        assert _server.__dict__[name] == 42
        assert name not in _state.__dict__
    finally:
        _server.__dict__.pop(name, None)


def test_monkeypatch_setattr_works(monkeypatch) -> None:
    """The exact pattern used in conftest._isolate_file_queue must not recurse."""
    monkeypatch.setattr(_server, "_file_queue", None)
    assert _server._file_queue is None
