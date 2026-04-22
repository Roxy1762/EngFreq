"""Minimal in-process token-bucket rate limiter.

Used to guard a handful of sensitive endpoints (login, registration) from
credential-stuffing and enumeration abuse without introducing a new
dependency. Counters are per-key sliding windows stored in memory; they are
reset on process restart and intentionally not shared between workers — the
goal is to raise the cost of a high-rate attack, not to provide perfect
accounting across a cluster.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict


@dataclass
class _Bucket:
    hits: Deque[float] = field(default_factory=deque)


class SlidingWindowLimiter:
    def __init__(self, *, max_hits: int, window_seconds: float) -> None:
        if max_hits <= 0:
            raise ValueError("max_hits must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self._max = max_hits
        self._window = window_seconds
        self._buckets: Dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> bool:
        """Return True when the request is allowed, False when throttled."""
        now = time.monotonic()
        cutoff = now - self._window
        with self._lock:
            bucket = self._buckets.setdefault(key, _Bucket())
            while bucket.hits and bucket.hits[0] < cutoff:
                bucket.hits.popleft()
            if len(bucket.hits) >= self._max:
                return False
            bucket.hits.append(now)
            return True

    def retry_after(self, key: str) -> float:
        """Seconds until the bucket next admits a request (0 when available)."""
        now = time.monotonic()
        cutoff = now - self._window
        with self._lock:
            bucket = self._buckets.get(key)
            if not bucket:
                return 0.0
            while bucket.hits and bucket.hits[0] < cutoff:
                bucket.hits.popleft()
            if len(bucket.hits) < self._max:
                return 0.0
            return max(0.0, bucket.hits[0] + self._window - now)
