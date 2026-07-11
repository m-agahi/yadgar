"""v5.95.0 config-integrity Phase 1: _offload.py accessors are yaml-authoritative.

The phantom-knob fix. Before v5.95.0 the six _offload.py accessors read
`os.environ.get("YADGAR_X")` ENV-ONLY, so config.yaml/UI showed+wrote values the
code never read — proven by `offload_tools: true` being ignored (offload ran OFF,
the --cpus 1 core froze, #72). Each accessor now resolves env > config.yaml > default.

Two assertions per knob (advisor's discriminating mechanic):
  - env override still wins (test/container escape hatch preserved);
  - config.yaml value is respected when env is UNSET (requires clear_config_caches()
    after writing yaml AND the env var unset — get_settings() is lru_cached).
"""

from __future__ import annotations

import pytest

from yadgar._shared.config.config_registry import clear_config_caches


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch, tmp_path):
    cfg = tmp_path / "yadgar-offload-integrity.yaml"
    monkeypatch.setenv("YADGAR_CONFIG_FILE", str(cfg))
    clear_config_caches()
    yield
    clear_config_caches()


def _write_yaml(body: str) -> None:
    from yadgar._shared.config.config_yaml import get_config_path

    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    clear_config_caches()


# ── offload_enabled — THE freeze fix (#72): arm via config.yaml ───────────────


def test_offload_enabled_armed_by_yaml(monkeypatch):
    """config.yaml `offload_tools: true` ARMS offload with env unset (the #72 fix)."""
    from yadgar._shared.runtime.offload import offload_enabled

    monkeypatch.delenv("YADGAR_OFFLOAD_TOOLS", raising=False)
    _write_yaml("offload_tools: true\n")
    assert offload_enabled() is True, "config.yaml offload_tools:true must arm offload"


def test_offload_enabled_env_overrides_yaml(monkeypatch):
    """Env override still wins: YADGAR_OFFLOAD_TOOLS=0 disarms even with yaml true."""
    from yadgar._shared.runtime.offload import offload_enabled

    monkeypatch.setenv("YADGAR_OFFLOAD_TOOLS", "0")
    _write_yaml("offload_tools: true\n")
    assert offload_enabled() is False, "env=0 must override yaml offload_tools:true"


def test_offload_disabled_by_default(monkeypatch):
    """No env, no yaml → offload stays OFF (Settings default OFFLOAD_TOOLS=False)."""
    from yadgar._shared.runtime.offload import offload_enabled

    monkeypatch.delenv("YADGAR_OFFLOAD_TOOLS", raising=False)
    _write_yaml("")  # empty config
    assert offload_enabled() is False, "default must be OFF (unvalidated-live safety)"


# ── _tool_timeout_sec ─────────────────────────────────────────────────────────


def test_tool_timeout_yaml_respected(monkeypatch):
    from yadgar._shared.runtime.offload import _tool_timeout_sec

    monkeypatch.delenv("YADGAR_TOOL_TIMEOUT_SEC", raising=False)
    _write_yaml("tool_timeout_sec: 12.5\n")
    assert _tool_timeout_sec() == 12.5


def test_tool_timeout_env_overrides(monkeypatch):
    from yadgar._shared.runtime.offload import _tool_timeout_sec

    monkeypatch.setenv("YADGAR_TOOL_TIMEOUT_SEC", "7")
    _write_yaml("tool_timeout_sec: 12.5\n")
    assert _tool_timeout_sec() == 7.0


# ── _heavy_concurrency (clamp stays outside resolve) ──────────────────────────


def test_heavy_concurrency_yaml_respected(monkeypatch):
    """yaml RECALL_HEAVY_CONCURRENCY respected, still clamped to [1, pool]."""
    from yadgar._shared.runtime.offload import _heavy_concurrency

    monkeypatch.delenv("YADGAR_RECALL_HEAVY_CONCURRENCY", raising=False)
    monkeypatch.delenv("YADGAR_TOOL_POOL_WORKERS", raising=False)
    # pool default is 2, so heavy concurrency 1 (yaml) stays 1 after clamp.
    _write_yaml("recall_heavy_concurrency: 1\ntool_pool_workers: 2\n")
    assert _heavy_concurrency() == 1


def test_heavy_concurrency_clamped_to_pool(monkeypatch):
    """A yaml value above the pool size is clamped down to the pool size."""
    from yadgar._shared.runtime.offload import _heavy_concurrency

    monkeypatch.delenv("YADGAR_RECALL_HEAVY_CONCURRENCY", raising=False)
    monkeypatch.delenv("YADGAR_TOOL_POOL_WORKERS", raising=False)
    _write_yaml("recall_heavy_concurrency: 9\ntool_pool_workers: 2\n")
    assert _heavy_concurrency() == 2, "clamp min(9, pool=2) == 2"


# ── _rerank_gate_acquire_timeout_sec ──────────────────────────────────────────


def test_rerank_gate_timeout_yaml_respected(monkeypatch):
    from yadgar._shared.runtime.offload import _rerank_gate_acquire_timeout_sec

    monkeypatch.delenv("YADGAR_RERANK_GATE_ACQUIRE_TIMEOUT_SEC", raising=False)
    _write_yaml("rerank_gate_acquire_timeout_sec: 3.5\n")
    assert _rerank_gate_acquire_timeout_sec() == 3.5


def test_rerank_gate_timeout_env_overrides(monkeypatch):
    from yadgar._shared.runtime.offload import _rerank_gate_acquire_timeout_sec

    monkeypatch.setenv("YADGAR_RERANK_GATE_ACQUIRE_TIMEOUT_SEC", "1.0")
    _write_yaml("rerank_gate_acquire_timeout_sec: 3.5\n")
    assert _rerank_gate_acquire_timeout_sec() == 1.0


# ── _saturation_grace_sec ─────────────────────────────────────────────────────


def test_saturation_grace_yaml_respected(monkeypatch):
    from yadgar._shared.runtime.offload import _saturation_grace_sec

    monkeypatch.delenv("YADGAR_TOOL_SATURATION_GRACE_SEC", raising=False)
    _write_yaml("tool_saturation_grace_sec: 90\n")
    assert _saturation_grace_sec() == 90.0


def test_saturation_grace_env_overrides(monkeypatch):
    from yadgar._shared.runtime.offload import _saturation_grace_sec

    monkeypatch.setenv("YADGAR_TOOL_SATURATION_GRACE_SEC", "20")
    _write_yaml("tool_saturation_grace_sec: 90\n")
    assert _saturation_grace_sec() == 20.0
