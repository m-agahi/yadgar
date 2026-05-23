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
from collections.abc import Callable
from typing import Protocol, runtime_checkable


def _rpc_span(name: str, attributes: dict | None = None):
    """Context manager: OTel span for RemoteMLClient RPC calls.

    Falls back to a no-op context when OTel is unavailable.
    """
    try:
        from opentelemetry import trace as _t  # noqa: PLC0415

        tracer = _t.get_tracer("yadgar.ml_client")
        ctx = tracer.start_as_current_span(name)
        return ctx
    except Exception:
        import contextlib  # noqa: PLC0415

        return contextlib.nullcontext()


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# N4 — Per-endpoint circuit breaker (v5.3.10 hotfix)
# ---------------------------------------------------------------------------

_STATE_CLOSED = "closed"
_STATE_OPEN = "open"
_STATE_HALF_OPEN = "half_open"


class _CircuitBreaker:
    """Per-endpoint open/closed/half-open state.

    States:
      CLOSED    — normal operation, calls go through
      OPEN      — fast-fail; calls return None without HTTP
      HALF_OPEN — single probe attempt; success → CLOSED, failure → OPEN

    Transitions:
      CLOSED → OPEN    when consecutive_failures >= failure_threshold
      OPEN → HALF_OPEN after open_duration_sec seconds
      HALF_OPEN → CLOSED  on probe success
      HALF_OPEN → OPEN    on probe failure (cooldown doubles up to max)

    All clock reads go through _time_fn so tests can inject a fake clock.

    v5.4.2 additions:
      - consecutive_probe_failures: tracks how many HALF_OPEN probes have failed
        without any intervening success.  Used to compute exponential backoff.
      - _base_open_duration_sec: the original config value; backoff multiplies
        the *current* _open_duration_sec, not this, so callers can read the live
        cooldown directly from _open_duration_sec.
      - _max_open_duration_sec / _backoff_factor: cap + multiplier for backoff.
    """

    def __init__(
        self,
        endpoint: str,
        failure_threshold: int,
        open_duration_sec: float,
        time_fn: Callable[[], float] | None = None,
        max_open_duration_sec: float = 600.0,
        backoff_factor: float = 2.0,
        metrics_module=None,
    ) -> None:
        self._endpoint = endpoint
        self._failure_threshold = failure_threshold
        self._base_open_duration_sec = open_duration_sec
        self._open_duration_sec = open_duration_sec
        self._max_open_duration_sec = max_open_duration_sec
        self._backoff_factor = backoff_factor
        self._time_fn: Callable[[], float] = time_fn or time.monotonic
        self._state: str = _STATE_CLOSED
        self._open_at: float = 0.0
        self.consecutive_failures: int = 0
        self.consecutive_probe_failures: int = 0
        self._metrics = metrics_module  # None → lazy-import on first use
        # Emit initial CLOSED state so viz sees the time-series from startup.
        self._set_gauge(0)

    # ------------------------------------------------------------------ #
    # Internal helpers                                                      #
    # ------------------------------------------------------------------ #

    def _set_gauge(self, value: int) -> None:
        """Update the circuit breaker state gauge. Non-fatal."""
        try:
            if self._metrics is None:
                import yadgar.metrics as _m  # noqa: PLC0415

                self._metrics = _m
            self._metrics.yadgar_circuit_breaker_state.labels(endpoint=self._endpoint).set(value)
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Public state queries                                                  #
    # ------------------------------------------------------------------ #

    def is_open(self, _now: float | None = None) -> bool:
        """Return True if in OPEN state (not yet past cooldown)."""
        if self._state == _STATE_OPEN:
            now = _now if _now is not None else self._time_fn()
            if now - self._open_at >= self._open_duration_sec:
                # Cooldown expired — move to half-open
                self._state = _STATE_HALF_OPEN
                self._set_gauge(1)
                logger.info(
                    "circuit breaker %s → HALF_OPEN (cooldown expired after %.0fs)",
                    self._endpoint,
                    self._open_duration_sec,
                )
                return False
            return True
        return False

    def is_half_open(self, _now: float | None = None) -> bool:
        """Return True if in HALF_OPEN state (probe allowed)."""
        # Calling is_open() first handles OPEN→HALF_OPEN transition.
        self.is_open(_now=_now)
        return self._state == _STATE_HALF_OPEN

    def is_closed(self) -> bool:
        return self._state == _STATE_CLOSED

    def cooldown_remaining(self, _now: float | None = None) -> float:
        if self._state != _STATE_OPEN:
            return 0.0
        now = _now if _now is not None else self._time_fn()
        return max(0.0, self._open_duration_sec - (now - self._open_at))

    # ------------------------------------------------------------------ #
    # State mutators                                                        #
    # ------------------------------------------------------------------ #

    def record_success(self) -> None:
        if self._state in (_STATE_HALF_OPEN, _STATE_OPEN):
            logger.info(
                "circuit breaker %s → CLOSED (probe succeeded)",
                self._endpoint,
            )
        self._state = _STATE_CLOSED
        self._set_gauge(0)
        self.consecutive_failures = 0
        # Reset exponential backoff so next OPEN starts from base duration
        self.consecutive_probe_failures = 0
        self._open_duration_sec = self._base_open_duration_sec

    def record_failure(self, _now: float | None = None) -> None:
        self.consecutive_failures += 1
        if self._state == _STATE_HALF_OPEN:
            # Probe failed — apply exponential backoff then reopen
            self.consecutive_probe_failures += 1
            new_duration = min(
                self._open_duration_sec * self._backoff_factor,
                self._max_open_duration_sec,
            )
            self._open_duration_sec = new_duration
            self._open(reason="probe failed (backoff)", _now=_now)
        elif self._state == _STATE_CLOSED and self.consecutive_failures >= self._failure_threshold:
            self._open(reason=f"{self.consecutive_failures} consecutive failures", _now=_now)

    def _open(self, reason: str, _now: float | None = None) -> None:
        now = _now if _now is not None else self._time_fn()
        self._state = _STATE_OPEN
        self._open_at = now
        self._set_gauge(2)
        logger.warning(
            "breaker_open",
            extra={
                "component": "circuit_breaker",
                "action": "breaker_open",
                "outcome": "degraded",
                "endpoint": self._endpoint,
                "reason": reason,
                "cooldown_s": round(self._open_duration_sec, 1),
            },
        )


