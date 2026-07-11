"""Token-bucket rate limiter for auto-capture endpoint.

Keyed on an arbitrary string (typically the directory path).  Each key
gets its own bucket; the global dict is bounded to prevent unbounded growth.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict

from yadgar._shared.observability.observe import observe

# Maximum number of distinct keys tracked simultaneously.
# Oldest keys are evicted when this limit is exceeded.
_MAX_KEYS = 1_000


class TokenBucketRateLimiter:
    """Per-key token-bucket rate limiter.

    Args:
        max_per_minute: Maximum allowed calls per key per minute.
                        Tokens refill at rate max_per_minute / 60 per second.
    """

    def __init__(self, max_per_minute: int = 30) -> None:
        self._rate = max_per_minute / 60.0  # tokens per second
        self._capacity = float(max_per_minute)
        # key → (tokens, last_refill_time)
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self._lock = threading.Lock()

    @observe(tier="hot")
    def allow(self, key: str) -> bool:
        """Return True and consume one token if the key is under the limit.

        Returns False (rate-limited) when the bucket is empty.
        """
        from yadgar._shared.observability.metrics import (
            record_cache_evict,
            record_cache_hit,
            record_cache_miss,
        )

        now = time.monotonic()
        with self._lock:
            if key not in self._buckets:
                record_cache_miss("rate_limit")
                # Evict oldest entry if at capacity
                if len(self._buckets) >= _MAX_KEYS:
                    self._buckets.popitem(last=False)
                    record_cache_evict("rate_limit")
                self._buckets[key] = (self._capacity, now)
            else:
                record_cache_hit("rate_limit")

            tokens, last_time = self._buckets[key]
            elapsed = now - last_time
            tokens = min(self._capacity, tokens + elapsed * self._rate)
            if tokens < 1.0:
                self._buckets[key] = (tokens, now)
                return False
            self._buckets[key] = (tokens - 1.0, now)
            return True

    @observe(tier="hot")
    def reset(self, key: str) -> None:
        """Reset bucket for key (for testing)."""
        with self._lock:
            self._buckets.pop(key, None)
