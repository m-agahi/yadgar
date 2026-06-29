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

import yadgar.paths as _paths

logger = logging.getLogger(__name__)

_REDACT_RE = re.compile(r"(secret|token|key|password|auth)", re.IGNORECASE)

_REDACTED = "<redacted>"


@dataclass
class ConfigEntry:
    """One registered env knob."""

    name: str
    default: str
    kind: str  # "int" | "float" | "bool" | "string"
    redact: bool = False

    def source(self) -> str:
        """Return 'env' if var is set in os.environ, else 'default'."""
        return "env" if self.name in os.environ else "default"

    def _raw_value(self) -> str:
        """Return resolved raw string value (env or default), without redaction."""
        return os.environ.get(self.name, self.default)

    def _should_redact(self) -> bool:
        return self.redact or bool(_REDACT_RE.search(self.name))

    def value(self) -> str:
        """Return value string, redacted if required."""
        if self._should_redact():
            return _REDACTED
        return _raw_value_str(self.name, self.default)

    def as_dict(self) -> dict:
        """Serialise to the /admin/config JSON schema."""
        return {
            "name": self.name,
            "value": self.value(),
            "source": self.source(),
            "kind": self.kind,
        }


def _raw_value_str(name: str, default: str) -> str:
    """Return os.environ.get(name, default) as a string."""
    return os.environ.get(name, default)


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
    ConfigEntry("YADGAR_SIGNALS_TOKEN_BUDGET_SOFT", "400", "int"),
    # ── v5.84.0 car #12: ADR nudge threshold ────────────────────────────────────
    ConfigEntry("YADGAR_ADR_DUE_WARN_HOURS", "12.0", "float"),
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
    ConfigEntry("YADGAR_FAST_PROFILE_CANDIDATE_MULTIPLIER", "3", "int"),
    ConfigEntry("YADGAR_STATS_CACHE_TTL_S", "5", "int"),
    # v5.53.1: stale wiki count cache TTL
    ConfigEntry("YADGAR_STALE_COUNT_CACHE_TTL_S", "300", "int"),
    # v5.54.1: precomputed graph prior weight (additive boost in all profiles)
    ConfigEntry("YADGAR_WRRF_GRAPH_PRIOR_WEIGHT", "0.2", "float"),
    # v5.54.2: precomputed co-recall (transition-edge) prior weight (additive boost in all profiles)
    ConfigEntry("YADGAR_WRRF_COFIRE_PRIOR_WEIGHT", "0.15", "float"),
    # ── v6 T6 unified-scoped-recall fan-out flag + fusion settings ────────────
    ConfigEntry("YADGAR_UNIFIED_RECALL_ENABLED", "true", "bool"),
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


def _set_config_gauges() -> None:
    """Set yadgar_config_value{name} for every int/float/bool entry.

    String entries are skipped — cardinality / plot-ability concern.
    Called at startup and on every GET /admin/config so live mutations
    (if any) are reflected.
    """
    try:
        from yadgar.metrics import yadgar_config_value  # noqa: PLC0415
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
