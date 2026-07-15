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

from yadgar._shared.config import resolve_knob
from yadgar._shared.observability.observe import observe

# _CircuitBreaker + the _STATE_* constants moved to ``circuit_breaker.py`` (task
# #18 C2 internal split). Imported here for RemoteMLClient; re-exported via the
# package ``__init__`` for back-compat importers.
from yadgar.backend.ml_client.circuit_breaker import (
    _STATE_CLOSED as _STATE_CLOSED,  # noqa: PLC0414 — intentional re-export
)
from yadgar.backend.ml_client.circuit_breaker import (
    _STATE_HALF_OPEN as _STATE_HALF_OPEN,  # noqa: PLC0414
)
from yadgar.backend.ml_client.circuit_breaker import (
    _STATE_OPEN as _STATE_OPEN,  # noqa: PLC0414
)
from yadgar.backend.ml_client.circuit_breaker import (
    _CircuitBreaker,
)


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
        import yadgar.backend.embed_service.embed_service_metrics as _esm  # noqa: PLC0415

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
        import yadgar.backend.embed_service.embed_service_metrics as _esm  # noqa: PLC0415

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
# MLClient protocol
# ---------------------------------------------------------------------------


# Car 2 (folder-split #17): the ML scoring Protocol moved to yadgar/_shared/protocols.py
# (the single home for cross-boundary seams). Re-exported here under the historical
# name ``MLClient`` for back-compat; ``LocalMLClient``/``RemoteMLClient`` below
# structurally satisfy it. The ``as MLClient`` redundant alias + ``__all__`` entry
# mark this as an intentional re-export (ruff F401 pass).
from yadgar._shared.contracts.protocols import MLClientProtocol as MLClient  # noqa: E402

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
