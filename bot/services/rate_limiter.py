"""
bot/services/rate_limiter.py - Async Token-Bucket Rate Limiter with HTTP 429 Backoff.

Enforces maximum 25.0 messages/second ceiling with burst capacity up to 25.0 tokens
and mandatory (retry_after + 1.0s) pause on Telegram HTTP 429.
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional


class TokenBucketRateLimiter:
    """
    Thread-safe and async token-bucket rate limiter.

    Enforces maximum throughput (default 25 msg/s) and initial burst allowance.
    On Telegram HTTP 429, dynamic backoff pauses token acquisition for
    retry_after + 1.0 second.
    """

    def __init__(
        self,
        rate: float = 25.0,
        capacity: float = 25.0,
        burst: Optional[float] = None,
    ) -> None:
        self.rate = float(rate)
        effective_capacity = float(burst if burst is not None else capacity)
        self._capacity = effective_capacity
        self.tokens = effective_capacity
        self.last_update = time.monotonic()
        self.backoff_until = 0.0
        self._lock = asyncio.Lock()

    @property
    def capacity(self) -> float:
        """Maximum token capacity."""
        return self._capacity

    @capacity.setter
    def capacity(self, value: float) -> None:
        self._capacity = float(value)

    @property
    def burst(self) -> float:
        """Burst capacity alias."""
        return self._capacity

    @burst.setter
    def burst(self, value: float) -> None:
        self._capacity = float(value)

    @property
    def is_in_backoff(self) -> bool:
        """Check whether rate limiter is currently paused due to HTTP 429 backoff."""
        return time.monotonic() < self.backoff_until

    def replenish(self) -> None:
        """Replenish tokens based on elapsed monotonic time."""
        now = time.monotonic()
        elapsed = now - self.last_update
        self.tokens = min(self._capacity, self.tokens + elapsed * self.rate)
        self.last_update = now

    async def acquire(self) -> float:
        """
        Asynchronously acquire 1 token, waiting if bucket is exhausted or in 429 backoff.
        Returns total seconds waited.
        """
        total_waited = 0.0
        async with self._lock:
            while True:
                now = time.monotonic()
                if now < self.backoff_until:
                    sleep_time = self.backoff_until - now
                    await asyncio.sleep(sleep_time)
                    total_waited += sleep_time
                    self.replenish()
                    continue

                self.replenish()
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return total_waited

                needed = 1.0 - self.tokens
                sleep_time = needed / self.rate
                await asyncio.sleep(sleep_time)
                total_waited += sleep_time

    def handle_retry_after(self, seconds: float) -> None:
        """
        Pause all token acquisition for seconds + 1.0 second upon HTTP 429.
        """
        backoff_delay = float(seconds) + 1.0
        self.backoff_until = max(self.backoff_until, time.monotonic() + backoff_delay)

    def handle_429(self, retry_after: float) -> None:
        """Alias for handle_retry_after."""
        self.handle_retry_after(retry_after)
