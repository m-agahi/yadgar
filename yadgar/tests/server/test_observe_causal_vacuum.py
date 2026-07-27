"""I33 STEP 2 — @observe sentinel checks for causal_discovery/sleep_compute/vacuum."""

from __future__ import annotations


def _has_span(fn) -> bool:
    return bool(getattr(fn, "_yadgar_observe_has_span", False))


def test_causal_discover_instrumented():
    from yadgar.backend.causal_discovery import CausalDiscovery

    assert _has_span(CausalDiscovery.discover_dag)


def test_vacuum_phases_instrumented():
    from yadgar.core.vacuum import phases

    assert _has_span(phases._atomic_swap)


def test_causal_pc_stages_instrumented():
    from yadgar.backend.causal_discovery import pc

    assert _has_span(pc.pc_algorithm)
    assert _has_span(pc.build_event_matrix)


def test_vacuum_init_boundary_instrumented():
    from yadgar.core.vacuum import cmd_vacuum_impl

    assert _has_span(cmd_vacuum_impl)
