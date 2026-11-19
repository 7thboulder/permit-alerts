"""Settings, read from environment variables or a .env file.

Every setting can be given as an environment variable prefixed with
PERMIT_ALERTS_, e.g. PERMIT_ALERTS_DISCORD_TOKEN. On a desktop install they
live in the .env file inside the data folder; on a server they are usually
real environment variables.
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def default_data_dir() -> Path:
    override = os.environ.get("PERMIT_ALERTS_DATA_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / "permit-alerts"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "permit-alerts"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "permit-alerts"


DATA_DIR = default_data_dir()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PERMIT_ALERTS_",
        env_file=(str(DATA_DIR / ".env"), ".env"),
        extra="ignore",
    )

    data_dir: Path = DATA_DIR
    # Any SQLAlchemy URL. SQLite by default; set a postgresql+psycopg:// URL on a server.
    database_url: str = ""

    # Dashboard
    host: str = "127.0.0.1"
    port: int = 8645
    # Set this before exposing the dashboard beyond your own computer (e.g. on a server).
    dashboard_password: str = ""

    # Checking
    default_interval_seconds: int = 45
    min_interval_seconds: int = 15
    reminder_after_minutes: int = 60
    request_timeout_seconds: float = 20.0

    # Discord: a bot that manages one server ("guild").
    discord_token: str = ""
    discord_guild_id: int = 0
    discord_category_name: str = "Permit Alerts"

    # Email (SMTP). For Gmail: smtp.gmail.com, port 587, and an app password.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""

    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{(self.data_dir / 'permit_alerts.db').as_posix()}"

    @property
    def discord_enabled(self) -> bool:
        return bool(self.discord_token and self.discord_guild_id)

    @property
    def email_enabled(self) -> bool:
        return bool(self.smtp_host and self.smtp_from)


@lru_cache
def get_settings() -> Settings:
    return Settings()
