"""Runtime configuration loaded from environment variables."""

from dataclasses import dataclass
import os
from typing import Optional


@dataclass(frozen=True)
class Settings:
    grocy_base_url: Optional[str]
    grocy_api_key: Optional[str]
    home_assistant_webhook_url: Optional[str]
    expiry_window_days: int = 7
    top_ingredient_limit: int = 8

    @property
    def grocy_enabled(self) -> bool:
        return bool(self.grocy_base_url and self.grocy_api_key)


def load_settings() -> Settings:
    return Settings(
        grocy_base_url=_clean_url(os.getenv("FOODBRAIN_GROCY_BASE_URL")),
        grocy_api_key=_blank_to_none(os.getenv("FOODBRAIN_GROCY_API_KEY")),
        home_assistant_webhook_url=_blank_to_none(
            os.getenv("FOODBRAIN_HOME_ASSISTANT_WEBHOOK_URL")
        ),
        expiry_window_days=_int_env("FOODBRAIN_EXPIRY_WINDOW_DAYS", 7),
        top_ingredient_limit=_int_env("FOODBRAIN_TOP_INGREDIENT_LIMIT", 8),
    )


def _blank_to_none(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _clean_url(value: Optional[str]) -> Optional[str]:
    cleaned = _blank_to_none(value)
    if cleaned is None:
        return None
    return cleaned.rstrip("/")


def _int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None or raw_value.strip() == "":
        return default
    try:
        return int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
