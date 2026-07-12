"""v5.95.0 config-integrity: backend cluster knobs → resolve_knob (TDD RED→GREEN).

Tests that each knob in embed_service.py and ml_client.py respects:
  (a) env override wins
  (b) config.yaml value respected when env unset
  (c) (MODEL_IDLE_EVICTION_SECONDS only: env override; yaml test deferred until field lands)

Fixture pattern mirrors test_config_yaml_aware_source.py.
"""

from __future__ import annotations

import pytest

from yadgar._shared.config.config_registry import clear_config_caches


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch, tmp_path):
    """Point YADGAR_CONFIG_FILE at a temp file; clear caches before/after."""
    cfg = tmp_path / "yadgar-backend-integrity-test.yaml"
    monkeypatch.setenv("YADGAR_CONFIG_FILE", str(cfg))
    clear_config_caches()
    yield
    clear_config_caches()


def _write_yaml(tmp_path, body: str) -> None:
    from yadgar._shared.config.config_yaml import get_config_path

    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    clear_config_caches()


# ---------------------------------------------------------------------------
# 1. _ce_cache_enabled
# ---------------------------------------------------------------------------


def test_ce_cache_enabled_env_override(monkeypatch):
    monkeypatch.setenv("YADGAR_CE_CACHE_ENABLED", "0")
    from yadgar.backend.embed_service import _ce_cache_enabled

    assert _ce_cache_enabled() is False


def test_ce_cache_enabled_yaml(monkeypatch, tmp_path):
    monkeypatch.delenv("YADGAR_CE_CACHE_ENABLED", raising=False)
    _write_yaml(tmp_path, "ce_cache_enabled: false\n")
    from yadgar.backend.embed_service import _ce_cache_enabled

    assert _ce_cache_enabled() is False


# ---------------------------------------------------------------------------
# 2. _embed_cache_enabled
# ---------------------------------------------------------------------------


def test_embed_cache_enabled_env_override(monkeypatch):
    monkeypatch.setenv("YADGAR_EMBED_CACHE_ENABLED", "false")
    from yadgar.backend.embed_service import _embed_cache_enabled

    assert _embed_cache_enabled() is False


def test_embed_cache_enabled_yaml(monkeypatch, tmp_path):
    monkeypatch.delenv("YADGAR_EMBED_CACHE_ENABLED", raising=False)
    _write_yaml(tmp_path, "embed_cache_enabled: false\n")
    from yadgar.backend.embed_service import _embed_cache_enabled

    assert _embed_cache_enabled() is False


# ---------------------------------------------------------------------------
# 3. _ce_cache_max_entries
# ---------------------------------------------------------------------------


def test_ce_cache_max_entries_env_override(monkeypatch):
    monkeypatch.setenv("YADGAR_CE_CACHE_MAX_ENTRIES", "42")
    from yadgar.backend.embed_service import _ce_cache_max_entries

    assert _ce_cache_max_entries() == 42


def test_ce_cache_max_entries_yaml(monkeypatch, tmp_path):
    monkeypatch.delenv("YADGAR_CE_CACHE_MAX_ENTRIES", raising=False)
    _write_yaml(tmp_path, "ce_cache_max_entries: 5\n")
    from yadgar.backend.embed_service import _ce_cache_max_entries

    assert _ce_cache_max_entries() == 5


# ---------------------------------------------------------------------------
# 4. _embed_cache_max_entries
# ---------------------------------------------------------------------------


def test_embed_cache_max_entries_env_override(monkeypatch):
    monkeypatch.setenv("YADGAR_EMBED_CACHE_MAX_ENTRIES", "99")
    from yadgar.backend.embed_service import _embed_cache_max_entries

    assert _embed_cache_max_entries() == 99


