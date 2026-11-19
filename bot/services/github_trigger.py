"""
bot/services/github_trigger.py - Periodic GitHub Actions workflow_dispatch trigger.

Replaces unreliable GitHub Actions `schedule:` cron triggers by dispatching
`scrape.yml` from the bot's own async loop using a GitHub API token.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

try:
    import httpx  # type: ignore[import-untyped]
    _HTTPX_AVAILABLE = True
except ImportError:
    _HTTPX_AVAILABLE = False

logger = logging.getLogger("bot.services.github_trigger")


class GitHubWorkflowTrigger:
    """Periodically triggers a GitHub Actions workflow via `workflow_dispatch`."""

    API_BASE = "https://api.github.com"

    def __init__(
        self,
        token: str,
        repo: str = "Yaneia/telegram_bot_dtek",
        workflow: str = "scrape.yml",
        ref: str = "main",
        interval_seconds: int = 900,
        initial_delay_seconds: int = 10,
        timeout_seconds: float = 15.0,
    ) -> None:
        self.token = (token or "").strip()
        self.repo = (repo or "").strip().strip("/")
        self.workflow = (workflow or "scrape.yml").strip()
        self.ref = (ref or "main").strip()
        self.interval_seconds = max(0, int(interval_seconds))
        self.initial_delay_seconds = max(0, int(initial_delay_seconds))
        self.timeout_seconds = float(timeout_seconds)
        self._task: Optional[asyncio.Task[Any]] = None

    @property
    def is_enabled(self) -> bool:
        return bool(self.token and self.repo and self.workflow and self.interval_seconds > 0)

    @property
    def dispatch_url(self) -> str:
        return (
            f"{self.API_BASE}/repos/{self.repo}/actions/workflows/"
            f"{self.workflow}/dispatches"
        )

    def _headers(self) -> Dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "telegram_bot_dtek",
            "Content-Type": "application/json",
        }

    async def trigger_workflow(self) -> bool:
        """Send a single `workflow_dispatch` POST request. Returns True on HTTP 204."""
        if not self.token or not self.repo or not self.workflow:
            return False

        url = self.dispatch_url
        headers = self._headers()
        payload = {"ref": self.ref}

        try:
            if _HTTPX_AVAILABLE:
                async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                    response = await client.post(url, headers=headers, json=payload)
                status_code = response.status_code
                body = response.text
            else:
                status_code, body = await asyncio.to_thread(
                    self._post_urllib, url, headers, payload
                )

            if status_code == 204:
                logger.info(
                    "Triggered GitHub workflow '%s' on %s (ref=%s).",
                    self.workflow,
                    self.repo,
                    self.ref,
                )
                return True

            logger.warning(
                "GitHub workflow dispatch failed for %s (%s): HTTP %s - %s",
                self.workflow,
                self.repo,
                status_code,
                (body or "")[:300],
            )
            return False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "GitHub workflow dispatch error for %s (%s): %s",
                self.workflow,
                self.repo,
                exc,
            )
            return False

    def _post_urllib(
        self, url: str, headers: Dict[str, str], payload: Dict[str, Any]
    ) -> tuple[int, str]:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                return int(resp.status), resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as err:
            body = err.read().decode("utf-8", errors="replace") if err.fp else str(err)
            return int(err.code), body

    async def _loop(self) -> None:
        if self.initial_delay_seconds > 0:
            await asyncio.sleep(self.initial_delay_seconds)

        while True:
            await self.trigger_workflow()
            await asyncio.sleep(self.interval_seconds)

    async def start(self) -> None:
        """Start the periodic GitHub workflow dispatch background task if enabled."""
        if not self.is_enabled:
            logger.info(
                "GitHub workflow trigger disabled (GITHUB_TOKEN not set or interval <= 0)."
            )
            return
        if self._task is not None and not self._task.done():
            return

        logger.info(
            "Starting GitHub workflow trigger for %s/%s every %ss (ref=%s).",
            self.repo,
            self.workflow,
            self.interval_seconds,
            self.ref,
        )
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Stop the periodic background task gracefully."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None


__all__ = ["GitHubWorkflowTrigger"]
