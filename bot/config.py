"""
bot/config.py - Environment configuration and production settings.

Supports pydantic-settings with graceful standard library fallback
adapter `_BaseSettingsCompat` for offline runtimes and test environments.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any, Dict

try:
    from pydantic_settings import BaseSettings, SettingsConfigDict  # type: ignore[import-untyped]
    _PYDANTIC_SETTINGS_AVAILABLE = True
except ImportError:
    _PYDANTIC_SETTINGS_AVAILABLE = False


class _BaseSettingsCompat:
    """Standard library fallback for pydantic-settings in offline runtimes."""

    def __init__(self, **values: Any) -> None:
        annotations = getattr(self.__class__, "__annotations__", {})
        # Load from class defaults
        for name in dir(self.__class__):
            if not name.startswith("_") and not callable(getattr(self.__class__, name)):
                setattr(self, name, getattr(self.__class__, name))
        # Override from os.environ
        for key, val in os.environ.items():
            attr_name = key.upper()
            if hasattr(self, attr_name) or attr_name in annotations:
                expected_type = annotations.get(attr_name, str)
                setattr(self, attr_name, self._cast(val, expected_type))
        # Override from constructor values
        for key, val in values.items():
            setattr(self, key, val)

    @staticmethod
    def _cast(val: str, target_type: Any) -> Any:
        if target_type is bool or target_type == "bool":
            return val.lower() in ("1", "true", "yes", "on")
        if target_type is int or target_type == "int":
            return int(val)
        if target_type is float or target_type == "float":
            return float(val)
        return str(val)

    def model_dump(self) -> Dict[str, Any]:
        return {
            k: getattr(self, k)
            for k in dir(self)
            if not k.startswith("_") and not callable(getattr(self, k))
        }


if _PYDANTIC_SETTINGS_AVAILABLE:
    class Settings(BaseSettings):  # type: ignore[misc]
        model_config = SettingsConfigDict(
            env_file=".env",
            env_file_encoding="utf-8",
            extra="ignore",
        )

        BOT_TOKEN: str = ""
        WEBAPP_URL: str = "https://yaneia.github.io/telegram_bot_dtek"
        DATABASE_PATH: str = "data/bot.db"
        HEARTBEAT_PATH: str = "/tmp/bot_heartbeat"
        SCRAPE_INTERVAL_SECONDS: int = 600
        NOTIFICATION_CHECK_INTERVAL_SECONDS: int = 60
        MOCK_FALLBACK_ONLY: bool = False
        ENABLE_DIRECT_SCRAPE: bool = False
        LOG_LEVEL: str = "INFO"
        BOT_USERNAME: str = "dtek_tracker_bot"
        MAX_RATE_PER_SECOND: float = 25.0
        CIRCUIT_BREAKER_RESET_TIMEOUT: int = 900
        DEFAULT_RENDER_MODE: str = "image"

        # Compatibility aliases
        RATE_LIMIT_RPS: float = 25.0
        TIMEZONE: str = "Europe/Kyiv"
else:
    class Settings(_BaseSettingsCompat):  # type: ignore[misc]
        BOT_TOKEN: str = ""
        WEBAPP_URL: str = "https://yaneia.github.io/telegram_bot_dtek"
        DATABASE_PATH: str = "data/bot.db"
        HEARTBEAT_PATH: str = "/tmp/bot_heartbeat"
        SCRAPE_INTERVAL_SECONDS: int = 600
        NOTIFICATION_CHECK_INTERVAL_SECONDS: int = 60
        MOCK_FALLBACK_ONLY: bool = False
        ENABLE_DIRECT_SCRAPE: bool = False
        LOG_LEVEL: str = "INFO"
        BOT_USERNAME: str = "dtek_tracker_bot"
        MAX_RATE_PER_SECOND: float = 25.0
        CIRCUIT_BREAKER_RESET_TIMEOUT: int = 900
        DEFAULT_RENDER_MODE: str = "image"

        # Compatibility aliases
        RATE_LIMIT_RPS: float = 25.0
        TIMEZONE: str = "Europe/Kyiv"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached production settings instance."""
    return Settings()


__all__ = ["Settings", "get_settings"]