def test_embed_cache_max_entries_yaml(monkeypatch, tmp_path):
    monkeypatch.delenv("YADGAR_EMBED_CACHE_MAX_ENTRIES", raising=False)
    _write_yaml(tmp_path, "embed_cache_max_entries: 7\n")
    from yadgar.backend.embed_service import _embed_cache_max_entries

    assert _embed_cache_max_entries() == 7


# ---------------------------------------------------------------------------
# 5. _cache_snapshot_dir
# ---------------------------------------------------------------------------


def test_cache_snapshot_dir_env_override(monkeypatch):
    monkeypatch.setenv("YADGAR_CACHE_SNAPSHOT_DIR", "/tmp/snap")
    from yadgar.backend.embed_service import _cache_snapshot_dir

    assert _cache_snapshot_dir() == "/tmp/snap"


def test_cache_snapshot_dir_yaml(monkeypatch, tmp_path):
    monkeypatch.delenv("YADGAR_CACHE_SNAPSHOT_DIR", raising=False)
    _write_yaml(tmp_path, "cache_snapshot_dir: /custom/snap\n")
    from yadgar.backend.embed_service import _cache_snapshot_dir

    assert _cache_snapshot_dir() == "/custom/snap"


# ---------------------------------------------------------------------------
# 6. _cache_snapshot_interval_sec
# ---------------------------------------------------------------------------


def test_cache_snapshot_interval_env_override(monkeypatch):
    monkeypatch.setenv("YADGAR_CACHE_SNAPSHOT_INTERVAL_SEC", "30")
    from yadgar.backend.embed_service import _cache_snapshot_interval_sec

    assert _cache_snapshot_interval_sec() == 30


def test_cache_snapshot_interval_yaml(monkeypatch, tmp_path):
    monkeypatch.delenv("YADGAR_CACHE_SNAPSHOT_INTERVAL_SEC", raising=False)
    _write_yaml(tmp_path, "cache_snapshot_interval_sec: 120\n")
    from yadgar.backend.embed_service import _cache_snapshot_interval_sec

    assert _cache_snapshot_interval_sec() == 120


# ---------------------------------------------------------------------------
# 7a. _get_embed_checkpoint_hash (EMBEDDING_MODEL, 3 sites via same accessor)
# ---------------------------------------------------------------------------


def test_get_embed_checkpoint_hash_env_override(monkeypatch):
    import hashlib

    monkeypatch.setenv("YADGAR_EMBEDDING_MODEL", "test-model-env")
    from yadgar.backend.embed_service import _get_embed_checkpoint_hash

    expected = hashlib.sha256(b"test-model-env").hexdigest()[:16]
    assert _get_embed_checkpoint_hash() == expected


def test_get_embed_checkpoint_hash_yaml(monkeypatch, tmp_path):
    import hashlib

    monkeypatch.delenv("YADGAR_EMBEDDING_MODEL", raising=False)
    _write_yaml(tmp_path, "embedding_model: yaml-model\n")
    from yadgar.backend.embed_service import _get_embed_checkpoint_hash

    expected = hashlib.sha256(b"yaml-model").hexdigest()[:16]
    assert _get_embed_checkpoint_hash() == expected


# ---------------------------------------------------------------------------
# 7b. _get_ce_checkpoint_hash — reranker-model resolution (T4 Car 0 fix).
# Pre-fix these tests asserted the EMBEDDING-model fallback (the split-brain
# bug: a reranker swap never changed _ckpt). Now the hash derives from
# GTE_RERANKER_MODEL + CE_SCORING_VERSION salt.
# ---------------------------------------------------------------------------


def test_get_ce_checkpoint_hash_reranker_env(monkeypatch):
    """Env override of GTE_RERANKER_MODEL feeds the CE checkpoint hash."""
    import hashlib

    monkeypatch.setenv("YADGAR_GTE_RERANKER_MODEL", "env-reranker")
    from yadgar.backend.embed_service import _get_ce_checkpoint_hash
    from yadgar.backend.embed_service.embed_service import CE_SCORING_VERSION

    expected = hashlib.sha256(f"env-reranker:{CE_SCORING_VERSION}".encode()).hexdigest()[:16]
    assert _get_ce_checkpoint_hash() == expected


