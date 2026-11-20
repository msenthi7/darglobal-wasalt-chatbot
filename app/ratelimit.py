"""Time-aware sliding-window limiter with an injectable clock (blueprint C05).

A mutex would only limit concurrency; this limits requests per time window. State is
in-memory, so limits hold per process: the app runs as a single uvicorn worker on purpose.
"""

from __future__ import annotations

import time
from collections import OrderedDict, deque


class SlidingWindowLimiter:
    def __init__(self, limit: int, window_s: float, *, clock=time.monotonic, max_keys: int = 50_000) -> None:
        if limit < 1 or window_s <= 0:
            raise ValueError("limit must be >= 1 and window_s > 0")
        self.limit, self.window_s, self._clock, self._max_keys = limit, window_s, clock, max_keys
        self._events: OrderedDict[str, deque[float]] = OrderedDict()

    def _prune(self, key: str, now: float) -> deque[float]:
        q = self._events.get(key)
        if q is None:
            q = deque()
            self._events[key] = q
            while len(self._events) > self._max_keys:  # bound memory: evict least recently used key
                self._events.popitem(last=False)
        else:
            self._events.move_to_end(key)
        cutoff = now - self.window_s
        while q and q[0] <= cutoff:
            q.popleft()
        return q

    def retry_after(self, key: str) -> float | None:
        """Seconds until a slot frees up, or None if a request is allowed now. Does not consume."""
        now = self._clock()
        q = self._prune(key, now)
        if len(q) < self.limit:
            return None
        return max(0.001, q[0] + self.window_s - now)

    def consume(self, key: str) -> None:
        now = self._clock()
        self._prune(key, now).append(now)

    def check(self, key: str) -> float | None:
        wait = self.retry_after(key)
        if wait is None:
            self.consume(key)
        return wait

    def remaining(self, key: str) -> int:
        return max(0, self.limit - len(self._prune(key, self._clock())))
