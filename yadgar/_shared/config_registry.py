"""Yadgar runtime configuration registry (v5.6.7 PR-J).

Single source of truth for every env knob yadgar reads.
The registry is consumed by:
  - GET /admin/config  — lists all knobs with current resolved values
  - startup config-dump log line  — event="startup.config"
  - yadgar_config_value{name} gauge family — numeric knobs only

Each entry is a ConfigEntry dataclass with:
  name     — env var name
  default  — raw string default (as yadgar uses it)
  kind     — "int" | "float" | "bool" | "string"
  redact   — True if value should always be masked regardless of name pattern

Redaction policy (applied by ConfigEntry.value()):
  1. Entry.redact=True → always "<redacted>"
  2. Name matches /(secret|token|key|password|auth)/i → "<redacted>"
  3. Otherwise → raw string value from os.environ or default

The existing os.getenv() call sites are intentionally NOT refactored in this PR.
Each entry's getter lambda calls os.getenv() live so tests can monkeypatch env
without module reload.

Future PR may replace scattered os.getenv() calls with registry.get(name).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from functools import lru_cache

import yadgar._shared.paths as _paths
from yadgar._shared.observability.observe import observe

logger = logging.getLogger(__name__)

_REDACT_RE = re.compile(r"(secret|token|key|password|auth)", re.IGNORECASE)

_REDACTED = "<redacted>"


# ---------------------------------------------------------------------------
# yaml-awareness (Bug A, v5.89)
# ---------------------------------------------------------------------------
# ConfigEntry needs 3-way source attribution (env > yaml > default), mirroring
# pydantic's settings_customise_sources precedence. The missing piece is knowing
# *which keys are present in config.yaml* — distinct from "the effective value
# equals the default". We read the yaml ONCE (cached) through the same loader
# Settings uses (config_yaml.load_yaml(get_config_path())), keyed to the
# ConfigEntry.name form (YADGAR_<UPPER>) so source()/_raw_value() can answer
# "is this knob in the yaml layer, and what is its yaml value?".
#
# The cache MUST be cleared alongside get_settings.cache_clear() on any yaml
# write (POST /api/control/config) and in tests that swap config files — use
# clear_config_caches() for both at once (O1 / advisor item 3).


@observe(tier="hot")
def _stringify_yaml_value(val: object) -> str:
    """Render a ruamel-typed yaml value to the canonical lowercase-bool string.

    bool → 'true'/'false' (ADR-0013, matches env/default + the POST response),
    everything else → str(val). Keeps GET 'current' + source attribution
    consistent with the env/default convention.
    """
    if isinstance(val, bool):
        return "true" if val else "false"
    return str(val)


@observe(tier="stage")
@lru_cache(maxsize=1)
def _yaml_layer() -> dict[str, str]:
    """Return {YADGAR_<UPPER>: stringified-yaml-value} for keys present in config.yaml.

    Cached — cleared via clear_config_caches() on yaml writes / config-file swaps.
    Keys are normalised to the ConfigEntry.name form (YADGAR_-prefixed, uppercase)
    so SURREAL_* aliases (not Settings fields, never YADGAR_-prefixed in yaml)
    simply never match — correct (they resolve env-or-default).
    """
    try:
        from yadgar._shared.config_yaml import get_config_path, load_yaml  # noqa: PLC0415

        raw = load_yaml(get_config_path())
    except Exception:  # noqa: BLE001 — a broken/missing yaml means "no yaml layer"
        return {}
    layer: dict[str, str] = {}
    for key, val in raw.items():
        if val is None:
            continue
        layer[f"YADGAR_{str(key).upper()}"] = _stringify_yaml_value(val)
    return layer


@observe(tier="stage")
def clear_config_caches() -> None:
    """Clear BOTH the yaml-present cache and the get_settings lru_cache.

    Single hot-reload entry point (advisor item 3): a yaml write that clears only
    get_settings would leave source() reporting 'default' until restart. POST
    /api/control/config and tests that swap config files MUST call this.
    """
    _yaml_layer.cache_clear()
    from yadgar._shared.config import get_settings  # noqa: PLC0415 — avoid import cycle

    get_settings.cache_clear()

    # Car 0 obs: config caches are @lru_cache singletons (near-100% hit after first
    # call; .cache_clear()/.cache_info() are used externally so a per-call hit/miss
    # wrapper would not be behavior-neutral). Emit the rare structural bust instead
    # — flood-safe and the informative signal for a singleton reload.
    from yadgar._shared.metrics import record_cache_evict  # noqa: PLC0415

    record_cache_evict("config_yaml")
    record_cache_evict("config_settings")


@dataclass
class ConfigEntry:
    """One registered env knob."""

    name: str
    default: str
    kind: str  # "int" | "float" | "bool" | "string"
    redact: bool = False

    @observe(tier="hot")
    def source(self) -> str:
        """Return source layer: 'env' > 'yaml' > 'default' (pydantic precedence)."""
        if self.name in os.environ:
            return "env"
        if self.name in _yaml_layer():
            return "yaml"
        return "default"

    @observe(tier="hot")
    def _raw_value(self) -> str:
        """Return resolved raw string value (env > yaml > default), without redaction."""
        if self.name in os.environ:
            return os.environ[self.name]
        yaml_layer = _yaml_layer()
        if self.name in yaml_layer:
            return yaml_layer[self.name]
        return self.default

    def _should_redact(self) -> bool:
        return self.redact or bool(_REDACT_RE.search(self.name))

    @observe(tier="hot")
    def value(self) -> str:
        """Return value string, redacted if required.

        Routes through _raw_value() so value()/as_dict() agree with source()
        (env > yaml > default) — no self-contradicting dict (advisor item 1).
        """
        if self._should_redact():
            return _REDACTED
        return self._raw_value()

    def as_dict(self) -> dict:
        """Serialise to the /admin/config JSON schema."""
        return {
            "name": self.name,
            "value": self.value(),
            "source": self.source(),
            "kind": self.kind,
        }


# ---------------------------------------------------------------------------
# Registry — all YADGAR_* knobs (and select non-prefixed DB auth aliases)
# ---------------------------------------------------------------------------
# Order here doesn't matter — list_config() sorts alphabetically.

_REGISTRY: list[ConfigEntry] = [
    # ── Core server ──────────────────────────────────────────────────────────
    ConfigEntry("YADGAR_PORT", "8765", "int"),
    ConfigEntry("YADGAR_DB_URL", "http://127.0.0.1:8000", "string"),
    ConfigEntry("YADGAR_EMBED_URL", "", "string"),
    ConfigEntry("YADGAR_DATA_DIR", str(_paths.DATA_DIR), "string"),
    ConfigEntry("YADGAR_HOST", "127.0.0.1", "string"),
    # ── Auth ────────────────────────────────────────────────────────────────
    ConfigEntry("YADGAR_MCP_AUTH_TOKEN", "", "string", redact=True),
    ConfigEntry("YADGAR_REQUIRE_AUTH", "1", "bool"),
    ConfigEntry("YADGAR_ALLOW_ROOT", "0", "bool"),
    # ── Database credentials ─────────────────────────────────────────────────
    ConfigEntry("YADGAR_DB_USER", "root", "string"),
    ConfigEntry("YADGAR_DB_PASS", "root", "string", redact=True),
    # ── Embedding / ML ───────────────────────────────────────────────────────
    ConfigEntry("YADGAR_EMBEDDING_MODEL", "all-MiniLM-L6-v2", "string"),
    ConfigEntry("YADGAR_MODEL_IDLE_EVICTION_SECONDS", "0", "int"),
    ConfigEntry("YADGAR_OLLAMA_URL", "http://localhost:11434", "string"),
    ConfigEntry("YADGAR_OLLAMA_MODEL", "qwen3:8b", "string"),
    ConfigEntry("YADGAR_CONFLICT_K", "5", "int"),
    ConfigEntry("YADGAR_CROSS_ENCODER_BACKEND", "st", "string"),
    ConfigEntry("YADGAR_GTE_RERANKER_BACKEND", "torch", "string"),
    ConfigEntry("YADGAR_GTE_RERANKER_ONNX_FILE", "onnx/model_int8.onnx", "string"),
    # ── Logging ─────────────────────────────────────────────────────────────
    ConfigEntry("YADGAR_BACKEND_LOG_LEVEL", "warn", "string"),
    ConfigEntry("YADGAR_CORE_LOG_LEVEL", "WARNING", "string"),
    ConfigEntry("YADGAR_LOG_FORMAT", "json", "string"),
    ConfigEntry("YADGAR_LOG_DIR", str(_paths.LOG_DIR), "string"),
    ConfigEntry("YADGAR_LOG_FILE_PATH", "", "string"),
    ConfigEntry("YADGAR_BACKEND_LOG_FILE_PATH", "", "string"),
    ConfigEntry("YADGAR_LOG_RATE_LIMIT_ENABLED", "1", "bool"),
    ConfigEntry("YADGAR_LOG_RATE_LIMIT_TOKENS_PER_SEC", "100", "float"),
    ConfigEntry("YADGAR_LOG_RATE_LIMIT_BURST", "200", "float"),
    ConfigEntry("YADGAR_BACKEND_LOG_RATE_LIMIT_TOKENS_PER_SEC", "100", "float"),
    ConfigEntry("YADGAR_BACKEND_LOG_RATE_LIMIT_BURST", "200", "float"),
    # ── Rate limits ──────────────────────────────────────────────────────────
    ConfigEntry("YADGAR_AUTO_CAPTURE_RATE_LIMIT", "30", "int"),
    # ── Feature flags ────────────────────────────────────────────────────────
    ConfigEntry("YADGAR_CONFLICT_RESOLVER", "off", "string"),
    ConfigEntry("YADGAR_PROFILE", "full", "string"),
    ConfigEntry("YADGAR_METRICS_ENABLED", "1", "bool"),
    ConfigEntry("YADGAR_ALLOWED_ORIGINS", "", "string"),
    # ── Daemon / container lifecycle ─────────────────────────────────────────
    ConfigEntry("YADGAR_DAEMON_CHECK_INTERVAL", "5", "int"),
    ConfigEntry("YADGAR_CONTAINER", "yadgar", "string"),
    ConfigEntry("YADGAR_IMAGE", "docker.io/openfantasy/yadgar:latest", "string"),
    ConfigEntry("YADGAR_VOLUME", "yadgar-data", "string"),
    ConfigEntry("YADGAR_DEV_CONTAINER", "yadgar-dev", "string"),
    ConfigEntry("YADGAR_DEV_IMAGE", "yadgar-dev", "string"),
    ConfigEntry("YADGAR_DEV_VOLUME", "yadgar-dev-data", "string"),
    ConfigEntry("YADGAR_BACKEND_CONTAINER", "yadgar-backend", "string"),
    ConfigEntry("YADGAR_BACKEND_IMAGE", "docker.io/openfantasy/yadgar-backend:latest", "string"),
    ConfigEntry("YADGAR_BACKEND_VOLUME", "yadgar-backend-data", "string"),
    ConfigEntry("YADGAR_DOCKERHUB_USER", "looseking", "string"),
    ConfigEntry("YADGAR_IN_CONTAINER", "0", "bool"),
    # ── Backend URLs ─────────────────────────────────────────────────────────
    ConfigEntry("YADGAR_BACKEND_EMBED_URL", "", "string"),
    ConfigEntry("YADGAR_BACKEND_METRICS_URL", "", "string"),
    # ── Backup retention ─────────────────────────────────────────────────────
    ConfigEntry("YADGAR_BACKUP_RETENTION", "3", "int"),
    # ── Vacuum trigger file ──────────────────────────────────────────────────
    ConfigEntry("YADGAR_VACUUM_TRIGGER_PATH", "/data/triggers/vacuum_requested", "string"),
    # ── Sensitive-job lock + signal drain (v5.69 P3) ─────────────────────────
    ConfigEntry("YADGAR_SENSITIVE_LOCK_TTL_SEC", "7200", "int"),
    ConfigEntry("YADGAR_SENSITIVE_DRAIN_TIMEOUT_SEC", "300.0", "float"),
    # ── Vacuum / DB credentials ──────────────────────────────────────────────
    ConfigEntry("YADGAR_RW_USER", "yadgar-rw", "string"),
    ConfigEntry("YADGAR_RW_PASS", "", "string", redact=True),
    ConfigEntry("YADGAR_RO_USER", "yadgar-ro", "string"),
    ConfigEntry("YADGAR_RO_PASS", "", "string", redact=True),
    # ── SurrealDB auth aliases (non-YADGAR-prefixed) ─────────────────────────
    ConfigEntry("SURREAL_USER", "root", "string"),
    ConfigEntry("SURREAL_PASS", "root", "string", redact=True),
    # ── Shutdown / ASGI ──────────────────────────────────────────────────────
    ConfigEntry("YADGAR_ASGI_SHUTDOWN_TIMEOUT_SEC", "5", "int"),
    # ── Viz ──────────────────────────────────────────────────────────────────
    ConfigEntry("YADGAR_VIZ_PROXY", "1", "bool"),
    ConfigEntry("YADGAR_VIZ_HEALTH_REFRESH_SEC", "5.0", "float"),
    # ── Viz knobs (v5.11.0) ──────────────────────────────────────────────────
    ConfigEntry("YADGAR_VIZ_NODE_SIZE_3D", "8.0", "float"),
    ConfigEntry("YADGAR_VIZ_NODE_SIZE_2D", "4.0", "float"),
    ConfigEntry("YADGAR_VIZ_HEAT_HUE_START", "240", "int"),
    ConfigEntry("YADGAR_VIZ_HEAT_HUE_END", "0", "int"),
    ConfigEntry("YADGAR_VIZ_HEAT_SAT_BASE", "60", "int"),
    ConfigEntry("YADGAR_VIZ_HEAT_SAT_GAIN", "30", "int"),
    ConfigEntry("YADGAR_VIZ_HEAT_LIGHT_BASE", "40", "int"),
    ConfigEntry("YADGAR_VIZ_HEAT_LIGHT_GAIN", "20", "int"),
    ConfigEntry("YADGAR_VIZ_CAT_COLOR_ARCHITECTURE", "#58a6ff", "string"),
    ConfigEntry("YADGAR_VIZ_CAT_COLOR_DECISION", "#ffa657", "string"),
    ConfigEntry("YADGAR_VIZ_CAT_COLOR_PATTERN", "#3fb950", "string"),
    ConfigEntry("YADGAR_VIZ_CAT_COLOR_DEBUGGING", "#f85149", "string"),
    ConfigEntry("YADGAR_VIZ_CAT_COLOR_REFERENCE", "#8b949e", "string"),
    ConfigEntry("YADGAR_VIZ_CAT_COLOR_CONVENTION", "#d2a8ff", "string"),
    ConfigEntry("YADGAR_VIZ_CAT_COLOR_FACT", "#a5d6ff", "string"),
    ConfigEntry("YADGAR_VIZ_CAT_COLOR_ANALYSIS", "#d29922", "string"),
    ConfigEntry("YADGAR_VIZ_EDGE_COLOR_SEMANTIC", "#1f6feb", "string"),
    ConfigEntry("YADGAR_VIZ_EDGE_COLOR_TEMPORAL", "#6e40c9", "string"),
    ConfigEntry("YADGAR_VIZ_EDGE_COLOR_TRANSITION", "#3fb950", "string"),
    ConfigEntry("YADGAR_VIZ_EDGE_COLOR_WIKI_CROSSREF", "#d2a8ff", "string"),
    ConfigEntry("YADGAR_VIZ_EDGE_COLOR_MEMORY_WIKI", "#ffa657", "string"),
    ConfigEntry("YADGAR_VIZ_EDGE_WIDTH_3D_MULTIPLIER", "1.8", "float"),
    ConfigEntry("YADGAR_VIZ_EDGE_ARROW_LEN", "5", "int"),
    ConfigEntry("YADGAR_VIZ_EDGE_OPACITY", "0.9", "float"),
    ConfigEntry("YADGAR_VIZ_EDGE_VARIANT", "C", "string"),
    ConfigEntry("YADGAR_VIZ_WIKI_SHAPE", "octahedron", "string"),
    ConfigEntry("YADGAR_VIZ_PHYSICS_CHARGE_STRENGTH", "-18.0", "float"),
    ConfigEntry("YADGAR_VIZ_PHYSICS_LINK_DISTANCE_2D", "30.0", "float"),
    ConfigEntry("YADGAR_VIZ_PHYSICS_LINK_DISTANCE_3D", "36.0", "float"),
    ConfigEntry("YADGAR_VIZ_LAYOUT_ZOOM_FIT_TICK", "80", "int"),
    ConfigEntry("YADGAR_VIZ_LAYOUT_ZOOM_FIT_PADDING", "50", "int"),
    ConfigEntry("YADGAR_VIZ_LAYOUT_ZOOM_FIT_TRANSITION_MS", "800", "int"),
    ConfigEntry("YADGAR_VIZ_SEARCH_MATCH_COLOR", "#ffffff", "string"),
    ConfigEntry("YADGAR_VIZ_SEARCH_PINNED_COLOR", "#ffd700", "string"),
    ConfigEntry("YADGAR_VIZ_SEARCH_DIM_OPACITY", "0.18", "float"),
    # ── Graph node caps (v5.88; 0 or -1 = unlimited) ─────────────────────────
    ConfigEntry("YADGAR_VIZ_MAX_MEMORIES", "500", "int"),
    ConfigEntry("YADGAR_VIZ_MAX_WIKI", "200", "int"),
    ConfigEntry("YADGAR_VIZ_MAX_ENTITIES", "2000", "int"),
    # ── Precomputed server-side graph layout (v5.88; default OFF) ────────────
    ConfigEntry("YADGAR_VIZ_PRECOMPUTED_LAYOUT_ENABLED", "false", "bool"),
    ConfigEntry("YADGAR_VIZ_LAYOUT_ITERATIONS", "50", "int"),
    # ── DB path (embedded mode) ──────────────────────────────────────────────
    ConfigEntry("YADGAR_DB_PATH", str(_paths.DB_PATH), "string"),
    # ── OTLP / Tempo exporter ────────────────────────────────────────────────
    ConfigEntry("YADGAR_OTLP_ENDPOINT", "", "string"),
    ConfigEntry("YADGAR_OTLP_HEADERS", "", "string"),
    ConfigEntry("YADGAR_OTLP_TIMEOUT_SEC", "10", "int"),
    ConfigEntry("YADGAR_OTLP_INSECURE", "1", "bool"),
    # ── Memory block caps (v5.35.1, I25) ────────────────────────────────────
    ConfigEntry("YADGAR_MEMORY_BLOCK_MAX_PER_SCOPE", "10", "int"),
    ConfigEntry("YADGAR_MEMORY_BLOCK_DEFAULT_CHAR_LIMIT", "2000", "int"),
    ConfigEntry("YADGAR_MEMORY_BLOCK_HARD_CHAR_LIMIT", "8000", "int"),
    ConfigEntry("YADGAR_MEMORY_BLOCK_TOTAL_BUDGET_CHARS", "12000", "int"),
    # ── Backend: /admin/dbsize cache (v5.3.0) ────────────────────────────────
    ConfigEntry("YADGAR_DBSIZE_CACHE_TTL_SEC", "60", "int"),
    # ── Backend: restart attribution marker path (v5.3.0) ────────────────────
    ConfigEntry("YADGAR_SHUTDOWN_MARKER_PATH", "/data/.shutdown_clean", "string"),
    # ── Config file path override (v5.7.10) ──────────────────────────────────
    # Container deployments set this to /data/config.yaml so the yaml loader
    # reads from the bind-mounted /data volume instead of /root/.yadgar/
    ConfigEntry("YADGAR_CONFIG_FILE", "", "string"),
    # ── project_brief thresholds + anchor cap (v5.7.12) ─────────────────────
    ConfigEntry("YADGAR_ACTIVE_WORK_STALE_HOURS", "24.0", "float"),
    ConfigEntry("YADGAR_CHECKPOINT_STALE_HOURS", "24.0", "float"),
    ConfigEntry("YADGAR_PROJECT_BRIEF_MAX_ANCHORS", "12", "int"),
    # ── project_brief soft warning tier + watchdog opt-in (v5.10.1) ─────────
    ConfigEntry("YADGAR_ACTIVE_WORK_WARN_HOURS", "12.0", "float"),
    ConfigEntry("YADGAR_CHECKPOINT_WARN_HOURS", "12.0", "float"),
    ConfigEntry("YADGAR_AUTO_REFRESH_ACTIVE_WORK", "false", "bool"),
    ConfigEntry("YADGAR_SIGNALS_TOKEN_BUDGET_SOFT", "500", "int"),
    # ── v5.84.0 car #12: ADR nudge threshold ────────────────────────────────────
    ConfigEntry("YADGAR_ADR_DUE_WARN_HOURS", "12.0", "float"),
    # ── v5.89 #69: dispatch-prelude read-side nudge threshold ────────────────────
    ConfigEntry("YADGAR_DISPATCH_PRELUDE_DUE_WARN_HOURS", "12.0", "float"),
    # ── anchor hygiene TTL knobs (v5.8.0) ────────────────────────────────────
    ConfigEntry("YADGAR_ANCHOR_CONDITIONAL_TTL_DAYS", "90", "int"),
    ConfigEntry("YADGAR_ANCHOR_EPHEMERAL_TTL_DAYS", "14", "int"),
    ConfigEntry("YADGAR_ANCHOR_SEMANTIC_IMMORTAL_REQUIRES_REASON", "true", "bool"),
    # ── anchor hygiene signals + recommended_actions knobs (v5.8.0 PR-B) ──
    ConfigEntry("YADGAR_ANCHOR_REDUNDANCY_COSINE", "0.92", "float"),
    ConfigEntry("YADGAR_ANCHOR_PROMOTE_WORDS", "500", "int"),
    ConfigEntry("YADGAR_ANCHOR_PROMOTE_HEADERS", "2", "int"),
    ConfigEntry("YADGAR_ANCHOR_AUDIT_THRESHOLD", "15", "int"),
    # ── anchor audit pass knobs (v5.9.0) ─────────────────────────────────────
    ConfigEntry("YADGAR_ANCHOR_AUDIT_CONSOLIDATION_ENABLED", "true", "bool"),
    ConfigEntry("YADGAR_ANCHOR_AUDIT_MAX_ACTIONS_PER_RUN", "20", "int"),
    ConfigEntry("YADGAR_ANCHOR_AUDIT_HISTORY_RETENTION_DAYS", "30", "int"),
    # ── cross-project anchor dedup knob (v5.21.0) ───────────────────────────
    ConfigEntry("YADGAR_ANCHOR_CROSS_PROJECT_COSINE", "0.95", "float"),
    # ── SESSION_END_CAPTURE sentinel-marker pattern (v5.10.6) ───────────────
    ConfigEntry("YADGAR_SESSION_END_CAPTURE_ENABLED", "true", "bool"),
    ConfigEntry("YADGAR_SESSION_END_RETENTION_DAYS", "30", "int"),
    ConfigEntry("YADGAR_SESSION_END_SNIPPET_TURNS", "5", "int"),
    ConfigEntry("YADGAR_SESSION_END_MIN_MESSAGES", "2", "int"),
    # ── CPU burst detection knobs (v5.15.0 D1) ──────────────────────────────
    ConfigEntry("YADGAR_PHASE_DURATION_WARN_MS", "60000", "int"),
    # ── Backend hot-path cache knobs (backend v5.4.0) ────────────────────────
    ConfigEntry("YADGAR_CE_CACHE_ENABLED", "true", "bool"),
    ConfigEntry("YADGAR_EMBED_CACHE_ENABLED", "true", "bool"),
    ConfigEntry("YADGAR_CE_CACHE_MAX_ENTRIES", "100000", "int"),
    ConfigEntry("YADGAR_EMBED_CACHE_MAX_ENTRIES", "100000", "int"),
    ConfigEntry("YADGAR_CACHE_SNAPSHOT_INTERVAL_SEC", "600", "int"),
    ConfigEntry("YADGAR_CACHE_SNAPSHOT_DIR", "/data/cache", "string"),
    # backend 5.17.0 (Car 0) — unified backend Cache byte budget as % of RAM.
    ConfigEntry("YADGAR_BACKEND_CACHE_RAM_PCT", "10.0", "float"),
    # core 5.112.0 (#49) — unified core Cache byte budget as % of core container RAM.
    ConfigEntry("YADGAR_CORE_CACHE_RAM_PCT", "10.0", "float"),
    # ── v5.41.2 wiki write wait timeout ─────────────────────────────────────
    ConfigEntry("YADGAR_WIKI_WRITE_WAIT_TIMEOUT_SECONDS", "5.0", "float"),
    # ── v5.39.0 wiki similarity gate knobs ───────────────────────────────────
    ConfigEntry("YADGAR_WIKI_SIM_GATE_ENABLED", "true", "bool"),
    ConfigEntry("YADGAR_WIKI_SIM_CONTENT_THRESHOLD", "0.80", "float"),
    ConfigEntry("YADGAR_WIKI_SIM_TITLE_THRESHOLD", "0.85", "float"),
    ConfigEntry("YADGAR_WIKI_SIM_MODE", "hard", "string"),
    ConfigEntry("YADGAR_WIKI_SIM_TOP_K", "5", "int"),
    # ── v5.42.1 embed failure behaviour knob ─────────────────────────────────
    ConfigEntry("YADGAR_WIKI_EMBED_FAILURE_BLOCKS_WRITE", "false", "bool"),
    # ── v5.42.6 enforcement knobs ─────────────────────────────────────────────
    ConfigEntry("YADGAR_DIRECTORY_ENFORCEMENT", "true", "bool"),
    ConfigEntry("YADGAR_BRANCH_ENFORCEMENT", "true", "bool"),
    # ── v5.48.0 update mechanism knobs ────────────────────────────────────────
    ConfigEntry("YADGAR_UPDATE_CHECK_ON_START", "false", "bool"),
    ConfigEntry("YADGAR_UPDATE_CHECK_TIMEOUT_SECONDS", "5", "int"),
    ConfigEntry("YADGAR_UPDATE_PYPI_URL", "https://pypi.org/pypi/yadgar/json", "string"),
    ConfigEntry("YADGAR_UPDATE_USER_AGENT_TEMPLATE", "yadgar/{version}", "string"),
    ConfigEntry("YADGAR_UPDATE_DEBUG_APIS_ENABLED", "off", "string"),
    # ── v5.50.2 control API gate ──────────────────────────────────────────────
    ConfigEntry("YADGAR_DEBUG_APIS_ENABLED", "false", "bool"),
    # ── cold-memory retention DRY-RUN visibility (#29) ───────────────────────
    ConfigEntry("YADGAR_COLD_MEMORY_RETENTION_DAYS", "90", "int"),
    ConfigEntry("YADGAR_COLD_MEMORY_PURGE_ENABLED", "false", "bool"),
    ConfigEntry("YADGAR_COLD_MEMORY_PURGE_DRY_RUN", "true", "bool"),
    # ── v5.49.0 memory archive retention knobs ────────────────────────────────
    ConfigEntry("YADGAR_MEMORY_ARCHIVE_RETENTION_DAYS", "90", "int"),
    ConfigEntry("YADGAR_MEMORY_ARCHIVE_RETENTION_CIRCUIT_BREAKER", "500", "int"),
    ConfigEntry("YADGAR_MEMORY_ARCHIVE_RETENTION_THRASH_GUARD_DAYS", "7", "int"),
    # ── v5.49.0 upgrade snapshot retention ───────────────────────────────────
    ConfigEntry("YADGAR_UPDATE_SNAPSHOT_RETENTION", "3", "int"),
    # ── v5.49.0 Phase 9 orchestrator knobs ───────────────────────────────────
    ConfigEntry("YADGAR_UPDATE_INSTALL_ENABLED", "false", "bool"),
    ConfigEntry("YADGAR_UPDATE_LOCK_MAX_AGE_SECONDS", "3600", "int"),
    # ── backend v5.5.0 model preload warm-up ─────────────────────────────────
    ConfigEntry("YADGAR_MODEL_PRELOAD", "true", "bool"),
    ConfigEntry("YADGAR_MODEL_PRELOAD_DELAY_SEC", "10", "int"),
    # ── v5.51.0 hook recall latency budget + fast profile tuning + stats cache ─
    ConfigEntry("YADGAR_HOOK_RECALL_TIMEOUT_S", "2.0", "float"),
    ConfigEntry("YADGAR_HOOK_RECALL_POOL_WORKERS", "1", "int"),
    ConfigEntry("YADGAR_FAST_PROFILE_CANDIDATE_MULTIPLIER", "3", "int"),
    ConfigEntry("YADGAR_STATS_CACHE_TTL_S", "5", "int"),
    # v5.53.1: stale wiki count cache TTL
    ConfigEntry("YADGAR_STALE_COUNT_CACHE_TTL_S", "300", "int"),
    # v5.54.1: precomputed graph prior weight (additive boost in all profiles)
    ConfigEntry("YADGAR_WRRF_GRAPH_PRIOR_WEIGHT", "0.2", "float"),
    # v5.54.2: precomputed co-recall (transition-edge) prior weight (additive boost in all profiles)
    ConfigEntry("YADGAR_WRRF_COFIRE_PRIOR_WEIGHT", "0.15", "float"),
    # ── v6 T6 unified-scoped-recall fusion settings ─────────────────────────
    # v6 T6 Step 4: cross-type fusion per-type quotas and prior weights.
    ConfigEntry("YADGAR_RECALL_MEMORY_QUOTA", "5", "int"),
    ConfigEntry("YADGAR_RECALL_WIKI_QUOTA", "5", "int"),
    ConfigEntry("YADGAR_RECALL_MEMORY_PRIOR_WEIGHT", "0.1", "float"),
    ConfigEntry("YADGAR_RECALL_WIKI_PRIOR_WEIGHT", "0.1", "float"),
    # ── v5.62.0 recall quality floor ─────────────────────────────────────────
    ConfigEntry("YADGAR_RECALL_QUALITY_FLOOR", "0.0", "float"),
    ConfigEntry("YADGAR_ASTROCYTE_POOL_ENABLED", "true", "bool"),
    # ── COMET enrichment (RETIRED/DORMANT per ADR-0004) ───────────────────────
    # Surfaced here so /admin/config + startup.config report it disabled (BC-EN2b).
    # Default flipped True→False on retire (en2a ablation: net-negative recall).
    ConfigEntry("YADGAR_COMET_ENRICHMENT_ENABLED", "false", "bool"),
    # ── v5.73.0 surprise-gate shadow mode ────────────────────────────────────
    # Shadow threshold for auditing — memories below this are stamped would_reject=True
    # but nothing is dropped (WRITE_GATE_THRESHOLD stays 0.0).
    ConfigEntry("YADGAR_WRITE_GATE_SHADOW_THRESHOLD", "0.15", "float"),
    # ── v5.85 car #6 agent-prompt Tier-1 passive library (Phase 1) kill-gate ──
    ConfigEntry("YADGAR_AGENT_PROMPT_LIBRARY_ENABLED", "true", "bool"),
    # ── v5.86 car #1 (OT-C4) incremental similarity-linking (default OFF) ──────
    ConfigEntry("YADGAR_SIMILARITY_LINKING_INCREMENTAL_ENABLED", "false", "bool"),
    ConfigEntry("YADGAR_SIMILARITY_LINKING_RECONCILE_INTERVAL_DAYS", "7", "int"),
    # ── v5.89 #67: I25 Tier-2 grandfathered backlog drain ─────────────────────
    # Registry entries for the FIELD_META backfill (config_yaml.py). Pairs each
    # newly-documented knob with its env-source attribution so the three-way
    # ratchet (test_config_three_way_sync) covers it. Defaults mirror config.py.
    # security / bind / CORS
    ConfigEntry("YADGAR_MAX_HASH_BYTES", "10485760", "int"),
    # core / batch / db-size / invariants / wiki-prefix / shutdown
    ConfigEntry("YADGAR_MAX_BATCH_STATEMENTS", "500", "int"),
    ConfigEntry("YADGAR_MAX_BATCH_BYTES", "1000000", "int"),
    ConfigEntry("YADGAR_MAX_CAUSED_BY_ROWS", "100000", "int"),
    ConfigEntry("YADGAR_DB_SIZE_WARNING_BYTES", "1073741824", "int"),
    ConfigEntry("YADGAR_CHECK_INVARIANTS_QUERY_TIMEOUT_SECONDS", "60", "int"),
    ConfigEntry("YADGAR_WIKI_SLUG_PREFIX", "", "string"),
    # daemon / consolidation similarity-linking
    ConfigEntry("YADGAR_SIMILARITY_LINK_THRESHOLD", "0.78", "float"),
    ConfigEntry("YADGAR_MAX_SIMILARITY_LINKS_PER_MEMORY", "15", "int"),
    ConfigEntry("YADGAR_SIMILARITY_MATRIX_MAX_CANDIDATES", "4000", "int"),
    ConfigEntry("YADGAR_CLS_PATTERN_MAX_CANDIDATES", "2000", "int"),
    # memory_lifecycle
    ConfigEntry("YADGAR_PREDICTIVE_CODING_ENTITY_TTL_SECONDS", "300", "int"),
    ConfigEntry("YADGAR_REINJECT_ON_WRITE", "false", "bool"),
    # thermodynamics / retrieval boosts
    ConfigEntry("YADGAR_RECALL_BOOST", "0.05", "float"),
    ConfigEntry("YADGAR_BRANCH_BOOST_WEIGHT", "0.2", "float"),
    ConfigEntry("YADGAR_POSTMORTEM_BOOST_FACTOR", "0.3", "float"),
    ConfigEntry(
        "YADGAR_POSTMORTEM_BOOST_KEYWORDS",
        "deploy,push,merge,restart,vacuum,rollback,upgrade,migrate,bump,release",
        "string",
    ),
    ConfigEntry("YADGAR_RETRIEVAL_PROFILE", "balanced", "string"),
    ConfigEntry("YADGAR_FANOUT_BOOST_SCOPE", "scoped", "string"),
    ConfigEntry("YADGAR_HEAVY_RERANK_ENABLED", "true", "bool"),
    # neuromorphic
    ConfigEntry("YADGAR_HOPFIELD_BETA", "8.0", "float"),
    ConfigEntry("YADGAR_HOPFIELD_MAX_PATTERNS", "5000", "int"),
    ConfigEntry("YADGAR_SR_DISCOUNT", "0.9", "float"),
    ConfigEntry("YADGAR_SR_UPDATE_RATE", "0.1", "float"),
    # embedding / profiles
    ConfigEntry("YADGAR_IMPLICIT_EMBEDDING_MODEL", "", "string"),
    ConfigEntry("YADGAR_PROFILE_SEARCH_WEIGHT", "1.0", "float"),
    # project_brief
    ConfigEntry("YADGAR_BRIEF_MODE_DEFAULT", "catalog", "string"),
    ConfigEntry("YADGAR_PROJECT_INIT_CAP_CHARS", "2000", "int"),
    # backend timeouts
    ConfigEntry("YADGAR_BACKEND_HTTP_TIMEOUT_SEC", "5", "int"),
    ConfigEntry("YADGAR_BACKEND_IMPORT_TIMEOUT_SEC", "300", "int"),
    ConfigEntry("YADGAR_MIGRATION_HTTP_TIMEOUT_SEC", "30", "int"),
    ConfigEntry("YADGAR_RERANK_BACKEND_TIMEOUT_SEC", "90", "int"),
    # circuit breaker + rerank concurrency
    ConfigEntry("YADGAR_CIRCUIT_BREAKER_ENABLED", "true", "bool"),
    ConfigEntry("YADGAR_CIRCUIT_BREAKER_FAILURE_THRESHOLD", "3", "int"),
    ConfigEntry("YADGAR_CIRCUIT_BREAKER_OPEN_DURATION_SEC", "60", "int"),
    ConfigEntry("YADGAR_CIRCUIT_BREAKER_PROBE_TIMEOUT_SEC", "2.0", "float"),
    ConfigEntry("YADGAR_CIRCUIT_BREAKER_MAX_OPEN_DURATION_SEC", "600.0", "float"),
    ConfigEntry("YADGAR_CIRCUIT_BREAKER_BACKOFF_FACTOR", "2.0", "float"),
    ConfigEntry("YADGAR_RERANK_MAX_CONCURRENCY", "8", "int"),
    ConfigEntry("YADGAR_RERANK_SEMAPHORE_ACQUIRE_TIMEOUT_SEC", "2.0", "float"),
    # ── Fix A (daemon-offload-A): tool-body offload off the asyncio loop ─────────
    ConfigEntry("YADGAR_OFFLOAD_TOOLS", "false", "bool"),
    ConfigEntry("YADGAR_TOOL_POOL_WORKERS", "2", "int"),  # v5.95: 8→2
    ConfigEntry(
        "YADGAR_RECALL_HEAVY_CONCURRENCY", "1", "int"
    ),  # v5.95: 3→1 (must be < TOOL_POOL_WORKERS=2)
    ConfigEntry("YADGAR_RERANK_GATE_ACQUIRE_TIMEOUT_SEC", "2.0", "float"),
    ConfigEntry("YADGAR_TOOL_TIMEOUT_SEC", "95.0", "float"),
    ConfigEntry("YADGAR_TOOL_SATURATION_GRACE_SEC", "120.0", "float"),
    ConfigEntry("YADGAR_HEALTH_READINESS_FAIL_THRESHOLD", "3", "int"),
    # v5.95 config-integrity Phase 4 — hot-path literals promoted to knobs
    ConfigEntry("YADGAR_RERANKER_IDLE_UNLOAD_SEC", "600.0", "float"),
    ConfigEntry("YADGAR_RERANKER_IDLE_CHECK_INTERVAL_SEC", "60", "int"),
    ConfigEntry("YADGAR_HEALTH_HANDLER_TIMEOUT_SEC", "3.0", "float"),
    ConfigEntry("YADGAR_HEALTH_PROBE_TIMEOUT_SEC", "2.0", "float"),
    ConfigEntry("YADGAR_VACUUM_AUTO_COOLDOWN_HOURS", "6.0", "float"),
    # write queue / DLQ
    ConfigEntry("YADGAR_QUEUE_DRAIN_INTERVAL", "30", "int"),
    ConfigEntry("YADGAR_QUEUE_MAX_PERMANENT_ATTEMPTS", "3", "int"),
    ConfigEntry("YADGAR_QUEUE_MAX_TRANSIENT_ATTEMPTS", "20", "int"),
    ConfigEntry("YADGAR_QUEUE_BACKOFF_BASE_S", "30", "int"),
    ConfigEntry("YADGAR_QUEUE_BACKOFF_MAX_S", "3600", "int"),
    ConfigEntry("YADGAR_QUEUE_DLQ_RETENTION_DAYS", "90", "int"),
    # table / memory retention windows
    ConfigEntry("YADGAR_ACTION_LOG_RETENTION_DAYS", "7", "int"),
    ConfigEntry("YADGAR_EPISODE_RETENTION_DAYS", "14", "int"),
    ConfigEntry("YADGAR_ASTROCYTE_PROCESS_RETENTION_DAYS", "7", "int"),
    ConfigEntry("YADGAR_MEMORY_CLUSTER_RETENTION_DAYS", "30", "int"),
    ConfigEntry("YADGAR_DERIVED_BELIEF_RETENTION_DAYS", "30", "int"),
    ConfigEntry("YADGAR_PROSPECTIVE_MEMORY_RETENTION_DAYS", "30", "int"),
    ConfigEntry("YADGAR_NARRATIVE_ENTRY_RETENTION_DAYS", "90", "int"),
    ConfigEntry("YADGAR_ACTION_STREAM_MAX_AGE_DAYS", "14", "int"),
    ConfigEntry("YADGAR_AUTO_GENERATED_MEMORY_MAX_AGE_DAYS", "30", "int"),
    ConfigEntry("YADGAR_AUTO_ABSTRACTED_MEMORY_MAX_AGE_DAYS", "30", "int"),
    ConfigEntry("YADGAR_DREAM_INSIGHT_MAX_AGE_DAYS", "21", "int"),
    # vacuum
    ConfigEntry("YADGAR_VACUUM_SNAPSHOT_RETENTION", "3", "int"),
    ConfigEntry("YADGAR_VACUUM_AUTO_ENABLED", "true", "bool"),
    ConfigEntry("YADGAR_VACUUM_AUTO_THRESHOLD_BYTES", "2147483648", "int"),
    ConfigEntry("YADGAR_VACUUM_AUTO_WINDOW_START", "19:00", "string"),
    ConfigEntry("YADGAR_VACUUM_AUTO_WINDOW_END", "23:00", "string"),
]


def list_config() -> list[ConfigEntry]:
    """Return all registered config entries, sorted alphabetically by name."""
    return sorted(_REGISTRY, key=lambda e: e.name)


def build_config_table() -> list[dict]:
    """Return serialised config table (list of dicts), sorted by name."""
    return [e.as_dict() for e in list_config()]


# ---------------------------------------------------------------------------
# Gauge helper
# ---------------------------------------------------------------------------


@observe(tier="stage")
def _set_config_gauges() -> None:
    """Set yadgar_config_value{name} for every int/float/bool entry.

    String entries are skipped — cardinality / plot-ability concern.
    Called at startup and on every GET /admin/config so live mutations
    (if any) are reflected.
    """
    try:
        from yadgar._shared.metrics import yadgar_config_value  # noqa: PLC0415
    except Exception:
        return  # metrics not available (embed service, tests without metrics)

    for entry in list_config():
        if entry.kind not in ("int", "float", "bool"):
            continue
        if entry._should_redact():
            continue
        raw = entry._raw_value()
        try:
            if entry.kind == "bool":
                numeric = 1.0 if raw.lower() in ("1", "true", "yes", "on") else 0.0
            else:
                numeric = float(raw)
            yadgar_config_value.labels(name=entry.name).set(numeric)
        except (ValueError, TypeError):  # fmt: skip
            pass  # bad env value — skip rather than crash


# ---------------------------------------------------------------------------
# Startup log helper
# ---------------------------------------------------------------------------


@observe(tier="boundary")
def emit_startup_config_log() -> None:
    """Emit a single INFO-level structured log line with the full config table.

    Log line tag: event="startup.config". Secrets still redacted.
    Uses the yadgar logger so it propagates through the configured JSON formatter.
    """
    table = build_config_table()
    logger.info(
        "startup.config",
        extra={
            "event": "startup.config",
            "config": table,
        },
    )


@observe(tier="hot")
def warn_comet_dormant(settings) -> None:
    """BC-EN2b: emit exactly ONE startup warning when COMET enrichment is disabled.

    COMET enrichment was retired to dormant per ADR-0004 (en2a ablation: net-negative
    recall, prohibitive cost). The flag defaults to False; the code is retained but
    inert. When disabled we announce the dormant state once at startup so operators
    know the (still-present) COMET branch is intentionally off. When someone re-enables
    it, this goes silent — re-enabling is their explicit choice and the warning would
    be noise.

    "Exactly once" is guaranteed by the single call site (lifecycle.main), NOT an
    in-function guard — a module-level guard would leak across tests. Pure + hermetic:
    takes a Settings instance, reads no env, loads no model.
    """
    if not settings.COMET_ENRICHMENT_ENABLED:
        logger.warning(
            "COMET enrichment is disabled (retired to dormant per ADR-0004 — "
            "net-negative recall in the en2a ablation). The COMET code is retained "
            "but inert; re-validate against the ablation before re-enabling."
        )
