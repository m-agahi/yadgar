"""I33 STEP 2 — @observe sentinel checks for causal_discovery/sleep_compute/repo_wiki/vacuum."""

from __future__ import annotations


def _has_span(fn) -> bool:
    return bool(getattr(fn, "_yadgar_observe_has_span", False))


def test_causal_discover_instrumented():
    from yadgar.causal_discovery import CausalDiscovery

    assert _has_span(CausalDiscovery.discover_dag)


def test_repo_wiki_scanner_instrumented():
    from yadgar.repo_wiki import scanner

    assert _has_span(scanner.scan_repo)


def test_vacuum_phases_instrumented():
    from yadgar.vacuum import phases

    assert _has_span(phases._atomic_swap)


def test_causal_pc_stages_instrumented():
    from yadgar.causal_discovery import pc

    assert _has_span(pc.pc_algorithm)
    assert _has_span(pc.build_event_matrix)


def test_vacuum_init_boundary_instrumented():
    from yadgar.vacuum import cmd_vacuum_impl

    assert _has_span(cmd_vacuum_impl)
