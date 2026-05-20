"""
Retry utility for async API calls — exponential backoff with jitter.

Designed for LLM provider calls (Anthropic, OpenAI-compatible) where transient
failures (rate limits, network blips, 5xx) should be retried, but permanent
errors (auth, bad request) should surface immediately.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from functools import wraps
from typing import Any, Awaitable, Callable, Iterable, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass
class RetryPolicy:
    max_attempts: int = 3
    initial_delay: float = 1.0
    max_delay: float = 30.0
    backoff_factor: float = 2.0
    jitter: float = 0.25   # +/- fraction randomized

    def __post_init__(self) -> None:
        # Validate at construction time so a mis-configured policy fails fast
        # rather than at the bottom of the retry loop where the error is far
        # from the caller. The previous `assert last_exc is not None` after
        # the loop relied on max_attempts > 0 silently.
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.initial_delay < 0:
            raise ValueError("initial_delay must be >= 0")
        if self.max_delay < self.initial_delay:
            raise ValueError("max_delay must be >= initial_delay")
        if self.backoff_factor < 1.0:
            raise ValueError("backoff_factor must be >= 1.0")
        if not (0.0 <= self.jitter <= 1.0):
            raise ValueError("jitter must be in [0.0, 1.0]")


# Errors we consider transient (retryable) — string match against repr(exc).
# Kept as strings to avoid importing anthropic / openai at this layer.
_TRANSIENT_MARKERS: tuple[str, ...] = (
    "RateLimitError", "APITimeoutError", "APIConnectionError",
    "InternalServerError", "ServiceUnavailable", "ReadTimeout",
    "ConnectionError", "ConnectTimeout", "RemoteProtocolError",
    "overloaded", "temporarily", "timed out", "timeout",
    "502", "503", "504", "429",
)


def is_transient(exc: BaseException) -> bool:
    """Best-effort classification of LLM provider errors."""
    if isinstance(exc, (asyncio.TimeoutError, ConnectionError)):
        return True
    text = f"{type(exc).__name__}: {exc!s}"
    return any(marker in text for marker in _TRANSIENT_MARKERS)


def _next_delay(policy: RetryPolicy, attempt: int) -> float:
    base = min(policy.max_delay, policy.initial_delay * (policy.backoff_factor ** attempt))
    jitter_span = base * policy.jitter
    return max(0.0, base + random.uniform(-jitter_span, jitter_span))


async def call_with_retry(
    fn: Callable[..., Awaitable[T]],
    *args: Any,
    policy: Optional[RetryPolicy] = None,
    label: str = "llm-call",
    transient: Callable[[BaseException], bool] = is_transient,
    **kwargs: Any,
) -> T:
    """
    Invoke an async callable with retry + exponential backoff.

    Raises the last exception if all attempts fail. Non-transient errors
    bypass retry and raise immediately.
    """
    pol = policy or RetryPolicy()
    last_exc: Optional[BaseException] = None

    for attempt in range(pol.max_attempts):
        t0 = time.monotonic()
        try:
            result = await fn(*args, **kwargs)
            if attempt > 0:
                logger.info(
                    "%s succeeded after %d retries (%.2fs)",
                    label, attempt, time.monotonic() - t0,
                )
            return result
        except Exception as exc:   # noqa: BLE001
            last_exc = exc
            if not transient(exc) or attempt == pol.max_attempts - 1:
                logger.warning(
                    "%s failed permanently (attempt %d/%d): %s",
                    label, attempt + 1, pol.max_attempts, exc,
                )
                raise
            delay = _next_delay(pol, attempt)
            logger.warning(
                "%s transient error (attempt %d/%d, retrying in %.1fs): %s",
                label, attempt + 1, pol.max_attempts, delay, exc,
            )
            await asyncio.sleep(delay)

    # `RetryPolicy.__post_init__` guarantees max_attempts >= 1, so the loop
    # always sets last_exc on the failure path. This branch is unreachable in
    # normal operation; raise a defensive RuntimeError if we ever land here.
    raise last_exc if last_exc is not None else RuntimeError(f"{label}: retry loop exited without result")


def with_retry(
    policy: Optional[RetryPolicy] = None,
    label: Optional[str] = None,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Decorator form of `call_with_retry`."""
    def decorator(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        name = label or fn.__name__

        @wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            return await call_with_retry(fn, *args, policy=policy, label=name, **kwargs)

        return wrapper
    return decorator
