"""In-process nonce pool for ``/session_bind`` (Car B, 0047 §3.2).

Mints a single-use nonce → project_id binding that the SessionStart hook
consumes to prove it owns the project_id it claims. The pool is FIFO with
a hard cap (``session_bind.max_pending_nonces``) and a soft-warn threshold
(80% of cap) so a stuck consumer — or a misbehaving caller that POSTs
without consuming — is observable in logs before it trips the eviction.

Single-use: a successful ``consume`` deletes the entry. A nonce can be
consumed at most once. An unknown / evicted / forged nonce returns
``None`` and the caller (the HTTP route) translates that into a
structured ``session_not_bound`` error envelope.

Thread-safety: the pool is guarded by a single ``threading.Lock``. The
cap and FIFO eviction run INSIDE the lock so a concurrent ``register`` +
``consume`` cannot race the dict size.

Why not asyncio.Lock: this module is importable from sync code paths
(``yadgar.core.server`` and tests). A stdlib ``threading.Lock`` is
cheaper and works for both sync and asyncio callers (the asyncio
caller pays only a small awaitable-yield).

NOT a write gate. The nonce pool is a control-plane mechanism, not a
storage layer. No DB writes, no queue enqueue, no project registry
mutation. Its only contract is "the caller of register() may now hand
this nonce to consume() and receive the project_id it was bound to".
"""

from __future__ import annotations

import logging
import secrets
import threading
from collections import OrderedDict

from yadgar._shared.config import resolve_knob
from yadgar._shared.observability.observe import observe
from yadgar._shared.observability.trace import trace_span

logger = logging.getLogger(__name__)


def _max_pending_nonces() -> int:
    """Read the cap from config; default 128. The cap is the number of
    OUTSTANDING nonces, not the lifetime throughput — FIFO eviction
    keeps the dict at the cap once steady-state is reached."""
    return int(
        resolve_knob(
            "YADGAR_SESSION_BIND_MAX_PENDING_NONCES",
            "SESSION_BIND_MAX_PENDING_NONCES",
            int,
            128,
        )
    )


class NoncePool:
    """FIFO single-use nonce store.

    Use ``register(project_id)`` to mint a nonce and bind it; pass the
    nonce string to the out-of-band caller; call ``consume(nonce)`` from
    the consumer. ``consume`` returns the bound project_id and removes
    the entry — a second call with the same nonce returns ``None``.
    """

    def __init__(self, max_size: int | None = None) -> None:
        self._max = max_size if max_size is not None else _max_pending_nonces()
        self._lock = threading.Lock()
        # OrderedDict preserves insertion order so eviction is deterministic.
        self._pool: OrderedDict[str, str] = OrderedDict()
        # Track last-warned occupancy so we don't spam the log on every
        # register past 80% — warn once per crossing.
        self._last_warned_occupancy: int = 0

    def _now(self) -> int:
        # stdlib time is fine; this only fires on warn/cap.
        import time as _time  # noqa: PLC0415

        return int(_time.monotonic())

    @property
    def max_size(self) -> int:
        return self._max

    @observe(tier="stage", metric="runtime.session_bind.register")
    def register(self, project_id: str) -> str:
        """Mint a single-use nonce bound to ``project_id``.

        FIFO-evicts the oldest entry when the pool is at cap. The cap is
        a guard against a stuck consumer; a healthy caller REGISTERs
        and IMMEDIATELY consumes, so the pool never grows beyond the
        in-flight count.
        """
        if not isinstance(project_id, str) or not project_id.strip():
            raise ValueError("project_id must be a non-empty string")
        nonce = secrets.token_urlsafe(32)
        with self._lock:
            if len(self._pool) >= self._max:
                # FIFO evict the oldest. The warn-at-80% is below.
                self._pool.popitem(last=False)
            self._pool[nonce] = project_id
            # Soft warn at 80% — once per crossing, not per register.
            _occ = len(self._pool)
            _threshold = max(1, int(self._max * 0.8))
            if _occ >= _threshold and self._last_warned_occupancy < _threshold:
                logger.warning(
                    "session_bind nonce pool at %d/%d (>=80%%) — a consumer may "
                    "be stuck; FIFO eviction engaged",
                    _occ,
                    self._max,
                )
                self._last_warned_occupancy = _occ
        return nonce

    @observe(tier="stage", metric="runtime.session_bind.consume")
    def consume(self, nonce: str) -> str | None:
        """Consume ``nonce`` and return the bound project_id, or ``None``.

        Single-use: removes the entry on success. A second call with the
        same nonce returns ``None`` (treated identically to an unknown
        nonce — the caller does not need to distinguish). Unknown /
        evicted / forged nonces also return ``None``.
        """
        if not isinstance(nonce, str) or not nonce:
            return None
        with self._lock:
            project_id = self._pool.pop(nonce, None)
        return project_id

    @observe(tier="hot", span=False)
    def __len__(self) -> int:
        with self._lock:
            return len(self._pool)

    @trace_span()
    def clear(self) -> None:
        """Drop every entry. Test-only hook — never call from production
        code, this would yank bindings out from under live consumers.
        """
        with self._lock:
            self._pool.clear()
            self._last_warned_occupancy = 0


# Module-level singleton; the HTTP route imports THIS instance, not a
# freshly-constructed one, so a register in one handler is visible to a
# consume in another. The lock makes the swap-in / swap-out of the
# instance safe (the route reads the live singleton at call time).
_POOL: NoncePool | None = None
_POOL_LOCK = threading.Lock()


@trace_span()
def get_nonce_pool() -> NoncePool:
    """Return the process-wide singleton ``NoncePool``.

    Lazy: defer construction to first use so import-time cost is zero
    (the pool is only created when the daemon actually serves a
    /session_bind request). The module-level lock makes "create once"
    atomic under a race.
    """
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = NoncePool()
        return _POOL


def mint_session_token() -> str:
    """Return a random session token string. Pure helper; no pool state.

    The token is what the /session_bind route returns to the caller. It
    is NOT itself bound to anything in the pool — its job is to be
    opaque to the client and to flow through the Mcp-Session-Id header
    on subsequent MCP calls. The binding is ``sid -> project_id`` (the
    transport's mcp_session_id → pool-consume outcome), and the token
    just lets the caller prove possession of the session they minted.
    """
    return secrets.token_urlsafe(32)


__all__ = [
    "NoncePool",
    "get_nonce_pool",
    "mint_session_token",
]
