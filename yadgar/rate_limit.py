"""Token-bucket rate limiter for auto-capture endpoint.

Keyed on an arbitrary string (typically the directory path).  Each key
gets its own bucket; the global dict is bounded to prevent unbounded growth.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict

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

    def allow(self, key: str) -> bool:
        """Return True and consume one token if the key is under the limit.

        Returns False (rate-limited) when the bucket is empty.
        """
        now = time.monotonic()
        with self._lock:
            if key not in self._buckets:
                # Evict oldest entry if at capacity
                if len(self._buckets) >= _MAX_KEYS:
                    self._buckets.popitem(last=False)
                self._buckets[key] = (self._capacity, now)

            tokens, last_time = self._buckets[key]
            elapsed = now - last_time
            tokens = min(self._capacity, tokens + elapsed * self._rate)
            if tokens < 1.0:
                self._buckets[key] = (tokens, now)
                return False
            self._buckets[key] = (tokens - 1.0, now)
            return True

    def reset(self, key: str) -> None:
        """Reset bucket for key (for testing)."""
        with self._lock:
            self._buckets.pop(key, None)
