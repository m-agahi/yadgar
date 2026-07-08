"""I33 STEP 2 — @observe sentinel checks for wiki / security / update functions.

Model-free: imports the modules and asserts the @observe sentinel is set on
representative instrumented functions.
"""

from __future__ import annotations


def _has_span(fn) -> bool:
    return bool(getattr(fn, "_yadgar_observe_has_span", False))


def test_security_allowlist_instrumented():
    from yadgar._shared.security import allowlist

    assert _has_span(allowlist.is_allowlisted)


def test_update_snapshot_create_instrumented():
    from yadgar.core.update import snapshot

    assert _has_span(snapshot.create_snapshot)


def test_update_snapshot_prune_instrumented():
    from yadgar.core.update import snapshot

    assert _has_span(snapshot.prune_old_snapshots)


def test_update_install_methods_instrumented():
    from yadgar.core.update import install_methods

    assert _has_span(install_methods.detect_install_method)


def test_update_check_instrumented():
    from yadgar.core.update import check

    assert _has_span(check.probe_latest_version)


def test_wiki_meta_instrumented():
    from yadgar._shared import wiki_meta

    assert _has_span(wiki_meta.check_page_type_format)


def test_wiki_store_ingest_instrumented():
    from yadgar._shared.wiki import WikiStore

    assert _has_span(WikiStore.ingest)


def test_wiki_store_append_section_instrumented():
    from yadgar._shared.wiki import WikiStore

    assert _has_span(WikiStore.append_section)


def test_update_orchestrator_run_install_instrumented():
    from yadgar.core.update import orchestrator

    assert _has_span(orchestrator.run_install)
