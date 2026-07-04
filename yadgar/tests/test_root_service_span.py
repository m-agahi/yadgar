"""I33 P5 obs-wave sentinel tests — root-level yadgar/*.py service modules.

Model-free: no embeddings, no SurrealDB, no OTLP endpoint required.
Each test imports a module and asserts that the sentinel attribute
``_yadgar_observe_has_span`` is present on the decorated function or method,
confirming @observe was applied and the decorator contract executed.

Only spot-checks a representative sample per file (one boundary, one stage/hot).
Full MISSING=0 coverage is verified by the I33 lint hook (check_observe_coverage.py).

Run with:
    HF_HUB_OFFLINE=1 HF_HUB_DISABLE_XET=1 YADGAR_OTLP_ENDPOINT='' \\
    uv run --extra test --extra ml python -m pytest \\
    yadgar/tests/test_root_service_span.py \\
    yadgar/tests/test_observe_decorator.py \\
    yadgar/tests/test_tracing.py -p no:randomly -o addopts=""
"""

from __future__ import annotations

_SENTINEL = "_yadgar_observe_has_span"


def _has_span(fn) -> bool:
    return bool(getattr(fn, _SENTINEL, False))


# ---------------------------------------------------------------------------
# daemon.py
# ---------------------------------------------------------------------------


def test_daemon_start_sentinel():
    from yadgar.daemon import YadgarDaemon

    assert _has_span(YadgarDaemon.start)


def test_daemon_container_running_sentinel():
    from yadgar.daemon import YadgarDaemon

    assert _has_span(YadgarDaemon._container_running)


# ---------------------------------------------------------------------------
# embeddings.py
# ---------------------------------------------------------------------------


def test_embeddings_encode_query_sentinel():
    from yadgar.embeddings import EmbeddingEngine

    assert _has_span(EmbeddingEngine.encode_query)


def test_embeddings_similarity_sentinel():
    from yadgar.embeddings import EmbeddingEngine

    assert _has_span(EmbeddingEngine.similarity)


# ---------------------------------------------------------------------------
# remote_embeddings.py
# ---------------------------------------------------------------------------


def test_remote_embeddings_encode_sentinel():
    from yadgar.remote_embeddings import RemoteEmbeddingEngine

    assert _has_span(RemoteEmbeddingEngine.encode)


# ---------------------------------------------------------------------------
# rules_engine.py
# ---------------------------------------------------------------------------


def test_rules_engine_add_rule_sentinel():
    from yadgar.rules_engine import RulesEngine

    assert _has_span(RulesEngine.add_rule)


def test_rules_engine_apply_rules_sentinel():
    from yadgar.rules_engine import RulesEngine

    assert _has_span(RulesEngine.apply_rules)


# ---------------------------------------------------------------------------
# config.py
# ---------------------------------------------------------------------------


def test_config_resolve_knob_sentinel():
    from yadgar.config import resolve_knob

    assert _has_span(resolve_knob)


# ---------------------------------------------------------------------------
# config_yaml.py
# ---------------------------------------------------------------------------


def test_config_yaml_cmd_config_init_sentinel():
    from yadgar.config_yaml import cmd_config_init

    assert _has_span(cmd_config_init)


def test_config_yaml_set_config_value_sentinel():
    from yadgar.config_yaml import set_config_value

    assert _has_span(set_config_value)


# ---------------------------------------------------------------------------
# config_registry.py
# ---------------------------------------------------------------------------


def test_config_registry_emit_startup_log_sentinel():
    from yadgar.config_registry import emit_startup_config_log

    assert _has_span(emit_startup_config_log)


# ---------------------------------------------------------------------------
# config_sync.py
# ---------------------------------------------------------------------------


def test_config_sync_cmd_sentinel():
    from yadgar.config_sync import cmd_config_sync

    assert _has_span(cmd_config_sync)


# ---------------------------------------------------------------------------
# log_config.py
# ---------------------------------------------------------------------------


def test_log_config_configure_logging_sentinel():
    from yadgar.log_config import configure_logging

    assert _has_span(configure_logging)


def test_log_config_is_sensitive_sentinel():
    from yadgar.log_config import _is_sensitive

    assert _has_span(_is_sensitive)


# ---------------------------------------------------------------------------
# restoration.py
# ---------------------------------------------------------------------------


def test_restoration_anchor_memory_sentinel():
    from yadgar.restoration import CheckpointRestore

    assert _has_span(CheckpointRestore.anchor_memory)


# ---------------------------------------------------------------------------
# staleness.py
# ---------------------------------------------------------------------------


def test_staleness_scan_directory_sentinel():
    from yadgar.staleness import StalenessDetector

    assert _has_span(StalenessDetector.scan_directory)


# ---------------------------------------------------------------------------
# narrative.py
# ---------------------------------------------------------------------------


def test_narrative_generate_narrative_sentinel():
    from yadgar.narrative import NarrativeEngine

    assert _has_span(NarrativeEngine.generate_narrative)


# ---------------------------------------------------------------------------
# conflict_resolver.py
# ---------------------------------------------------------------------------


def test_conflict_resolver_resolve_sentinel():
    from yadgar.conflict_resolver import resolve_conflict

    assert _has_span(resolve_conflict)


# ---------------------------------------------------------------------------
# prospective.py
# ---------------------------------------------------------------------------


