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
    from yadgar.core.daemon import YadgarDaemon

    assert _has_span(YadgarDaemon.start)


def test_daemon_container_running_sentinel():
    from yadgar.core.daemon import YadgarDaemon

    assert _has_span(YadgarDaemon._container_running)


# ---------------------------------------------------------------------------
# embeddings.py
# ---------------------------------------------------------------------------


def test_embeddings_encode_query_sentinel():
    from yadgar._shared.embeddings import EmbeddingEngine

    assert _has_span(EmbeddingEngine.encode_query)


def test_embeddings_similarity_sentinel():
    from yadgar._shared.embeddings import EmbeddingEngine

    assert _has_span(EmbeddingEngine.similarity)


# ---------------------------------------------------------------------------
# remote_embeddings.py
# ---------------------------------------------------------------------------


def test_remote_embeddings_encode_sentinel():
    from yadgar._shared.embeddings.remote_embeddings import RemoteEmbeddingEngine

    assert _has_span(RemoteEmbeddingEngine.encode)


# ---------------------------------------------------------------------------
# rules_engine.py
# ---------------------------------------------------------------------------


def test_rules_engine_add_rule_sentinel():
    from yadgar._shared.rules_engine import RulesEngine

    assert _has_span(RulesEngine.add_rule)


def test_rules_engine_apply_rules_sentinel():
    from yadgar._shared.rules_engine import RulesEngine

    assert _has_span(RulesEngine.apply_rules)


# ---------------------------------------------------------------------------
# config.py
# ---------------------------------------------------------------------------


def test_config_resolve_knob_sentinel():
    from yadgar._shared.config import resolve_knob

    assert _has_span(resolve_knob)


# ---------------------------------------------------------------------------
# config_yaml.py
# ---------------------------------------------------------------------------


def test_config_yaml_cmd_config_init_sentinel():
    from yadgar._shared.config.config_yaml import cmd_config_init

    assert _has_span(cmd_config_init)


def test_config_yaml_set_config_value_sentinel():
    from yadgar._shared.config.config_yaml import set_config_value

    assert _has_span(set_config_value)


# ---------------------------------------------------------------------------
# config_registry.py
# ---------------------------------------------------------------------------


def test_config_registry_emit_startup_log_sentinel():
    from yadgar._shared.config.config_registry import emit_startup_config_log

    assert _has_span(emit_startup_config_log)


# ---------------------------------------------------------------------------
# core/config_sync/sync.py (moved from _shared in T2 Car A)
# ---------------------------------------------------------------------------


def test_config_sync_cmd_sentinel():
    from yadgar.core.config_sync.sync import cmd_config_sync

    assert _has_span(cmd_config_sync)


# ---------------------------------------------------------------------------
# log_config.py — the ENTIRE logging subsystem is un-instrumentable by @observe.
# It is the sink @observe's own span+metric+log writes flow into: a span here
# → LogSpanProcessor 'span_end' log → re-enters the observed log path → per-log
# span amplification flood that crash-looped core+backend under real OTLP
# (v5.105 → v5.106). These fns MUST NOT carry the @observe span sentinel; they
# are path-glob-exempted in .observe-allowlist.json. Asserting the NEGATIVE here
# guards against re-introducing the flood. See test_log_span_amplification.py.
# ---------------------------------------------------------------------------


def test_log_config_configure_logging_not_observed():
    from yadgar._shared.observability.log_config import configure_logging

    assert not _has_span(configure_logging), (
        "configure_logging must not be @observe'd — log-emission path span→log→span flood (v5.106)"
    )


def test_log_config_is_sensitive_not_observed():
    from yadgar._shared.observability.log_config import _is_sensitive

    assert not _has_span(_is_sensitive), (
        "_is_sensitive must not be @observe'd — log-emission path span→log→span flood (v5.106)"
    )


# ---------------------------------------------------------------------------
# restoration.py
# ---------------------------------------------------------------------------


def test_restoration_anchor_memory_sentinel():
    from yadgar._shared.restoration import CheckpointRestore

    assert _has_span(CheckpointRestore.anchor_memory)


# ---------------------------------------------------------------------------
# staleness.py
# ---------------------------------------------------------------------------


def test_staleness_validate_memory_sentinel():
    from yadgar.core.staleness import StalenessDetector

    assert _has_span(StalenessDetector.validate_memory)


# ---------------------------------------------------------------------------
# narrative.py
# ---------------------------------------------------------------------------


def test_narrative_generate_narrative_sentinel():
    from yadgar.backend.narrative import NarrativeEngine

    assert _has_span(NarrativeEngine.generate_narrative)


# ---------------------------------------------------------------------------
# conflict_resolver.py
# ---------------------------------------------------------------------------


def test_conflict_resolver_resolve_sentinel():
    from yadgar.backend.conflict_resolver import resolve_conflict

    assert _has_span(resolve_conflict)


# ---------------------------------------------------------------------------
# prospective.py
# ---------------------------------------------------------------------------