# ---------------------------------------------------------------------------
# MLClient protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class MLClient(Protocol):
    """Protocol for ML scoring clients."""

    def score_cross_encoder(self, query: str, texts: list[str]) -> list[float] | None:
        """Score query-text pairs using a cross-encoder. Returns raw scores or None on circuit-open."""
        ...

    def score_nli(self, query: str, texts: list[str]) -> list[float] | None:
        """Score query-text pairs using NLI entailment. Returns raw scores or None on circuit-open."""
        ...

    def score_pair(self, query: str, text: str) -> float | None:
        """Score a single query-text pair. Returns raw score or None on circuit-open."""
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
        self._gte_load_failed = False  # T-0006: track permanent GTE failure
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
        # T-0006: gate subsequent attempts after permanent failure
        gte_failed = False
        if (
            settings is not None
            and getattr(settings, "GTE_RERANKER_ENABLED", False)
            and not self._gte_load_failed
        ):
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
                self._gte_load_failed = True  # T-0006: mark permanent failure
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
    On HTTP error or open circuit breaker, returns None so callers skip rerank.

    N4: per-endpoint circuit breakers prevent repeated saturation of a struggling
    backend (e.g. post-v5.3.9 BindsTo→Wants decouple, where cascade-kill was removed).
    """

    def __init__(self, base_url: str) -> None:
        import httpx

        from yadgar.config import get_settings as _get_settings

        settings = _get_settings()

        self._base_url = base_url
        _token = os.environ.get("YADGAR_MCP_AUTH_TOKEN", "")
        _headers = {"Authorization": f"Bearer {_token}"} if _token else {}
        _timeout_sec = float(settings.BACKEND_HTTP_TIMEOUT_SEC)
        _timeout = httpx.Timeout(connect=2.0, read=_timeout_sec, write=_timeout_sec, pool=5.0)
        self._client = httpx.Client(base_url=base_url, timeout=_timeout, headers=_headers)

        # v5.4.2: probe-specific short timeout for HALF_OPEN probe calls
        _probe_sec = float(settings.CIRCUIT_BREAKER_PROBE_TIMEOUT_SEC)
        self._probe_timeout = httpx.Timeout(
            connect=2.0, read=_probe_sec, write=_probe_sec, pool=5.0
        )

        # v5.6.6 C: dedicated timeout for /rerank calls (CE inference 8-46s on CPU).
        # Using general 5s timeout caused spurious CB-1 opens on every CE call.
        _rerank_sec = float(settings.RERANK_BACKEND_TIMEOUT_SEC)
        self._rerank_timeout = httpx.Timeout(
            connect=2.0, read=_rerank_sec, write=_rerank_sec, pool=5.0
        )

        # I3: check flag at construction — zero overhead when disabled
        self._breaker_enabled: bool = bool(settings.CIRCUIT_BREAKER_ENABLED)
        if self._breaker_enabled:
            _threshold = int(settings.CIRCUIT_BREAKER_FAILURE_THRESHOLD)
            _duration = float(settings.CIRCUIT_BREAKER_OPEN_DURATION_SEC)
            _max_duration = float(settings.CIRCUIT_BREAKER_MAX_OPEN_DURATION_SEC)
            _backoff = float(settings.CIRCUIT_BREAKER_BACKOFF_FACTOR)
            self._breakers: dict[str, _CircuitBreaker] | None = {
                mode: _CircuitBreaker(
                    f"/rerank/{mode}",
                    _threshold,
                    _duration,
                    max_open_duration_sec=_max_duration,
                    backoff_factor=_backoff,
                )
                for mode in ("ce", "nli", "pair")
            }
        else:
            self._breakers = None

    def _now(self) -> float:
        """Clock accessor — overridden by tests via self._fake_now."""
        return getattr(self, "_fake_now", time.monotonic())

    def score_cross_encoder(self, query: str, texts: list[str]) -> list[float] | None:
        with _rpc_span(
            "rpc.rerank.ce",
            {"rerank.mode": "ce", "rerank.n_passages": len(texts), "http.url": self._base_url},
        ):
            _is_probe = False
            if self._breaker_enabled:
                breaker = self._breakers["ce"]  # type: ignore[index]
                now = self._now()
                if breaker.is_open(_now=now) and not breaker.is_half_open(_now=now):
                    logger.warning(
                        "/rerank/ce circuit OPEN — skipping (cooldown %.0fs remaining)",
                        breaker.cooldown_remaining(_now=now),
                    )
                    return None
                _is_probe = breaker.is_half_open(_now=self._now())
            try:
                _kwargs = {"json": {"query": query, "texts": texts, "mode": "ce"}}
                if _is_probe:
                    _kwargs["timeout"] = self._probe_timeout
                else:
                    _kwargs["timeout"] = self._rerank_timeout
                r = self._client.post("/rerank", **_kwargs)
                r.raise_for_status()
                result = r.json()["scores"]
                if self._breaker_enabled:
                    self._breakers["ce"].record_success()  # type: ignore[index]
                return result
            except Exception as e:
                import httpx as _httpx

                if isinstance(
                    e, (_httpx.TimeoutException, _httpx.ConnectError, _httpx.HTTPStatusError)
                ):
                    if self._breaker_enabled:
                        b = self._breakers["ce"]  # type: ignore[index]
                        b.record_failure(_now=self._now())
                        logger.warning(
                            "/rerank/ce failure (%d consecutive): %s",
                            b.consecutive_failures,
                            e,
                        )
                    else:
                        logger.warning(
                            "backend timeout: RemoteMLClient /rerank ce timed out: %s", e
                        )
                else:
                    logger.warning("RemoteMLClient: /rerank ce failed: %s", e)
                return None

    def score_nli(self, query: str, texts: list[str]) -> list[float] | None:
        with _rpc_span(
            "rpc.rerank.nli",
            {"rerank.mode": "nli", "rerank.n_passages": len(texts), "http.url": self._base_url},
        ):
            _is_probe = False
            if self._breaker_enabled:
                breaker = self._breakers["nli"]  # type: ignore[index]
                now = self._now()
                if breaker.is_open(_now=now) and not breaker.is_half_open(_now=now):
                    logger.warning(
                        "/rerank/nli circuit OPEN — skipping (cooldown %.0fs remaining)",
                        breaker.cooldown_remaining(_now=now),
                    )
                    return None
                _is_probe = breaker.is_half_open(_now=self._now())
            try:
                _kwargs = {"json": {"query": query, "texts": texts, "mode": "nli"}}
                if _is_probe:
                    _kwargs["timeout"] = self._probe_timeout
                else:
                    _kwargs["timeout"] = self._rerank_timeout
                r = self._client.post("/rerank", **_kwargs)
                r.raise_for_status()
                result = r.json()["scores"]
                if self._breaker_enabled:
                    self._breakers["nli"].record_success()  # type: ignore[index]
                return result
            except Exception as e:
                import httpx as _httpx

                if isinstance(
                    e, (_httpx.TimeoutException, _httpx.ConnectError, _httpx.HTTPStatusError)
                ):
                    if self._breaker_enabled:
                        b = self._breakers["nli"]  # type: ignore[index]
                        b.record_failure(_now=self._now())
                        logger.warning(
                            "/rerank/nli failure (%d consecutive): %s",
                            b.consecutive_failures,
                            e,
                        )
                    else:
                        logger.warning(
                            "backend timeout: RemoteMLClient /rerank nli timed out: %s", e
                        )
                else:
                    logger.warning("RemoteMLClient: /rerank nli failed: %s", e)
                return None

    def score_pair(self, query: str, text: str) -> float | None:
        with _rpc_span(
            "rpc.rerank.pair",
            {"rerank.mode": "pair", "rerank.n_passages": 1, "http.url": self._base_url},
        ):
            _is_probe = False
            if self._breaker_enabled:
                breaker = self._breakers["pair"]  # type: ignore[index]
                now = self._now()
                if breaker.is_open(_now=now) and not breaker.is_half_open(_now=now):
                    logger.warning(
                        "/rerank/pair circuit OPEN — skipping (cooldown %.0fs remaining)",
                        breaker.cooldown_remaining(_now=now),
                    )
                    return None
                _is_probe = breaker.is_half_open(_now=self._now())
            try:
                _kwargs = {"json": {"query": query, "texts": [text], "mode": "pair"}}
                if _is_probe:
                    _kwargs["timeout"] = self._probe_timeout
                else:
                    _kwargs["timeout"] = self._rerank_timeout
                r = self._client.post("/rerank", **_kwargs)
                r.raise_for_status()
                result = r.json()["scores"][0]
                if self._breaker_enabled:
                    self._breakers["pair"].record_success()  # type: ignore[index]
                return result
            except Exception as e:
                import httpx as _httpx

                if isinstance(
                    e, (_httpx.TimeoutException, _httpx.ConnectError, _httpx.HTTPStatusError)
                ):
                    if self._breaker_enabled:
                        b = self._breakers["pair"]  # type: ignore[index]
                        b.record_failure(_now=self._now())
                        logger.warning(
                            "/rerank/pair failure (%d consecutive): %s",
                            b.consecutive_failures,
                            e,
                        )
                    else:
                        logger.warning(
                            "backend timeout: RemoteMLClient /rerank pair timed out: %s", e
                        )
                else:
                    logger.warning("RemoteMLClient: /rerank pair failed: %s", e)
                return None

    def unload_if_idle(self, idle_seconds: float = 600.0) -> None:
        pass  # backend manages its own lifecycle