def test_prospective_create_trigger_sentinel():
    from yadgar.prospective import ProspectiveMemoryEngine

    assert _has_span(ProspectiveMemoryEngine.create_trigger)


# ---------------------------------------------------------------------------
# thermodynamics.py
# ---------------------------------------------------------------------------


def test_thermodynamics_compute_surprise_sentinel():
    from yadgar.thermodynamics import MemoryThermodynamics

    assert _has_span(MemoryThermodynamics.compute_surprise)


# ---------------------------------------------------------------------------
# sensory_buffer.py
# ---------------------------------------------------------------------------


def test_sensory_buffer_capture_sentinel():
    from yadgar.sensory_buffer import ActionLogger

    assert _has_span(ActionLogger.capture)


# ---------------------------------------------------------------------------
# auth_middleware.py
# ---------------------------------------------------------------------------


def test_auth_middleware_is_protected_sentinel():
    from yadgar.auth_middleware import _is_protected

    assert _has_span(_is_protected)


# ---------------------------------------------------------------------------
# rate_limit.py
# ---------------------------------------------------------------------------


def test_rate_limit_allow_sentinel():
    from yadgar.rate_limit import TokenBucketRateLimiter

    assert _has_span(TokenBucketRateLimiter.allow)


# ---------------------------------------------------------------------------
# sensitive_lock.py
# ---------------------------------------------------------------------------


def test_sensitive_lock_acquire_sentinel():
    from yadgar.sensitive_lock import acquire

    assert _has_span(acquire)


# ---------------------------------------------------------------------------
# secrets.py
# ---------------------------------------------------------------------------


def test_secrets_gate_or_reject_sentinel():
    from yadgar.secrets import gate_or_reject

    assert _has_span(gate_or_reject)


# ---------------------------------------------------------------------------
# ops.py
# ---------------------------------------------------------------------------


def test_ops_detect_service_mode_sentinel():
    from yadgar.ops import detect_service_mode

    assert _has_span(detect_service_mode)


# ---------------------------------------------------------------------------
# backup.py
# ---------------------------------------------------------------------------


def test_backup_create_snapshot_sentinel():
    from yadgar.backup import create_snapshot

    assert _has_span(create_snapshot)


# ---------------------------------------------------------------------------
# drain.py
# ---------------------------------------------------------------------------


def test_drain_drain_in_flight_requests_sentinel():
    from yadgar.drain import drain_in_flight_requests

    assert _has_span(drain_in_flight_requests)


# ---------------------------------------------------------------------------
# paths.py
# ---------------------------------------------------------------------------


def test_paths_data_dir_sentinel():
    from yadgar.paths import _data_dir

    assert _has_span(_data_dir)


# ---------------------------------------------------------------------------
# platform_paths.py
# ---------------------------------------------------------------------------


def test_platform_paths_get_claude_config_dir_sentinel():
    from yadgar.platform_paths import get_claude_config_dir

    assert _has_span(get_claude_config_dir)


# ---------------------------------------------------------------------------
# install_hooks_lib.py
# ---------------------------------------------------------------------------


def test_install_hooks_impl_sentinel():
    from yadgar.install_hooks_lib import install_hooks_impl

    assert _has_span(install_hooks_impl)


# ---------------------------------------------------------------------------
# install_subagents_lib.py
# ---------------------------------------------------------------------------


def test_install_subagents_impl_sentinel():
    from yadgar.install_subagents_lib import install_subagents_impl

    assert _has_span(install_subagents_impl)


# ---------------------------------------------------------------------------
# exception_telemetry.py
# ---------------------------------------------------------------------------


def test_exception_telemetry_record_exception_sentinel():
    """record_exception is @observe(exempt=...), NOT span-sourced.

    It enriches the CALLER's active span (sets ERROR status + records the
    exception onto it); opening its own child span would enrich the wrong span
    and double the span count (see test_record_exception_enriches_active_span,
    which asserts exactly ONE span). So it is a categorized no-op exempt — the
    coverage lint counts a non-empty @observe(exempt=...) reason as satisfied.
    """
    from yadgar.exception_telemetry import record_exception

    assert not _has_span(record_exception), "record_exception must not open its own span"
    reason = getattr(record_exception, "_yadgar_observe_exempt", None)
    assert isinstance(reason, str) and reason.strip(), (
        "record_exception must carry an @observe(exempt=...) rationale"
    )


# ---------------------------------------------------------------------------
# sanitize.py
# ---------------------------------------------------------------------------


def test_sanitize_log_field_sentinel():
    from yadgar.sanitize import sanitize_log_field

    assert _has_span(sanitize_log_field)


# ---------------------------------------------------------------------------
# sd_notify.py
# ---------------------------------------------------------------------------


def test_sd_notify_sentinel():
    from yadgar.sd_notify import notify

    assert _has_span(notify)


# ---------------------------------------------------------------------------
# models.py
# ---------------------------------------------------------------------------


def test_models_indent_continuation_sentinel():
    from yadgar.models import _indent_continuation

    assert _has_span(_indent_continuation)


# ---------------------------------------------------------------------------
# _surreal_runner.py
# ---------------------------------------------------------------------------


def test_surreal_runner_spawn_surreal_sentinel():
    from yadgar._surreal_runner import spawn_surreal

    assert _has_span(spawn_surreal)


def test_surreal_runner_allocate_port_sentinel():
    from yadgar._surreal_runner import allocate_port

    assert _has_span(allocate_port)
