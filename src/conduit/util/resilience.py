"""Retry, rate limiting and circuit breaking for outbound calls.

Private trackers throttle aggressively and Plex/qBittorrent go away without
warning. The reference project wrapped every call in a bare ``try/except`` that
swallowed the error and returned ``None``, so a rate-limit response looked
exactly like "no results" -- and the scheduler would happily keep hammering.

These three primitives fix that: retries with jittered backoff, a token-bucket
limiter that keeps us inside the tracker's budget, and a breaker that stops
calling a dead service instead of timing out on every tick.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from ..logs import get_logger

log = get_logger("resilience")

T = TypeVar("T")


class TransientError(Exception):
    """A failure worth retrying (timeout, 5xx, connection reset)."""


class RateLimited(TransientError):
    """Upstream asked us to slow down. Carries the advised wait, if any."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class CircuitOpen(Exception):
    """The breaker is open; the call was not attempted."""


class PermanentError(Exception):
    """A failure that will not fix itself (bad credentials, 404, bad request)."""


@dataclass(slots=True)
class RetryPolicy:
    attempts: int = 4
    base_delay: float = 0.5
    max_delay: float = 20.0
    jitter: float = 0.3

    def delay_for(self, attempt: int) -> float:
        raw = min(self.base_delay * (2 ** (attempt - 1)), self.max_delay)
        return raw * (1 + random.uniform(-self.jitter, self.jitter))


async def retry(
    fn: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy | None = None,
    name: str = "call",
) -> T:
    """Run ``fn`` with exponential backoff on :class:`TransientError`."""
    policy = policy or RetryPolicy()
    last: Exception | None = None
    for attempt in range(1, max(policy.attempts, 1) + 1):
        try:
            return await fn()
        except RateLimited as exc:
            last = exc
            wait = exc.retry_after if exc.retry_after is not None else policy.delay_for(attempt)
            # A tracker asking for an hour's pause would otherwise park the
            # whole search pass here. Give up and let the next cycle retry.
            if attempt == policy.attempts or wait > policy.max_delay:
                break
            log.warning(
                "rate limited, backing off", extra={"call": name, "wait": round(wait, 2)}
            )
            await asyncio.sleep(wait)
        except TransientError as exc:
            last = exc
            if attempt == policy.attempts:
                break
            wait = policy.delay_for(attempt)
            log.debug(
                "transient failure, retrying",
                extra={"call": name, "attempt": attempt, "wait": round(wait, 2), "err": str(exc)},
            )
            await asyncio.sleep(wait)
    # Not an assert: `python -O` strips those, and this line is the only thing
    # standing between a swallowed failure and a confusing `None` return.
    if last is None:  # pragma: no cover -- unreachable while attempts >= 1
        raise TransientError(f"{name}: no attempt was made")
    raise last


class RateLimiter:
    """Async token bucket. ``rate_per_minute <= 0`` disables limiting."""

    def __init__(self, rate_per_minute: int, burst: int | None = None) -> None:
        self.rate = max(rate_per_minute, 0) / 60.0
        self.capacity = float(burst if burst is not None else max(rate_per_minute, 1))
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, cost: float = 1.0) -> None:
        if self.rate <= 0:
            return
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
                self._updated = now
                if self._tokens >= cost:
                    self._tokens -= cost
                    return
                await asyncio.sleep((cost - self._tokens) / self.rate)


@dataclass
class CircuitBreaker:
    """Trips after ``threshold`` consecutive failures, recovers via half-open probe."""

    name: str
    threshold: int = 5
    recovery_seconds: float = 60.0
    _failures: int = field(default=0, init=False)
    _opened_at: float | None = field(default=None, init=False)

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        if time.monotonic() - self._opened_at >= self.recovery_seconds:
            return "half_open"
        return "open"

    def _check(self) -> None:
        if self.state == "open":
            remaining = self.recovery_seconds - (time.monotonic() - (self._opened_at or 0))
            raise CircuitOpen(f"{self.name} unavailable, retrying in {remaining:.0f}s")

    def record_success(self) -> None:
        if self._opened_at is not None:
            log.info("service recovered", extra={"service": self.name})
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures < self.threshold:
            return
        was_closed = self._opened_at is None
        # Also restarts the clock after a failed half-open probe. Without that
        # the breaker stayed half-open for good once it had tripped once, and
        # every call went straight through to the dead service.
        self._opened_at = time.monotonic()
        if was_closed:
            log.error(
                "circuit opened after repeated failures",
                extra={"service": self.name, "failures": self._failures},
            )

    async def call(self, fn: Callable[[], Awaitable[T]]) -> T:
        self._check()
        try:
            result = await fn()
        except PermanentError:
            raise  # config problems must not trip the breaker
        except Exception:
            self.record_failure()
            raise
        self.record_success()
        return result

    def snapshot(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state,
            "failures": self._failures,
            "opened_at": self._opened_at,
        }
