"""Task 68 / Car C1: vacuum-abort belt exercises the Requires=-pinned
cascade path — proves the belt actually restores the core when the
backend-stop cascaded onto it.

A generator change does NOT rewrite units already installed: a host whose
``yadgar.service`` still carries ``Requires=yadgar-backend.service`` will see
``svc.stop_backend()`` cascade onto the core too.  This test models that
cascade in the ServiceController double: ``stop_backend`` also brings the
core down (it does, on those hosts), and the abort belt must put the core
back up via ``start_yadgar``.

The pre-existing ``test_abort_belt_keeps_both_starts`` (in
``test_vacuum_core_stays_up.py``) only proves both starts ARE called — it
never asserts the core is observably up afterward.  A belt that called
``start_yadgar`` on a stopped-already-running core would pass it.  Task 68
closes that gap.
"""

from __future__ import annotations

from unittest.mock import MagicMock


def _recording_svc_with_cascade() -> tuple[MagicMock, dict[str, int]]:
    """ServiceController double that simulates the Requires=-pinned cascade.

    ``stop_backend`` is the cascade carrier: when called, it ALSO flips the
    internal ``core_up`` flag to False (mirroring the systemd behaviour that
    a Requires= relationship drags the dependent down when its dependency
    stops).  ``start_yadgar`` flips it back; ``start_backend`` flips the
    backend back on (we record but don't gate on that).  Every other method
    is a no-op recorder.
    """
    state = {"core_up": True, "backend_up": True}
    counts = {"stop_backend": 0, "start_backend": 0, "start_yadgar": 0}

    svc = MagicMock()
    svc.stop.side_effect = lambda: (state.update(core_up=False, backend_up=False), counts.update())[
        1
    ]
    svc.stop_backend.side_effect = lambda: (
        state.update(core_up=False, backend_up=False),
        counts.update(stop_backend=1),
    )[1]
    svc.start_backend.side_effect = lambda: (
        state.update(backend_up=True),
        counts.update(start_backend=1),
    )[1]
    svc.start_yadgar.side_effect = lambda: (
        state.update(core_up=True),
        counts.update(start_yadgar=1),
    )[1]
    svc.state = state  # type: ignore[attr-defined]
    return svc, counts


def test_abort_belt_lifts_cascade_after_stop_backend() -> None:
    """The belt must bring the core back UP after a Requires=-cascade.

    Simulates a host whose ``yadgar.service`` carries
    ``Requires=yadgar-backend.service``: calling ``stop_backend`` cascades
    onto the core too, leaving ``core_up=False``.  The abort belt must then
    call ``start_yadgar`` (and only that — ``start_backend`` ran first per
    the documented order in ``_restart_services_after_abort``) and end with
    ``core_up=True`` and ``backend_up=True``.
    """
    from yadgar.core.vacuum import _restart_services_after_abort

    svc, counts = _recording_svc_with_cascade()

    # 1. Simulate the cascade (Phase 2 stopping the backend drags the core down).
    svc.stop_backend()
    assert svc.state["core_up"] is False, (
        "test fixture broken: stop_backend must cascade the core down on a Requires=-pinned host"
    )
    assert svc.state["backend_up"] is False

    # 2. Belt fires — this is what we're proving is load-bearing.
    _restart_services_after_abort(svc, backend=True)

    # 3. Core IS back up.  This is the assertion that did not exist pre-task-68.
    assert svc.state["core_up"] is True, (
        "abort belt did NOT lift the cascade — Requires=-pinned host would "
        "stay DOWN with the memory engine unreachable"
    )
    assert svc.state["backend_up"] is True

    # Belt must have run both starts, in order.
    assert counts["start_backend"] == 1, f"belt must start the backend first; counts={counts}"
    assert counts["start_yadgar"] == 1, (
        f"belt must start the core to undo the cascade; counts={counts}"
    )


def test_abort_belt_lifts_cascade_even_when_backend_already_up() -> None:
    """The belt still lifts the core even when ``backend=False`` is passed.

    Some callers reach the belt with the backend already up (the post-swap
    finalize rollback path); the core is the only thing still down because
    of the cascade, and the belt must lift it via ``start_yadgar``.
    """
    from yadgar.core.vacuum import _restart_services_after_abort

    svc, counts = _recording_svc_with_cascade()

    # Cascade: backend off AND core off.
    svc.stop_backend()
    assert svc.state["core_up"] is False
    # Caller restarted the backend already (post-swap path).
    svc.start_backend()

    # Belt runs with backend=False — must NOT call start_backend again, must
    # still call start_yadgar.
    _restart_services_after_abort(svc, backend=False)

    assert svc.state["core_up"] is True, (
        "belt with backend=False still must lift the core; otherwise the "
        "post-swap rollback leaves the memory engine DOWN on Requires=-pinned hosts"
    )
    assert counts["start_backend"] == 1, (
        f"backend=False must NOT re-start the backend; counts={counts}"
    )
    assert counts["start_yadgar"] == 1
