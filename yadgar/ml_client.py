"""ML scoring client — local (sentence_transformers) or remote (HTTP).

LocalMLClient: loads models directly (used in stdio/daemon mode).
RemoteMLClient: delegates to backend /rerank HTTP endpoint (used in Docker core container).

No sentence_transformers import at module level — all heavy imports are lazy
inside LocalMLClient methods, so importing this module is safe in core container.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class MLClient(Protocol):
    """Protocol for ML scoring clients."""

    def score_cross_encoder(self, query: str, texts: list[str]) -> list[float]:
        """Score query-text pairs using a cross-encoder. Returns raw scores."""
        ...

    def score_nli(self, query: str, texts: list[str]) -> list[float]:
        """Score query-text pairs using NLI entailment. Returns raw scores."""
        ...

    def score_pair(self, query: str, text: str) -> float:
        """Score a single query-text pair. Returns raw score."""
        ...

    def unload_if_idle(self, idle_seconds: float = 600.0) -> None:
        """Unload models if unused for idle_seconds."""
        ...


class LocalMLClient:
    """Uses sentence_transformers directly. For stdio/daemon mode (no backend).

    All heavy imports (sentence_transformers, torch, flashrank) are deferred
    to method bodies — importing this module has zero ML import cost.
    """

    def __init__(self, settings) -> None:
        self._settings = settings
        self._gte_reranker = None  # Lazy-loaded GTE-Reranker (STCrossEncoder)
        self._nli_model = None  # Lazy-loaded NLI CrossEncoder
        self._flashrank_ranker = None  # Lazy-loaded FlashRank Ranker
        self._cross_encoder = None  # Lazy-loaded sentence-transformers CrossEncoder (fallback)
        self._last_used: float = 0.0  # monotonic timestamp of last call

    def score_cross_encoder(self, query: str, texts: list[str]) -> list[float]:
        """Return raw cross-encoder scores for (query, text) pairs.

        Tries GTE-Reranker first, falls back to FlashRank, then sentence-transformers
        CrossEncoder — mirroring the priority chain in reranking.py.

        Returns list of float scores, one per text. Returns zeros on total failure.
        """
        self._last_used = time.monotonic()
        if not texts:
            return []

        settings = self._settings

        # --- GTE-Reranker (best zero-shot OOD) ---
        gte_failed = False
        if settings is not None and getattr(settings, "GTE_RERANKER_ENABLED", False):
            try:
                if self._gte_reranker is None:
                    from sentence_transformers import CrossEncoder as STCrossEncoder

                    self._gte_reranker = STCrossEncoder(
                        settings.GTE_RERANKER_MODEL,
                        max_length=settings.GTE_RERANKER_MAX_LENGTH,
                    )
                    logger.info(
                        "LocalMLClient: loaded GTE-Reranker: %s", settings.GTE_RERANKER_MODEL
                    )

                if self._gte_reranker is not False:
                    pairs = [(query, t[:512]) for t in texts]
                    scores = self._gte_reranker.predict(pairs)
                    return [float(s) for s in scores]
            except Exception as e:
                logger.warning("LocalMLClient: GTE-Reranker failed, falling back: %s", e)
                self._gte_reranker = False
                gte_failed = True

        if (
            gte_failed
            and settings is not None
            and not getattr(settings, "GTE_RERANKER_FALLBACK_TO_FLASHRANK", True)
        ):
            return [0.0] * len(texts)

        # --- FlashRank (ONNX, fast on CPU) ---
        try:
            from flashrank import Ranker, RerankRequest

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

        # Respect explicit disable before loading the heavy CrossEncoder fallback.
        if settings is not None and not getattr(settings, "CROSS_ENCODER_ENABLED", True):
            return [0.0] * len(texts)

        # --- sentence-transformers CrossEncoder (final fallback) ---
        try:
            from sentence_transformers import CrossEncoder
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
                self._cross_encoder = CrossEncoder(ce_model)
            except Exception:
                return [0.0] * len(texts)

        pairs = [(query, t) for t in texts]
        try:
            scores = self._cross_encoder.predict(pairs, show_progress_bar=False)
            return [float(s) for s in scores]
        except Exception:
            return [0.0] * len(texts)

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

                self._nli_model = CrossEncoder(nli_model_name)
                logger.info("LocalMLClient: loaded NLI model: %s", nli_model_name)

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
            logger.warning("LocalMLClient: NLI scoring failed: %s", e)
            self._nli_model = False
            return [0.0] * len(texts)

    def score_pair(self, query: str, text: str) -> float:
        """Score a single query-document pair using the active CE model."""
        scores = self.score_cross_encoder(query, [text])
        return scores[0] if scores else 0.0

    def unload_if_idle(self, idle_seconds: float = 600.0) -> None:
        """Unload all model handles if unused for idle_seconds. Frees ~500 MB RSS."""
        import gc

        if self._last_used == 0.0:
            return
        if time.monotonic() - self._last_used < idle_seconds:
            return

        unloaded = []
        if self._gte_reranker not in (None, False):
            self._gte_reranker = None
            unloaded.append("GTE-Reranker")
        if self._nli_model not in (None, False):
            self._nli_model = None
            unloaded.append("NLI")
        if self._flashrank_ranker is not None:
            self._flashrank_ranker = None
            unloaded.append("FlashRank")
        if self._cross_encoder is not None:
            self._cross_encoder = None
            unloaded.append("CrossEncoder")

        if unloaded:
            gc.collect()
            logger.info("LocalMLClient: idle unload (%.0fs): %s", idle_seconds, ", ".join(unloaded))


class RemoteMLClient:
    """Delegates to backend /rerank endpoint via HTTP.

    Used in the Docker core container where sentence_transformers must NOT load.
    On HTTP error, logs a warning and returns zeros so recall degrades gracefully.
    """

    def __init__(self, base_url: str) -> None:
        import httpx

        self._base_url = base_url
        self._client = httpx.Client(base_url=base_url, timeout=30.0)

    def score_cross_encoder(self, query: str, texts: list[str]) -> list[float]:
        try:
            r = self._client.post("/rerank", json={"query": query, "texts": texts, "mode": "ce"})
            r.raise_for_status()
            return r.json()["scores"]
        except Exception as e:
            logger.warning("RemoteMLClient: /rerank ce failed: %s", e)
            return [0.0] * len(texts)

    def score_nli(self, query: str, texts: list[str]) -> list[float]:
        try:
            r = self._client.post("/rerank", json={"query": query, "texts": texts, "mode": "nli"})
            r.raise_for_status()
            return r.json()["scores"]
        except Exception as e:
            logger.warning("RemoteMLClient: /rerank nli failed: %s", e)
            return [0.0] * len(texts)

    def score_pair(self, query: str, text: str) -> float:
        try:
            r = self._client.post("/rerank", json={"query": query, "texts": [text], "mode": "pair"})
            r.raise_for_status()
            return r.json()["scores"][0]
        except Exception as e:
            logger.warning("RemoteMLClient: /rerank pair failed: %s", e)
            return 0.0

    def unload_if_idle(self, idle_seconds: float = 600.0) -> None:
        pass  # backend manages its own lifecycle
