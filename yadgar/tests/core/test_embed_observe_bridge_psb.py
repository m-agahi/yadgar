"""P-SB backend bridge (plan §3.2): expose yadgar_observe_* on :8001 /metrics.

Recall runs in the backend process; its ``@observe`` stage samples land in the
SHARED registry (``yadgar/_shared/observability/registry.py``). :8001 /metrics
serves only the ISOLATED embed registry, so those stage histograms are never
scraped. ``_SharedObserveBridge`` — a scrape-time custom collector registered on
the embed registry — yields ONLY ``yadgar_observe_*`` families from the shared
registry, closing the exposure gap.

Strict prefix filter is collision-safe: the 7 families in BOTH registries
(``yadgar_cache_*`` / ``yadgar_log_*``) do NOT start with ``yadgar_observe_``, so
the bridge can never duplicate them. The parse + no-duplicate-family test pins
this.
"""

from __future__ import annotations

from prometheus_client import generate_latest
from prometheus_client.parser import text_string_to_metric_families

_OBSERVE_PREFIX = "yadgar_observe_"


def _emit_a_stage_sample(stage_label: str) -> None:
    """Fire a real @observe(tier='stage') fn so the shared registry has an
    observe family with a populated sample (the bridge yields it at scrape).
    """
    from yadgar._shared.observability import observe as observe_mod

    assert observe_mod._PROM_AVAILABLE is True

    @observe_mod.observe(tier="stage", metric=stage_label)
    def _probe() -> int:
        return 1

    _probe()


def test_bridge_is_registered_on_embed_registry():
    """The bridge collector is registered on the backend embed _registry."""
    from yadgar.backend.embed_service import embed_service_metrics as esm

    collectors = esm._registry._collector_to_names.keys()
    assert any(type(c).__name__ == "_SharedObserveBridge" for c in collectors), (
        "_SharedObserveBridge must be registered on the embed registry"
    )


def test_backend_metrics_exposes_observe_families():
    """After a stage emission, the backend exposition contains yadgar_observe_* families."""
    from yadgar.backend.embed_service import embed_service_metrics as esm

    _emit_a_stage_sample("test.psb.bridge.exposure_probe")
    body = generate_latest(esm._registry).decode()
    observe_lines = [
        ln for ln in body.splitlines() if ln.startswith(_OBSERVE_PREFIX) and not ln.startswith("#")
    ]
    assert observe_lines, "no yadgar_observe_* samples in backend exposition"


def test_backend_exposition_parses_and_has_no_duplicate_families():
    """The :8001 exposition parses via text_string_to_metric_families and no family
    name appears twice (the 7 cache/log collision families must not duplicate).
    """
    # CRITICAL: import metrics.py so the SHARED registry carries the 7
    # yadgar_cache_*/yadgar_log_* families that ALSO live on the embed registry —
    # the exact collision the strict prefix filter guards against. Without this
    # import the shared registry holds only yadgar_observe_* and the no-duplicate
    # assertion passes trivially (the hazard is never exercised).
    import yadgar._shared.observability.metrics  # noqa: F401
    from yadgar.backend.embed_service import embed_service_metrics as esm

    _emit_a_stage_sample("test.psb.bridge.dupe_probe")
    body = generate_latest(esm._registry).decode()

    names: list[str] = [mf.name for mf in text_string_to_metric_families(body)]
    dupes = {n for n in names if names.count(n) > 1}
    assert not dupes, f"duplicate metric families in backend exposition: {sorted(dupes)}"

    # The known collider MUST appear exactly once — proves the bridge's strict
    # yadgar_observe_ prefix excluded the shared registry's copy of it. (If the
    # bridge yielded non-observe families, yadgar_cache_hit would appear twice:
    # once from the embed CacheStatsCollector, once bridged from the shared reg.)
    assert names.count("yadgar_cache_hit") == 1, (
        f"collider yadgar_cache_hit appeared {names.count('yadgar_cache_hit')}x — "
        f"bridge prefix filter leaked the shared registry's copy"
    )

    # And confirm at least one observe family is present + parsed.
    observe_families = [n for n in names if n.startswith(_OBSERVE_PREFIX)]
    assert observe_families, "no yadgar_observe_* family parsed from backend exposition"


def test_bridge_yields_only_observe_prefixed_families():
    """The bridge itself must yield ONLY yadgar_observe_* families (strict prefix)."""
    from yadgar.backend.embed_service.embed_service_metrics import _SharedObserveBridge

    _emit_a_stage_sample("test.psb.bridge.prefix_probe")
    yielded = list(_SharedObserveBridge().collect())
    assert yielded, "bridge yielded nothing — shared registry should carry observe families"
    non_observe = [m.name for m in yielded if not m.name.startswith(_OBSERVE_PREFIX)]
    assert not non_observe, f"bridge leaked non-observe families: {non_observe}"
