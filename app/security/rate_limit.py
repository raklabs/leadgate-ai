"""Sliding-window rate limiter to prevent LLM Denial-of-Wallet and webhook flood attacks."""

from __future__ import annotations

import time
from collections import deque
from threading import Lock


class SlidingWindowRateLimiter:
    """Thread-safe per-client sliding-window rate limiter."""

    def __init__(self, max_requests: int = 120, window_seconds: int = 60) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._buckets: dict[str, deque[float]] = {}
        self._lock = Lock()

    def allow_request(self, client_key: str, now: float | None = None) -> tuple[bool, int]:
        """Return (allowed, retry_after_seconds)."""
        current = now if now is not None else time.monotonic()
        cutoff = current - self.window_seconds

        with self._lock:
            bucket = self._buckets.setdefault(client_key, deque())
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()

            if len(bucket) >= self.max_requests:
                retry_after = max(1, int(self.window_seconds - (current - bucket[0])))
                return False, retry_after

            bucket.append(current)
            return True, 0

