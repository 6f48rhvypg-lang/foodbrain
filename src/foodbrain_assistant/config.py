"""Runtime configuration loaded from environment variables."""

from dataclasses import dataclass
import os
from pathlib import Path
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


def load_settings(env_file: Optional[Path] = None) -> Settings:
    file_values = _load_env_file(env_file or Path(".env"))

    return Settings(
        grocy_base_url=_clean_url(_setting("FOODBRAIN_GROCY_BASE_URL", file_values)),
        grocy_api_key=_blank_to_none(_setting("FOODBRAIN_GROCY_API_KEY", file_values)),
        home_assistant_webhook_url=_blank_to_none(
            _setting("FOODBRAIN_HOME_ASSISTANT_WEBHOOK_URL", file_values)
        ),
        expiry_window_days=_int_setting("FOODBRAIN_EXPIRY_WINDOW_DAYS", 7, file_values),
        top_ingredient_limit=_int_setting("FOODBRAIN_TOP_INGREDIENT_LIMIT", 8, file_values),
    )


def _load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            raise ValueError(f"{path}:{line_number} must be KEY=VALUE")

        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"{path}:{line_number} is missing a variable name")
        values[key] = _strip_quotes(value.strip())

    return values


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _setting(name: str, file_values: dict[str, str]) -> Optional[str]:
    return os.getenv(name, file_values.get(name))


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


def _int_setting(name: str, default: int, file_values: dict[str, str]) -> int:
    raw_value = _setting(name, file_values)
    if raw_value is None or raw_value.strip() == "":
        return default
    try:
        return int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
