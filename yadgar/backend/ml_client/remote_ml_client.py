"""RemoteMLClient: delegates to backend /rerank HTTP endpoint."""

from __future__ import annotations

import logging
import os
import time

from yadgar._shared.observability.observe import observe
from yadgar.backend.ml_client._telemetry import _rpc_span
from yadgar.backend.ml_client.circuit_breaker import (
    _STATE_CLOSED,
    _STATE_HALF_OPEN,
    _STATE_OPEN,
    _CircuitBreaker,
)

logger = logging.getLogger(__name__)

# Silence unused-import warnings — these are re-exported via the facade for back-compat.
_ = (_STATE_CLOSED, _STATE_HALF_OPEN, _STATE_OPEN)


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
        except Exception as e:  # BLE001-KEEP: the caught object IS this handler's input: it is isinstance-tested against httpx TimeoutException/ConnectError/HTTPStatusError to decide whether the circuit breaker records a failure, so the catch must be wider than the types it then classifies
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