def test_prospective_create_trigger_sentinel():
    from yadgar.backend.prospective import ProspectiveMemoryEngine

    assert _has_span(ProspectiveMemoryEngine.create_trigger)


# ---------------------------------------------------------------------------
# thermodynamics.py
# ---------------------------------------------------------------------------


def test_thermodynamics_compute_surprise_sentinel():
    from yadgar._shared.thermodynamics import MemoryThermodynamics

    assert _has_span(MemoryThermodynamics.compute_surprise)


# ---------------------------------------------------------------------------
# sensory_buffer.py
# ---------------------------------------------------------------------------


def test_sensory_buffer_capture_sentinel():
    from yadgar._shared.sensory_buffer import ActionLogger

    assert _has_span(ActionLogger.capture)


# ---------------------------------------------------------------------------
# auth_middleware.py
# ---------------------------------------------------------------------------


def test_auth_middleware_is_protected_sentinel():
    from yadgar.core.auth_middleware import _is_protected

    assert _has_span(_is_protected)


# ---------------------------------------------------------------------------
# rate_limit.py
# ---------------------------------------------------------------------------


def test_rate_limit_allow_sentinel():
    from yadgar._shared.rate_limit import TokenBucketRateLimiter

    assert _has_span(TokenBucketRateLimiter.allow)


# ---------------------------------------------------------------------------
# sensitive_lock.py
# ---------------------------------------------------------------------------


def test_sensitive_lock_acquire_sentinel():
    from yadgar.core.sensitive_lock import acquire

    assert _has_span(acquire)


# ---------------------------------------------------------------------------
# secrets.py
# ---------------------------------------------------------------------------


def test_secrets_gate_or_reject_sentinel():
    from yadgar._shared.security.secrets import gate_or_reject

    assert _has_span(gate_or_reject)


# ---------------------------------------------------------------------------
# ops.py
# ---------------------------------------------------------------------------


def test_ops_detect_service_mode_sentinel():
    from yadgar.core.ops import detect_service_mode

    assert _has_span(detect_service_mode)


# ---------------------------------------------------------------------------
# backup.py
# ---------------------------------------------------------------------------


def test_backup_create_snapshot_sentinel():
    from yadgar.core.backup import create_snapshot

    assert _has_span(create_snapshot)


# ---------------------------------------------------------------------------
# drain.py
# ---------------------------------------------------------------------------


def test_drain_drain_in_flight_requests_sentinel():
    from yadgar.core.daemon.drain import drain_in_flight_requests

    assert _has_span(drain_in_flight_requests)


# ---------------------------------------------------------------------------
# paths.py
# ---------------------------------------------------------------------------


def test_paths_data_dir_sentinel():
    from yadgar._shared.paths import _data_dir

    assert _has_span(_data_dir)


# ---------------------------------------------------------------------------
# core/install/platform_paths.py (moved from _shared in T2 Car A)
# ---------------------------------------------------------------------------


def test_platform_paths_get_claude_config_dir_sentinel():
    from yadgar.core.install.platform_paths import get_claude_config_dir

    assert _has_span(get_claude_config_dir)


# ---------------------------------------------------------------------------
# install_hooks_lib.py
# ---------------------------------------------------------------------------


def test_install_hooks_impl_sentinel():
    from yadgar.core.install.install_hooks_lib import install_hooks_impl

    assert _has_span(install_hooks_impl)


# ---------------------------------------------------------------------------
# install_subagents_lib.py
# ---------------------------------------------------------------------------


def test_install_subagents_impl_sentinel():
    from yadgar.core.install.install_subagents_lib import install_subagents_impl

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
    from yadgar._shared.observability.exception_telemetry import record_exception

    assert not _has_span(record_exception), "record_exception must not open its own span"
    reason = getattr(record_exception, "_yadgar_observe_exempt", None)
    assert isinstance(reason, str) and reason.strip(), (
        "record_exception must carry an @observe(exempt=...) rationale"
    )


# ---------------------------------------------------------------------------
# sanitize.py
# ---------------------------------------------------------------------------


def test_sanitize_log_field_sentinel():
    from yadgar.core.sanitize import sanitize_log_field

    assert _has_span(sanitize_log_field)


# ---------------------------------------------------------------------------
# sd_notify.py
# ---------------------------------------------------------------------------


def test_sd_notify_sentinel():
    from yadgar.core.daemon.sd_notify import notify

    assert _has_span(notify)


# ---------------------------------------------------------------------------
# models.py
# ---------------------------------------------------------------------------


def test_models_indent_continuation_sentinel():
    from yadgar._shared.contracts.models import _indent_continuation

    assert _has_span(_indent_continuation)


# ---------------------------------------------------------------------------
# _surreal_runner.py
# ---------------------------------------------------------------------------


def test_surreal_runner_spawn_surreal_sentinel():
    from yadgar.core._surreal_runner import spawn_surreal

    assert _has_span(spawn_surreal)


def test_surreal_runner_allocate_port_sentinel():
    from yadgar.core._surreal_runner import allocate_port

    assert _has_span(allocate_port)
