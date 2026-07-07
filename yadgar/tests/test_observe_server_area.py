"""I33 STEP 2 — @observe sentinel checks for server non-tool + memorize-phase functions.

Model-free: imports server helper modules and asserts the @observe sentinel is set
(or the governed exempt marker is present) on representative instrumented functions.
"""

from __future__ import annotations


def _has_span(fn) -> bool:
    return bool(getattr(fn, "_yadgar_observe_has_span", False))


def _is_exempt(fn) -> bool:
    return hasattr(fn, "_yadgar_observe_exempt")


def test_helpers_file_hash_instrumented():
    from yadgar.core.server import _helpers

    assert _has_span(_helpers._file_hash)


def test_offload_pool_stats_instrumented():
    from yadgar._shared.runtime import offload as _offload

    assert _has_span(_offload.pool_stats)


def test_offload_run_offloaded_instrumented():
    from yadgar._shared.runtime import offload as _offload

    assert _has_span(_offload.run_offloaded)


def test_http_wiki_versioning_instrumented():
    from yadgar.core.server import http_wiki_versioning

    assert _has_span(http_wiki_versioning._wiki_search_semantic)


def test_control_route_handler_instrumented():
    from yadgar.core.server.routes import control

    assert _has_span(control.control_action_handler)


def test_control_update_handler_instrumented():
    from yadgar.core.server.routes import control_update

    assert _has_span(control_update.control_update_handler)


def test_logs_poll_handler_instrumented():
    from yadgar.core.server.routes import logs

    assert _has_span(logs.logs_poll_handler)


def test_phase_validate_instrumented():
    from yadgar.core.server.tools._memorize_phases import _phase_validate

    assert _has_span(_phase_validate._validate_gate_and_policy)


def test_phase_post_write_instrumented():
    from yadgar.core.server.tools._memorize_phases import _phase_post_write

    assert _has_span(_phase_post_write._build_response)


def test_app_helpers_instrumented():
    from yadgar.core.server import _app

    assert _has_span(_app._get_allowed_origins)


def test_lifecycle_init_engines_instrumented():
    from yadgar._shared.runtime import lifecycle

    assert _has_span(lifecycle.init_engines)


def test_lifecycle_metrics_loop_exempt():
    """_metrics_loop opens a manual span in-body → governed @observe(exempt=...).

    R2a Car D1: _metrics_loop moved to yadgar.core.daemons.
    """
    from yadgar.core import daemons

    assert _is_exempt(daemons._metrics_loop)


def test_http_event_stream_is_exempt():
    """_make_event_stream is an async generator → governed @observe(exempt=...)."""
    from yadgar.core.server import http

    assert _is_exempt(http._make_event_stream)
