"""
tests/test_challenger_m5_rate_limiter.py - Adversarial Stress & Empirical Verification for Milestone 5.

Focuses strictly on TokenBucketRateLimiter:
1. 50 concurrent tasks acquiring tokens (strict <= 25.0 msg/s ceiling).
2. HTTP 429 backoff handling: all tasks wait >= retry_after + 1.0s.
3. Mid-flight 429 interruptions & multiple backoff extensions.
4. Replenishment accuracy over time & capacity clamping.
5. Task cancellation resilience, lock release & token preservation.
6. High concurrency (100 coroutines) stress harness.
"""

from __future__ import annotations

import asyncio
import time
import unittest
from unittest.mock import patch

from bot.services.rate_limiter import TokenBucketRateLimiter


class TestTokenBucketRateLimiterEmpiricalStress(unittest.TestCase):
    """Adversarial stress testing suite for TokenBucketRateLimiter."""

    def test_50_concurrent_tasks_zero_burst_rate_ceiling(self) -> None:
        """
        Verify that 50 concurrent coroutines acquiring tokens with zero initial burst
        strictly observe the <= 25.0 msg/s throughput ceiling.
        """
        async def run_scenario() -> None:
            limiter = TokenBucketRateLimiter(rate=25.0, capacity=25.0)
            limiter.tokens = 0.0
            limiter.last_update = time.monotonic()

            t0 = time.monotonic()
            timestamps: list[float] = []

            async def worker(idx: int) -> None:
                await limiter.acquire()
                timestamps.append(time.monotonic())

            await asyncio.gather(*[worker(i) for i in range(50)])
            t_total = time.monotonic() - t0

            # 50 tokens at 25 tokens/s with 0 initial burst must take at least 50 / 25 = 2.0s
            self.assertGreaterEqual(
                t_total,
                1.95,
                f"Completed too fast: {t_total:.4f}s for 50 tokens (expected >= 1.95s)",
            )
            rate = len(timestamps) / t_total
            self.assertLessEqual(
                rate,
                25.1,
                f"Rate ceiling violated: {rate:.2f} msg/s (expected <= 25.0)",
            )

        asyncio.run(run_scenario())

    def test_50_concurrent_tasks_initial_burst_sustained_ceiling(self) -> None:
        """
        Verify that with initial burst capacity (25.0), the first 25 tokens are consumed
        immediately, and subsequent 25 tokens strictly follow the 25.0 msg/s replenishment ceiling.
        """
        async def run_scenario() -> None:
            limiter = TokenBucketRateLimiter(rate=25.0, capacity=25.0)
            t0 = time.monotonic()
            completed_at: list[float] = []

            async def worker(idx: int) -> None:
                await limiter.acquire()
                completed_at.append(time.monotonic() - t0)

            await asyncio.gather(*[worker(i) for i in range(50)])
            completed_at.sort()

            # First 25 should finish nearly instantaneously (< 0.05s)
            burst_duration = completed_at[24]
            self.assertLess(
                burst_duration,
                0.05,
                f"Burst took unexpectedly long: {burst_duration:.4f}s",
            )

            # Subsequent 25 tokens (indices 25 to 49) must take >= 0.98s
            sustained_duration = completed_at[49] - completed_at[24]
            self.assertGreaterEqual(
                sustained_duration,
                0.98,
                f"Sustained flow took only {sustained_duration:.4f}s for 25 tokens (expected >= 0.98s)",
            )
            sustained_rate = 25.0 / sustained_duration
            self.assertLessEqual(
                sustained_rate,
                25.1,
                f"Sustained rate ceiling violated: {sustained_rate:.2f} msg/s (expected <= 25.0)",
            )

        asyncio.run(run_scenario())

    def test_concurrent_http_429_backoff_synchronization(self) -> None:
        """
        Verify that when HTTP 429 backoff is triggered (retry_after = 0.4s),
        all concurrent tasks wait at least retry_after + 1.0s (1.4s).
        """
        async def run_scenario() -> None:
            limiter = TokenBucketRateLimiter(rate=25.0, capacity=25.0)
            retry_after = 0.4
            expected_wait = retry_after + 1.0  # 1.40s

            limiter.handle_429(retry_after)
            self.assertTrue(limiter.is_in_backoff)

            t0 = time.monotonic()
            wait_times: list[float] = []

            async def worker(idx: int) -> None:
                start = time.monotonic()
                await limiter.acquire()
                elapsed = time.monotonic() - start
                wait_times.append(elapsed)

            tasks = [asyncio.create_task(worker(i)) for i in range(20)]
            await asyncio.gather(*tasks)

            self.assertFalse(limiter.is_in_backoff)
            self.assertEqual(len(wait_times), 20)
            for i, waited in enumerate(wait_times):
                self.assertGreaterEqual(
                    waited,
                    expected_wait - 0.02,
                    f"Task {i} did not wait minimum backoff: waited {waited:.4f}s < {expected_wait}s",
                )

        asyncio.run(run_scenario())

    def test_mid_flight_http_429_backoff_interruption(self) -> None:
        """
        Verify that when handle_429 is triggered mid-flight while tasks are already
        blocked waiting on acquire(), all tasks wait at least retry_after + 1.0s
        from the moment backoff was signaled.
        """
        async def run_scenario() -> None:
            limiter = TokenBucketRateLimiter(rate=25.0, capacity=25.0)
            limiter.tokens = 0.0
            limiter.last_update = time.monotonic()

            finish_times: dict[int, float] = {}

            async def worker(idx: int) -> None:
                await limiter.acquire()
                finish_times[idx] = time.monotonic()

            tasks = [asyncio.create_task(worker(i)) for i in range(15)]
            # Allow tasks to start and enter acquire()
            await asyncio.sleep(0.01)

            t_trigger = time.monotonic()
            retry_after = 0.35
            expected_min_finish = t_trigger + retry_after + 1.0  # +1.35s
            limiter.handle_429(retry_after)

            await asyncio.gather(*tasks)

            # All tasks completing after 429 trigger must complete at or after expected_min_finish
            for idx, ftime in finish_times.items():
                if ftime > t_trigger + 0.05:
                    self.assertGreaterEqual(
                        ftime,
                        expected_min_finish - 0.02,
                        f"Task {idx} finished before mandatory backoff expired: "
                        f"{ftime - t_trigger:.4f}s < {retry_after + 1.0}s",
                    )

        asyncio.run(run_scenario())

    def test_multiple_extending_429_backoffs(self) -> None:
        """
        Verify that sequential/overlapping 429 signals extend the backoff window to max
        and do not regress or shorten an existing backoff window.
        """
        limiter = TokenBucketRateLimiter(rate=25.0, capacity=25.0)
        t_now = time.monotonic()

        # First backoff: 0.5s -> backoff until t_now + 1.5s
        limiter.handle_retry_after(0.5)
        initial_backoff = limiter.backoff_until
        self.assertGreaterEqual(initial_backoff, t_now + 1.49)

        # Shorter backoff received: 0.1s -> should NOT shorten existing backoff
        limiter.handle_retry_after(0.1)
        self.assertEqual(limiter.backoff_until, initial_backoff)

        # Longer backoff received: 2.0s -> should extend backoff
        limiter.handle_retry_after(2.0)
        self.assertGreaterEqual(limiter.backoff_until, initial_backoff + 0.5)

    def test_replenishment_accuracy_deterministic(self) -> None:
        """
        Verify replenishment calculations with deterministic monotonic clock mock:
        - Exact linear token generation at 25.0 tokens/s.
        - Clamping strictly at capacity.
        - Partial fractional tokens.
        """
        limiter = TokenBucketRateLimiter(rate=25.0, capacity=25.0)
        limiter.tokens = 0.0

        with patch("time.monotonic") as mock_time:
            mock_time.return_value = 1000.0
            limiter.last_update = 1000.0

            # 40ms -> exactly 1.0 token
            mock_time.return_value = 1000.040
            limiter.replenish()
            self.assertAlmostEqual(limiter.tokens, 1.0, places=5)

            # 200ms -> +5.0 tokens (total 6.0 tokens)
            mock_time.return_value = 1000.240
            limiter.replenish()
            self.assertAlmostEqual(limiter.tokens, 6.0, places=5)

            # 1.0s elapsed from 1000.240 -> +25.0 tokens, clamped at capacity 25.0
            mock_time.return_value = 1001.240
            limiter.replenish()
            self.assertEqual(limiter.tokens, 25.0)

            # 1000.0s elapsed -> still strictly clamped at 25.0
            mock_time.return_value = 2001.240
            limiter.replenish()
            self.assertEqual(limiter.tokens, 25.0)

    def test_cancellation_resilience_and_token_preservation(self) -> None:
        """
        Verify that cancelling a task blocked inside acquire() cleanly releases the lock
        and does not leak or incorrectly deduct tokens.
        """
        async def run_scenario() -> None:
            limiter = TokenBucketRateLimiter(rate=25.0, capacity=25.0)
            limiter.tokens = 0.0
            limiter.last_update = time.monotonic()

            async def cancelled_task() -> None:
                await limiter.acquire()

            task = asyncio.create_task(cancelled_task())
            await asyncio.sleep(0.01)  # Enter acquire and wait for replenishment
            task.cancel()

            with self.assertRaises(asyncio.CancelledError):
                await task

            # Subsequent acquire should succeed without deadlock once tokens replenish
            await asyncio.sleep(0.05)  # Replenish ~1.25 tokens
            waited = await limiter.acquire()
            self.assertLessEqual(waited, 0.02)

        asyncio.run(run_scenario())

    def test_capacity_and_burst_property_sync(self) -> None:
        """Verify capacity and burst properties stay synchronized and constrain replenishment."""
        limiter = TokenBucketRateLimiter(rate=10.0, burst=15.0)
        self.assertEqual(limiter.capacity, 15.0)
        self.assertEqual(limiter.burst, 15.0)

        limiter.capacity = 30.0
        self.assertEqual(limiter.burst, 30.0)

        limiter.burst = 20.0
        self.assertEqual(limiter.capacity, 20.0)

    def test_stress_100_concurrent_requests(self) -> None:
        """
        Stress test with 100 concurrent requests:
        Verifies zero deadlocks, zero uncaught exceptions, and that all 100 requests
        are serviced with the throughput rate capped strictly at <= 25.0 msg/s.
        """
        async def run_scenario() -> None:
            limiter = TokenBucketRateLimiter(rate=25.0, capacity=25.0)
            limiter.tokens = 0.0
            limiter.last_update = time.monotonic()

            t0 = time.monotonic()
            results: list[int] = []

            async def worker(idx: int) -> None:
                await limiter.acquire()
                results.append(idx)

            await asyncio.gather(*[worker(i) for i in range(100)])
            t_total = time.monotonic() - t0

            self.assertEqual(len(results), 100)
            # 100 tokens at 25/s with 0 burst must take >= 3.90s
            self.assertGreaterEqual(
                t_total,
                3.90,
                f"100 tokens acquired too fast: {t_total:.4f}s (rate: {100 / t_total:.2f} msg/s)",
            )
            rate = 100.0 / t_total
            self.assertLessEqual(rate, 25.1)

        asyncio.run(run_scenario())


if __name__ == "__main__":
    unittest.main()
