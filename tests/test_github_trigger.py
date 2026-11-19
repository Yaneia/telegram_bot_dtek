"""
tests/test_github_trigger.py - Unit tests for GitHubWorkflowTrigger.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import patch

from bot.services.github_trigger import GitHubWorkflowTrigger


class TestGitHubWorkflowTrigger(unittest.IsolatedAsyncioTestCase):
    """Tests for periodic GitHub Actions workflow_dispatch triggering."""

    def test_disabled_without_token(self) -> None:
        trigger = GitHubWorkflowTrigger(token="", interval_seconds=900)
        self.assertFalse(trigger.is_enabled)

        trigger_zero = GitHubWorkflowTrigger(token="ghp_test", interval_seconds=0)
        self.assertFalse(trigger_zero.is_enabled)

        trigger_ok = GitHubWorkflowTrigger(token="ghp_test", interval_seconds=900)
        self.assertTrue(trigger_ok.is_enabled)
        self.assertEqual(
            trigger_ok.dispatch_url,
            "https://api.github.com/repos/Yaneia/telegram_bot_dtek/actions/workflows/scrape.yml/dispatches",
        )

    async def test_trigger_workflow_success_urllib_fallback(self) -> None:
        trigger = GitHubWorkflowTrigger(
            token="ghp_secret_123",
            repo="Yaneia/telegram_bot_dtek",
            workflow="scrape.yml",
            ref="main",
        )
        captured: dict = {}

        def fake_post(url: str, headers: dict, payload: dict) -> tuple[int, str]:
            captured["url"] = url
            captured["headers"] = headers
            captured["payload"] = payload
            return 204, ""

        with patch("bot.services.github_trigger._HTTPX_AVAILABLE", False), patch.object(
            trigger, "_post_urllib", side_effect=fake_post
        ):
            ok = await trigger.trigger_workflow()

        self.assertTrue(ok)
        self.assertEqual(
            captured["url"],
            "https://api.github.com/repos/Yaneia/telegram_bot_dtek/actions/workflows/scrape.yml/dispatches",
        )
        self.assertEqual(captured["headers"]["Authorization"], "Bearer ghp_secret_123")
        self.assertEqual(captured["headers"]["Accept"], "application/vnd.github+json")
        self.assertEqual(captured["payload"], {"ref": "main"})

    async def test_trigger_workflow_handles_http_error_and_exception(self) -> None:
        trigger = GitHubWorkflowTrigger(token="ghp_bad", repo="Yaneia/telegram_bot_dtek")

        with patch("bot.services.github_trigger._HTTPX_AVAILABLE", False), patch.object(
            trigger, "_post_urllib", return_value=(401, json.dumps({"message": "Bad credentials"}))
        ):
            self.assertFalse(await trigger.trigger_workflow())

        with patch("bot.services.github_trigger._HTTPX_AVAILABLE", False), patch.object(
            trigger, "_post_urllib", side_effect=OSError("network down")
        ):
            self.assertFalse(await trigger.trigger_workflow())

    async def test_start_and_stop_lifecycle(self) -> None:
        disabled = GitHubWorkflowTrigger(token="", interval_seconds=900)
        await disabled.start()
        self.assertIsNone(disabled._task)
        await disabled.stop()

        trigger = GitHubWorkflowTrigger(
            token="ghp_test",
            interval_seconds=60,
            initial_delay_seconds=0,
        )
        calls = 0

        async def fake_trigger() -> bool:
            nonlocal calls
            calls += 1
            return True

        with patch.object(trigger, "trigger_workflow", side_effect=fake_trigger):
            await trigger.start()
            self.assertIsNotNone(trigger._task)
            await asyncio.sleep(0.05)
            await trigger.stop()

        self.assertGreaterEqual(calls, 1)
        self.assertIsNone(trigger._task)


if __name__ == "__main__":
    unittest.main()
