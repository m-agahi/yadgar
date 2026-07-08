"""ML scoring client — local (sentence_transformers) or remote (HTTP).

LocalMLClient: loads models directly (used in stdio/daemon mode).
RemoteMLClient: delegates to backend /rerank HTTP endpoint (used in Docker core container).

No sentence_transformers import at module level — all heavy imports are lazy
inside LocalMLClient methods, so importing this module is safe in core container.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable

from yadgar._shared.config import resolve_knob
from yadgar._shared.observability.observe import observe


def _rpc_span(name: str, attributes: dict | None = None):
    """Context manager: OTel span for RemoteMLClient RPC calls.

    Falls back to a no-op context when OTel is unavailable.
    """
    try:
        from opentelemetry import trace as _t  # noqa: PLC0415

        tracer = _t.get_tracer("yadgar.backend.ml_client")
        ctx = tracer.start_as_current_span(name)
        return ctx
    except Exception:
        import contextlib  # noqa: PLC0415

        return contextlib.nullcontext()


logger = logging.getLogger(__name__)


class OnnxRerankerUnavailableError(RuntimeError):
    """Raised when GTE_RERANKER_BACKEND=onnx-int8 is selected but the ONNX reranker
    fails to load. v5.98 Lever 3 is DORMANT: this is a loud, deliberate failure so an
    operator flipping the flag is NOT silently degraded to FlashRank/zeros. Distinct
    from a generic transient GTE failure, which DOES fall back."""


# ---------------------------------------------------------------------------
# v5.6.7 PR-G — YADGAR_MODEL_IDLE_EVICTION_SECONDS
#
# Default 0 = never evict (models stay loaded for container lifetime).
# Positive integer = evict heavy models (CE/NLI/pair) after that many idle seconds.
# Embedding model is managed separately (EmbeddingEngine) and is unaffected.
#
# Read per-call so that tests can monkeypatch os.environ without module reload.
# Cost: one os.getenv() per unload_if_idle() invocation — negligible.
# ---------------------------------------------------------------------------


def _record_model_load(model: str, duration_seconds: float) -> None:
    """Record a cold model load: observe histogram + emit OTel span.

    model: metric/span label — "ce" or "nli".
    duration_seconds: wall-clock elapsed for the constructor call.
    cold_load is always True here (called only when the handle was None before construction).
    """
    # Histogram
    try:
        import yadgar.backend.embed_service_metrics as _esm  # noqa: PLC0415

        _esm.model_load_duration_seconds.labels(model=model).observe(duration_seconds)
    except Exception:
        pass  # metrics not available in core container

    # OTel span
    try:
        from opentelemetry import trace as _otel  # noqa: PLC0415

        tracer = _otel.get_tracer("yadgar.backend.ml_client")
        with tracer.start_as_current_span("model.load") as span:
            span.set_attribute("model", model)
            span.set_attribute("cold_load", True)
            span.set_attribute("duration_seconds", duration_seconds)
    except Exception:
        pass  # OTel not available — no-op


def _emit_unload_telemetry(unloaded_ce: bool, unloaded_nli: bool, effective: float) -> None:
    """Emit Prometheus + OTel telemetry for an idle-eviction unload event.

    Extracted to keep LocalMLClient.unload_if_idle under the cyclo hard limit.
    """
    # Prometheus gauges + counters
    try:
        import yadgar.backend.embed_service_metrics as _esm  # noqa: PLC0415

        if unloaded_ce:
            _esm.model_loaded.labels(model="ce").set(0)
            _esm.model_unload_total.labels(model="ce").inc()
        if unloaded_nli:
            _esm.model_loaded.labels(model="nli").set(0)
            _esm.model_unload_total.labels(model="nli").inc()
    except Exception:
        pass  # metrics not available in core container

    # OTel span — one span per call that actually evicted
    try:
        from opentelemetry import trace as _otel  # noqa: PLC0415

        tracer = _otel.get_tracer("yadgar.backend.ml_client")
        model_label = ",".join((["ce"] if unloaded_ce else []) + (["nli"] if unloaded_nli else []))
        with tracer.start_as_current_span("model.unload") as span:
            span.set_attribute("model", model_label)
            span.set_attribute("idle_seconds", float(effective))
    except Exception:
        pass  # OTel not available — no-op


def _idle_eviction_seconds() -> int:
    """Return the configured idle-eviction threshold in seconds.

    Returns 0 when YADGAR_MODEL_IDLE_EVICTION_SECONDS is unset, empty, or
    unparseable — meaning 'never evict' (safe default).
    """
    return resolve_knob("YADGAR_MODEL_IDLE_EVICTION_SECONDS", "MODEL_IDLE_EVICTION_SECONDS", int, 0)


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
        self._lock = threading.Lock()
        # Emit initial CLOSED state so viz sees the time-series from startup.
        self._set_gauge(0)
        # Emit initial reachable=1 — assume reachable until proven otherwise.
        self._set_reachable(1)

    # ------------------------------------------------------------------ #
    # Internal helpers                                                      #
    # ------------------------------------------------------------------ #

    def _set_gauge(self, value: int) -> None:
        """Update the circuit breaker state gauge. Non-fatal."""
        try:
            if self._metrics is None:
                import yadgar._shared.metrics as _m  # noqa: PLC0415

                self._metrics = _m
            self._metrics.yadgar_circuit_breaker_state.labels(endpoint=self._endpoint).set(value)
        except Exception:
            pass

    def _set_reachable(self, value: int) -> None:
        """Update backend reachability gauge (1=reachable, 0=unreachable). Non-fatal."""
        try:
            if self._metrics is None:
                import yadgar._shared.metrics as _m  # noqa: PLC0415

                self._metrics = _m
            self._metrics.yadgar_backend_reachable.labels(endpoint=self._endpoint).set(value)
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Public state queries                                                  #
    # ------------------------------------------------------------------ #

    @observe(tier="hot")
    def is_open(self, _now: float | None = None) -> bool:
        """Return True if in OPEN state (not yet past cooldown)."""
        with self._lock:
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

    @observe(tier="hot")
    def cooldown_remaining(self, _now: float | None = None) -> float:
        if self._state != _STATE_OPEN:
            return 0.0
        now = _now if _now is not None else self._time_fn()
        return max(0.0, self._open_duration_sec - (now - self._open_at))

    # ------------------------------------------------------------------ #
    # State mutators                                                        #
    # ------------------------------------------------------------------ #

    @observe(tier="hot")
    def record_success(self) -> None:
        with self._lock:
            if self._state in (_STATE_HALF_OPEN, _STATE_OPEN):
                logger.info(
                    "circuit breaker %s → CLOSED (probe succeeded)",
                    self._endpoint,
                )
            self._state = _STATE_CLOSED
            self._set_gauge(0)
            self._set_reachable(1)
            self.consecutive_failures = 0
            # Reset exponential backoff so next OPEN starts from base duration
            self.consecutive_probe_failures = 0
            self._open_duration_sec = self._base_open_duration_sec

    @observe(tier="hot")
    def record_failure(self, _now: float | None = None) -> None:
        with self._lock:
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
            elif (
                self._state == _STATE_CLOSED
                and self.consecutive_failures >= self._failure_threshold
            ):
                self._open(reason=f"{self.consecutive_failures} consecutive failures", _now=_now)

    @observe(tier="hot")
    def _open(self, reason: str, _now: float | None = None) -> None:
        now = _now if _now is not None else self._time_fn()
        self._state = _STATE_OPEN
        self._open_at = now
        self._set_gauge(2)
        self._set_reachable(0)
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


# Car 2 (folder-split #17): the ML scoring Protocol moved to yadgar/_shared/protocols.py
# (the single home for cross-boundary seams). Re-exported here under the historical
# name ``MLClient`` for back-compat; ``LocalMLClient``/``RemoteMLClient`` below
# structurally satisfy it. The ``as MLClient`` redundant alias + ``__all__`` entry
# mark this as an intentional re-export (ruff F401 pass).
from yadgar._shared.protocols import MLClientProtocol as MLClient  # noqa: E402

__all__ = ["LocalMLClient", "MLClient", "RemoteMLClient"]


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
        """Construct the GTE-Reranker CrossEncoder for the configured backend.

        v5.98 Lever 3: GTE_RERANKER_BACKEND selects the inference backend —
        "torch" (fp32, default) or "onnx-int8" (quantized ONNX). The onnx-int8
        path is DORMANT: code-present but NOT yet verified in a built backend
        image (onnxruntime import / onnx CrossEncoder load unproven there — see
        the plan-doc follow-up). If it is explicitly selected and fails to load,
        raise a loud, distinct OnnxRerankerUnavailableError so the flip is caught
        rather than silently degraded to FlashRank/zeros (a quiet quality
        landmine). Any other value loads plain torch fp32.
        """
        from sentence_transformers import CrossEncoder as STCrossEncoder  # noqa: PLC0415

        gte_backend = getattr(settings, "GTE_RERANKER_BACKEND", "torch")
        if gte_backend != "onnx-int8":
            model = STCrossEncoder(
                settings.GTE_RERANKER_MODEL,
                max_length=settings.GTE_RERANKER_MAX_LENGTH,
            )
            logger.info("LocalMLClient: loaded GTE-Reranker: %s", settings.GTE_RERANKER_MODEL)
            return model

        onnx_file = getattr(settings, "GTE_RERANKER_ONNX_FILE", "onnx/model_int8.onnx")
        try:
            model = STCrossEncoder(
                settings.GTE_RERANKER_MODEL,
                max_length=settings.GTE_RERANKER_MAX_LENGTH,
                backend="onnx",
                model_kwargs={"file_name": onnx_file},
            )
        except Exception as onnx_err:
            logger.error(
                "LocalMLClient: GTE_RERANKER_BACKEND=onnx-int8 was selected but the ONNX "
                "reranker (%s) FAILED to load — NOT silently falling back. This backend is "
                "DORMANT/unverified in the deployed image; keep the default 'torch' until the "
                "onnx-int8 artifact-build/runtime step lands. Cause: %s",
                onnx_file,
                onnx_err,
            )
            raise OnnxRerankerUnavailableError(
                "GTE_RERANKER_BACKEND=onnx-int8 selected but the ONNX reranker "
                f"({onnx_file}) failed to load; refusing to silently fall back. "
                "Set GTE_RERANKER_BACKEND=torch (the shipped default) — Lever 3 is "
                "code-present but not yet functional in the backend image."
            ) from onnx_err
        logger.info(
            "LocalMLClient: loaded GTE-Reranker (onnx-int8: %s): %s",
            onnx_file,
            settings.GTE_RERANKER_MODEL,
        )
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
        except OnnxRerankerUnavailableError:
            # v5.98 Lever 3 guardrail: onnx-int8 was explicitly selected and failed.
            # Do NOT silently fall back — propagate the loud, distinct error.
            raise
        except Exception as e:
            from yadgar._shared.exception_telemetry import record_exception  # noqa: PLC0415

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
                from yadgar._shared.exception_telemetry import record_exception  # noqa: PLC0415

                record_exception("ml_client.score_pair", e)
                return [0.0] * len(texts)

        pairs = [(query, t) for t in texts]
        try:
            scores = self._cross_encoder.predict(pairs, show_progress_bar=False)
            return [float(s) for s in scores]
        except Exception as e:
            from yadgar._shared.exception_telemetry import record_exception  # noqa: PLC0415

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
            from yadgar._shared.exception_telemetry import record_exception  # noqa: PLC0415

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


class RemoteMLClient:
    """Delegates to backend /rerank endpoint via HTTP.

    Used in the Docker core container where sentence_transformers must NOT load.
    On HTTP error or open circuit breaker, returns None so callers skip rerank.

    N4: per-endpoint circuit breakers prevent repeated saturation of a struggling
    backend (e.g. post-v5.3.9 BindsTo→Wants decouple, where cascade-kill was removed).
    """

    def __init__(self, base_url: str) -> None:
        import httpx

        from yadgar._shared.config import get_settings as _get_settings

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
                    time_fn=self._now,
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

    @observe(tier="stage")
    def _rerank_rpc(self, mode: str, query: str, texts: list[str]) -> list | None:
        """Shared HTTP + circuit-breaker logic for all /rerank modes.

        Returns the raw ``scores`` list on success, or None on circuit-open /
        HTTP error.  Callers apply result indexing (e.g. ``[0]`` for pair).
        """
        _is_probe = False
        if self._breaker_enabled:
            breaker = self._breakers[mode]  # type: ignore[index]
            now = self._now()
            if breaker.is_open(_now=now) and not breaker.is_half_open(_now=now):
                logger.warning(
                    "/rerank/%s circuit OPEN — skipping (cooldown %.0fs remaining)",
                    mode,
                    breaker.cooldown_remaining(_now=now),
                )
                return None
            _is_probe = breaker.is_half_open(_now=self._now())

        # #74 fix #2: bound concurrent backend reranks behind the process-wide
        # heavy-rerank gate so N offload workers can't drive N parallel /rerank
        # requests and saturate the backend (→ slow /health → core 503 → P0 kill).
        # HALF_OPEN probes BYPASS the gate (they must fast-fail, not queue). On a
        # gate-acquire timeout we DEGRADE (skip rerank → pre-rerank order), reusing
        # the breaker-open None path — never block the worker (it would leak its slot).
        _gated = False
        if not _is_probe:
            from yadgar._shared.runtime.offload import acquire_rerank_slot  # noqa: PLC0415

            if not acquire_rerank_slot():
                logger.warning(
                    "/rerank/%s heavy-gate full — skipping rerank (pre-rerank order)", mode
                )
                return None
            _gated = True
        try:
            _timeout = self._probe_timeout if _is_probe else self._rerank_timeout
            r = self._client.post(
                "/rerank", json={"query": query, "texts": texts, "mode": mode}, timeout=_timeout
            )
            r.raise_for_status()
            result = r.json()["scores"]
            if self._breaker_enabled:
                self._breakers[mode].record_success()  # type: ignore[index]
            return result
        except Exception as e:
            import httpx as _httpx  # noqa: PLC0415

            if isinstance(e, (_httpx.TimeoutException, _httpx.ConnectError, _httpx.HTTPStatusError)):  # fmt: skip
                if self._breaker_enabled:
                    b = self._breakers[mode]  # type: ignore[index]
                    b.record_failure(_now=self._now())
                    logger.warning(
                        "/rerank/%s failure (%d consecutive): %s",
                        mode,
                        b.consecutive_failures,
                        e,
                    )
                else:
                    logger.warning(
                        "backend timeout: RemoteMLClient /rerank %s timed out: %s", mode, e
                    )
            else:
                logger.warning("RemoteMLClient: /rerank %s failed: %s", mode, e)
            return None
        finally:
            if _gated:
                from yadgar._shared.runtime.offload import release_rerank_slot  # noqa: PLC0415

                release_rerank_slot()

    def score_cross_encoder(self, query: str, texts: list[str]) -> list[float] | None:
        with _rpc_span(
            "rpc.rerank.ce",
            {"rerank.mode": "ce", "rerank.n_passages": len(texts), "http.url": self._base_url},
        ):
            return self._rerank_rpc("ce", query, texts)

    def score_nli(self, query: str, texts: list[str]) -> list[float] | None:
        with _rpc_span(
            "rpc.rerank.nli",
            {"rerank.mode": "nli", "rerank.n_passages": len(texts), "http.url": self._base_url},
        ):
            return self._rerank_rpc("nli", query, texts)

    def score_pair(self, query: str, text: str) -> float | None:
        with _rpc_span(
            "rpc.rerank.pair",
            {"rerank.mode": "pair", "rerank.n_passages": 1, "http.url": self._base_url},
        ):
            result = self._rerank_rpc("pair", query, [text])
            return result[0] if result else None

    def unload_if_idle(self, idle_seconds: float | None = None) -> None:
        pass  # backend manages its own lifecycle
