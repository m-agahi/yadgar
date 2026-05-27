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
    ConfigEntry("YADGAR_DATA_DIR", "~/.yadgar", "string"),
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
    # ── Logging ─────────────────────────────────────────────────────────────
    ConfigEntry("YADGAR_BACKEND_LOG_LEVEL", "warn", "string"),
    ConfigEntry("YADGAR_CORE_LOG_LEVEL", "WARNING", "string"),
    ConfigEntry("YADGAR_LOG_FORMAT", "json", "string"),
    ConfigEntry("YADGAR_LOG_DIR", "~/.yadgar/logs", "string"),
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
    ConfigEntry("YADGAR_IMAGE", "looseking/yadgar:latest", "string"),
    ConfigEntry("YADGAR_VOLUME", "yadgar-data", "string"),
    ConfigEntry("YADGAR_DEV_CONTAINER", "yadgar-dev", "string"),
    ConfigEntry("YADGAR_DEV_IMAGE", "yadgar-dev", "string"),
    ConfigEntry("YADGAR_DEV_VOLUME", "yadgar-dev-data", "string"),
    ConfigEntry("YADGAR_BACKEND_CONTAINER", "yadgar-backend", "string"),
    ConfigEntry("YADGAR_BACKEND_IMAGE", "looseking/yadgar-backend:latest", "string"),
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
    # ── DB path (embedded mode) ──────────────────────────────────────────────
    ConfigEntry("YADGAR_DB_PATH", "~/.yadgar/surreal_db", "string"),
    # ── OTLP / Tempo exporter ────────────────────────────────────────────────
    ConfigEntry("YADGAR_OTLP_ENDPOINT", "", "string"),
    ConfigEntry("YADGAR_OTLP_HEADERS", "", "string"),
    ConfigEntry("YADGAR_OTLP_TIMEOUT_SEC", "10", "int"),
    ConfigEntry("YADGAR_OTLP_INSECURE", "1", "bool"),
    # ── Backend: /admin/dbsize cache (v5.3.0) ────────────────────────────────
    ConfigEntry("YADGAR_DBSIZE_CACHE_TTL_SEC", "60", "int"),
    # ── Backend: restart attribution marker path (v5.3.0) ────────────────────
    ConfigEntry("YADGAR_SHUTDOWN_MARKER_PATH", "/data/.shutdown_clean", "string"),
    # ── Config file path override (v5.7.10) ──────────────────────────────────
    # Container deployments set this to /data/config.yaml so the yaml loader
    # reads from the bind-mounted /data volume instead of /root/.yadgar/
    ConfigEntry("YADGAR_CONFIG_FILE", "", "string"),
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
        except ValueError, TypeError:
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
