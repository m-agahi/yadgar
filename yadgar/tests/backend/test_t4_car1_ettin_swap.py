"""T4 Car 1 — Ettin CE-model swap guards (t4-ettin-train-2026-07-12.md).

The swap itself is one config line (``GTE_RERANKER_MODEL``). These tests are the
drift + variant-leak guards the plan mandates so the swap — and a later
32M→68M fallback — stays a config-only edit with ZERO test change:

  * config↔loader drift guard: the loader (``LocalMLClient._load_gte_reranker``)
    instantiates from ``settings.GTE_RERANKER_MODEL`` dynamically, never a
    literal model id. If someone hardcodes the model in the loader, the A/B
    ``--settings-override`` lever silently no-ops and the gate measures the
    wrong model.
  * variant-leak guard: no ``"32m"`` / ``"68m"`` / param-count string is baked
    into the loader or config default resolution, so switching the shipped
    Ettin variant is a one-line config change.

These are structural (no model download); the live in-process CE-ran proof
(Ettin actually scores, non-degenerate rerank) is exercised by the LongMemEval
A/B gate and its smoke, not here (that needs the weights + is the gate itself).

Run: uv run --extra test --extra ml pytest \
    yadgar/tests/backend/test_t4_car1_ettin_swap.py
"""

from __future__ import annotations

import inspect

from yadgar._shared.config.config import Settings

GTE_DEFAULT = "Alibaba-NLP/gte-reranker-modernbert-base"
ETTIN_32M = "cross-encoder/ettin-reranker-32m-v1"
ETTIN_68M = "cross-encoder/ettin-reranker-68m-v1"


class TestLoaderReadsConfigDynamically:
    """The A/B lever only works if the loader reads the config field, not a literal."""

    def test_load_gte_reranker_reads_settings_field(self):
        """``_load_gte_reranker`` must pass ``settings.GTE_RERANKER_MODEL`` to the
        CrossEncoder ctor — the config↔loader contract the A/B override rides on."""
        from yadgar.backend.ml_client import ml_client as _mc

        src = inspect.getsource(_mc.LocalMLClient._load_gte_reranker)
        assert "settings.GTE_RERANKER_MODEL" in src, (
            "loader must instantiate from settings.GTE_RERANKER_MODEL — a hardcoded "
            "model id would make the --settings-override A/B lever a silent no-op"
        )

    def test_loader_hardcodes_no_ettin_model_id(self):
        """No hardcoded Ettin *model-id literal* in the loader — the shipped
        variant is chosen by config alone, so 32M→68M fallback is a config-only
        edit. (A docstring mention of "ettin" is fine; a quoted
        ``cross-encoder/ettin-reranker-*`` id in the code is not.)"""
        from yadgar.backend.ml_client import ml_client as _mc

        src = inspect.getsource(_mc.LocalMLClient._load_gte_reranker)
        for variant in ("32m", "68m", "17m", "150m"):
            leaked_id = f"cross-encoder/ettin-reranker-{variant}-v1"
            assert leaked_id not in src, f"loader hardcodes model id {leaked_id!r}"


class TestConfigSwapLever:
    """The ``GTE_RERANKER_MODEL`` field is the whole swap surface."""

    def test_default_is_ettin_32m(self, monkeypatch):
        """Car 2 flip: the shipped DEFAULT reranker is Ettin-32m (gate winner).
        No env override → the config default resolves to the swap target."""
        from yadgar._shared.config.config_registry import clear_config_caches

        monkeypatch.delenv("YADGAR_GTE_RERANKER_MODEL", raising=False)
        clear_config_caches()
        try:
            assert Settings().GTE_RERANKER_MODEL == ETTIN_32M
        finally:
            clear_config_caches()

    def test_override_selects_ettin_32m(self):
        assert Settings(GTE_RERANKER_MODEL=ETTIN_32M).GTE_RERANKER_MODEL == ETTIN_32M

    def test_override_selects_ettin_68m(self):
        assert Settings(GTE_RERANKER_MODEL=ETTIN_68M).GTE_RERANKER_MODEL == ETTIN_68M

    def test_env_override_selects_model(self, monkeypatch):
        """Env ``YADGAR_GTE_RERANKER_MODEL`` is the runtime rollback lever."""
        from yadgar._shared.config.config_registry import clear_config_caches

        monkeypatch.setenv("YADGAR_GTE_RERANKER_MODEL", ETTIN_32M)
        clear_config_caches()
        try:
            assert Settings().GTE_RERANKER_MODEL == ETTIN_32M
        finally:
            clear_config_caches()

    def test_max_length_ge_512(self):
        """Ettin advertises 8K context; the config keeps ≥ GTE's 512 truncation."""
        assert Settings(GTE_RERANKER_MODEL=ETTIN_32M).GTE_RERANKER_MAX_LENGTH >= 512