def test_get_ce_checkpoint_hash_reranker_yaml(monkeypatch, tmp_path):
    """Env unset → yaml value for GTE_RERANKER_MODEL used."""
    import hashlib

    monkeypatch.delenv("YADGAR_GTE_RERANKER_MODEL", raising=False)
    _write_yaml(tmp_path, "gte_reranker_model: yaml-reranker\n")
    from yadgar.backend.embed_service import _get_ce_checkpoint_hash
    from yadgar.backend.embed_service.embed_service import CE_SCORING_VERSION

    expected = hashlib.sha256(f"yaml-reranker:{CE_SCORING_VERSION}".encode()).hexdigest()[:16]
    assert _get_ce_checkpoint_hash() == expected


# ---------------------------------------------------------------------------
# 8. BACKEND_LOG_LEVEL (lifespan)
# We test the accessor directly via the resolve_knob import
# ---------------------------------------------------------------------------


def test_backend_log_level_env_override(monkeypatch):
    monkeypatch.setenv("YADGAR_BACKEND_LOG_LEVEL", "debug")
    from yadgar._shared.config import resolve_knob

    result = resolve_knob("YADGAR_BACKEND_LOG_LEVEL", "BACKEND_LOG_LEVEL", str, "warn")
    assert result.upper() == "DEBUG"


def test_backend_log_level_yaml(monkeypatch, tmp_path):
    monkeypatch.delenv("YADGAR_BACKEND_LOG_LEVEL", raising=False)
    _write_yaml(tmp_path, "backend_log_level: info\n")
    from yadgar._shared.config import resolve_knob

    result = resolve_knob("YADGAR_BACKEND_LOG_LEVEL", "BACKEND_LOG_LEVEL", str, "warn")
    assert result.upper() == "INFO"


# ---------------------------------------------------------------------------
# 9. LOG_FORMAT (embed_service.py lifespan only)
# ---------------------------------------------------------------------------


def test_log_format_env_override(monkeypatch):
    monkeypatch.setenv("YADGAR_LOG_FORMAT", "text")
    from yadgar._shared.config import resolve_knob

    result = resolve_knob("YADGAR_LOG_FORMAT", "LOG_FORMAT", str, "json")
    assert result == "text"


def test_log_format_yaml(monkeypatch, tmp_path):
    monkeypatch.delenv("YADGAR_LOG_FORMAT", raising=False)
    _write_yaml(tmp_path, "log_format: text\n")
    from yadgar._shared.config import resolve_knob

    result = resolve_knob("YADGAR_LOG_FORMAT", "LOG_FORMAT", str, "json")
    assert result == "text"


# ---------------------------------------------------------------------------
# 10. MODEL_IDLE_EVICTION_SECONDS — now a full 5-place knob (Settings field added
#     alongside this wire), so both env override AND config.yaml must be respected.
# ---------------------------------------------------------------------------


def test_model_idle_eviction_env_override(monkeypatch):
    """Env override wins over config.yaml + default."""
    monkeypatch.setenv("YADGAR_MODEL_IDLE_EVICTION_SECONDS", "300")
    from yadgar.backend.ml_client import _idle_eviction_seconds

    assert _idle_eviction_seconds() == 300


def test_model_idle_eviction_yaml_respected(monkeypatch, tmp_path):
    """config.yaml model_idle_eviction_seconds is respected when env is unset."""
    monkeypatch.delenv("YADGAR_MODEL_IDLE_EVICTION_SECONDS", raising=False)
    _write_yaml(tmp_path, "model_idle_eviction_seconds: 450\n")
    from yadgar.backend.ml_client import _idle_eviction_seconds

    assert _idle_eviction_seconds() == 450
