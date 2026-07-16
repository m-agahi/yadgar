"""LocalMLClient: loads ML models directly for stdio/daemon mode (no backend)."""

from __future__ import annotations

import logging
import os
import time

from yadgar._shared.observability.observe import observe
from yadgar.backend.ml_client._telemetry import (
    _emit_unload_telemetry,
    _idle_eviction_seconds,
    _record_model_load,
)

logger = logging.getLogger(__name__)


class LocalMLClient:
    """Uses sentence_transformers directly. For stdio/daemon mode (no backend).

    All heavy imports (sentence_transformers, torch, flashrank) are deferred
    to method bodies — importing this module has zero ML import cost.
    """

    def __init__(self, settings) -> None:
        self._settings = settings
        self._gte_reranker = None  # Lazy-loaded GTE-Reranker (STCrossEncoder)
        self._gte_load_failed = False  # T-0006: track permanent GTE failure
        self._nli_model = None  # Lazy-loaded NLI CrossEncoder
        self._flashrank_ranker = None  # Lazy-loaded FlashRank Ranker
        self._cross_encoder = None  # Lazy-loaded sentence-transformers CrossEncoder (fallback)
        self._last_used: float = 0.0  # monotonic timestamp of last call

    @observe(tier="stage")
    def _load_gte_reranker(self, settings):
        """Construct the GTE-Reranker CrossEncoder (torch fp32).

        The v5.98 Lever 3 onnx-int8 backend (GTE_RERANKER_BACKEND knob) was
        REMOVED in the 5.131.0 deps-modernization train: optimum-onnx (0.1.0,
        latest) caps transformers<4.58.0, which hard-conflicts with the
        transformers>=5.0 floor Ettin requires. The path was dormant (ADR-0043
        NO-GO, never verified in a built image). Re-adding ONNX reranking needs
        an optimum-onnx release that supports transformers 5.x.
        """
        from sentence_transformers import CrossEncoder as STCrossEncoder  # noqa: PLC0415

        model = STCrossEncoder(
            settings.GTE_RERANKER_MODEL,
            max_length=settings.GTE_RERANKER_MAX_LENGTH,
        )
        logger.info("LocalMLClient: loaded GTE-Reranker: %s", settings.GTE_RERANKER_MODEL)
        return model

    @observe(tier="hot")
    def _try_gte_reranker(self, query: str, texts: list[str]) -> list[float] | None:
        """Attempt GTE-Reranker scoring.  Returns scores on success, None to fall through.

        Sets self._gte_load_failed on permanent failure (T-0006).
        Returns [0.0]*len(texts) (terminal) when fallback is disabled.
        """
        settings = self._settings
        if not (
            settings is not None
            and getattr(settings, "GTE_RERANKER_ENABLED", False)
            and not self._gte_load_failed
        ):
            return None

        try:
            if self._gte_reranker is None:
                self._gte_reranker = self._load_gte_reranker(settings)

            if self._gte_reranker is not False:
                pairs = [(query, t[:512]) for t in texts]
                scores = self._gte_reranker.predict(pairs)
                return [float(s) for s in scores]
        except Exception as e:
            from yadgar._shared.observability.exception_telemetry import (
                record_exception,  # noqa: PLC0415
            )

            record_exception("ml_client.reranker_fallback", e)
            logger.warning("LocalMLClient: GTE-Reranker failed, falling back: %s", e)
            self._gte_reranker = False
            self._gte_load_failed = True  # T-0006: mark permanent failure
            # Terminal: return zeros when fallback to FlashRank is explicitly disabled
            if not getattr(settings, "GTE_RERANKER_FALLBACK_TO_FLASHRANK", True):
                return [0.0] * len(texts)

        return None

    @observe(tier="hot")
    def _try_flashrank(self, query: str, texts: list[str]) -> list[float] | None:
        """Attempt FlashRank (ONNX) scoring.  Returns scores on success, None to fall through."""
        try:
            from flashrank import Ranker, RerankRequest  # noqa: PLC0415

            if self._flashrank_ranker is None:
                self._flashrank_ranker = Ranker(
                    model_name="ms-marco-MiniLM-L-12-v2",
                    cache_dir=os.path.expanduser("~/.cache/flashrank"),
                )

            passages = [{"id": i, "text": t} for i, t in enumerate(texts)]
            rerank_req = RerankRequest(query=query, passages=passages)
            results = self._flashrank_ranker.rerank(rerank_req)
            # Rebuild score list in original order
            score_map: dict[int, float] = {r["id"]: r["score"] for r in results}
            return [score_map.get(i, 0.0) for i in range(len(texts))]
        except ImportError:
            pass
        except Exception:
            logger.debug(
                "LocalMLClient: FlashRank failed, trying sentence-transformers CrossEncoder"
            )
        return None

    @observe(tier="hot")
    def _try_st_cross_encoder(self, query: str, texts: list[str]) -> list[float]:
        """sentence-transformers CrossEncoder fallback.  Always returns a list (zeros on error)."""
        settings = self._settings
        # Respect explicit disable before loading the heavy CrossEncoder fallback.
        if settings is not None and not getattr(settings, "CROSS_ENCODER_ENABLED", True):
            return [0.0] * len(texts)

        try:
            from sentence_transformers import CrossEncoder  # noqa: PLC0415
        except ImportError:
            logger.warning("LocalMLClient: no reranker available (install yadgar[ml])")
            return [0.0] * len(texts)

        if self._cross_encoder is None:
            ce_model = (
                settings.CROSS_ENCODER_MODEL
                if settings is not None
                else "cross-encoder/ms-marco-MiniLM-L-6-v2"
            )
            try:
                _t0 = time.monotonic()
                self._cross_encoder = CrossEncoder(ce_model)
                _load_dur = time.monotonic() - _t0
                # Histogram + OTel span for cold load (v5.6.7 PR-G)
                _record_model_load("ce", _load_dur)
            except Exception as e:
                from yadgar._shared.observability.exception_telemetry import (
                    record_exception,  # noqa: PLC0415
                )

                record_exception("ml_client.score_pair", e)
                return [0.0] * len(texts)

        pairs = [(query, t) for t in texts]
        try:
            scores = self._cross_encoder.predict(pairs, show_progress_bar=False)
            return [float(s) for s in scores]
        except Exception as e:
            from yadgar._shared.observability.exception_telemetry import (
                record_exception,  # noqa: PLC0415
            )

            record_exception("ml_client.score_pair", e)
            return [0.0] * len(texts)

    @observe(tier="stage")
    def score_cross_encoder(self, query: str, texts: list[str]) -> list[float]:
        """Return raw cross-encoder scores for (query, text) pairs.

        Tries GTE-Reranker first, falls back to FlashRank, then sentence-transformers
        CrossEncoder — mirroring the priority chain in reranking.py.

        Returns list of float scores, one per text. Returns zeros on total failure.
        """
        self._last_used = time.monotonic()
        if not texts:
            return []

        result = self._try_gte_reranker(query, texts)
        if result is not None:
            return result

        result = self._try_flashrank(query, texts)
        if result is not None:
            return result

        return self._try_st_cross_encoder(query, texts)

    @observe(tier="stage")
    def score_nli(self, query: str, texts: list[str]) -> list[float]:
        """Return NLI entailment probability for each (text, hypothesis) pair.

        When the model returns 3-class logits [contradiction, neutral, entailment],
        applies softmax and returns index-2 (entailment) probability as a scalar.
        Returns zeros on failure.
        """
        self._last_used = time.monotonic()
        if not texts:
            return []

        settings = self._settings
        nli_model_name = (
            settings.NLI_MODEL if settings is not None else "cross-encoder/nli-deberta-v3-small"
        )

        try:
            if self._nli_model is None:
                from sentence_transformers import CrossEncoder

                _t0 = time.monotonic()
                self._nli_model = CrossEncoder(nli_model_name)
                _load_dur = time.monotonic() - _t0
                logger.info("LocalMLClient: loaded NLI model: %s", nli_model_name)
                # Histogram + OTel span for cold load (v5.6.7 PR-G)
                _record_model_load("nli", _load_dur)

            import numpy as np

            pairs = [(t[:512], query) for t in texts]
            raw_scores = self._nli_model.predict(pairs)

            result: list[float] = []
            for s in raw_scores:
                if hasattr(s, "__len__") and len(s) == 3:
                    arr = np.array(s, dtype=np.float64)
                    exp_arr = np.exp(arr - np.max(arr))
                    probs = exp_arr / exp_arr.sum()
                    result.append(float(probs[2]))  # entailment probability
                else:
                    result.append(float(s))
            return result

        except Exception as e:
            from yadgar._shared.observability.exception_telemetry import (
                record_exception,  # noqa: PLC0415
            )

            record_exception("ml_client.score_nli", e)
            logger.warning("LocalMLClient: NLI scoring failed: %s", e)
            self._nli_model = False
            return [0.0] * len(texts)

    @observe(tier="hot")
    def score_pair(self, query: str, text: str) -> float:
        """Score a single query-document pair using the active CE model."""
        scores = self.score_cross_encoder(query, [text])
        return scores[0] if scores else 0.0

    @observe(tier="stage")
    def unload_if_idle(self, idle_seconds: float | None = None) -> None:
        """Unload all model handles if unused for the configured threshold. Frees ~500 MB RSS.

        idle_seconds: explicit threshold (seconds). None = read from env
                      YADGAR_MODEL_IDLE_EVICTION_SECONDS (default 0 = never evict).

        When the effective threshold is 0 and no explicit idle_seconds is given,
        this method is a no-op (never evict). Callers that pass an explicit
        idle_seconds=N continue to work regardless of the env setting.

        Handle → gauge/counter label mapping:
          _gte_reranker, _flashrank_ranker, _cross_encoder → "ce"
          _nli_model                                       → "nli"
        Pair/embedding are not managed here.
        """
        import gc

        # Resolve effective threshold
        if idle_seconds is None:
            effective = _idle_eviction_seconds()
            if effective == 0:
                # Never-evict default — early return, no INFO spam
                return
        else:
            effective = idle_seconds

        if self._last_used == 0.0:
            return
        if time.monotonic() - self._last_used < effective:
            return

        unloaded_ce = False
        unloaded_nli = False

        unloaded = []
        if self._gte_reranker not in (None, False):
            self._gte_reranker = None
            unloaded.append("GTE-Reranker")
            unloaded_ce = True
        if self._nli_model not in (None, False):
            self._nli_model = None
            unloaded.append("NLI")
            unloaded_nli = True
        if self._flashrank_ranker is not None:
            self._flashrank_ranker = None
            unloaded.append("FlashRank")
            unloaded_ce = True
        if self._cross_encoder is not None:
            self._cross_encoder = None
            unloaded.append("CrossEncoder")
            unloaded_ce = True

        if not unloaded:
            return

        gc.collect()
        logger.info("LocalMLClient: idle unload (%.0fs): %s", effective, ", ".join(unloaded))
        _emit_unload_telemetry(unloaded_ce, unloaded_nli, effective)
