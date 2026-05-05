from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    probe_base_url: str = Field("https://mmomaid.cloud", alias="PROBE_BASE_URL")
    probe_interval_seconds: int = Field(60, alias="PROBE_INTERVAL_SECONDS")

    db_path: str = Field("/data/status.db", alias="DB_PATH")

    host: str = Field("0.0.0.0", alias="HOST")
    port: int = Field(8081, alias="PORT")

    discord_bot_token: str = Field("", alias="DISCORD_BOT_TOKEN")

    alert_discord_webhook_url: str = Field("", alias="ALERT_DISCORD_WEBHOOK_URL")
    alert_threshold_min: int = Field(3, alias="ALERT_THRESHOLD_MIN")
    alert_cooldown_min: int = Field(15, alias="ALERT_COOLDOWN_MIN")

    heartbeat_ping_url: str = Field("", alias="HEARTBEAT_PING_URL")

    admin_hmac_secret: str = Field("", alias="ADMIN_HMAC_SECRET")

    sla_target_pct: float = Field(99.9, alias="SLA_TARGET_PCT")

    ssl_warn_days: int = Field(14, alias="RR_SSL_WARN_DAYS")
    ssl_critical_days: int = Field(3, alias="RR_SSL_CRITICAL_DAYS")

    brand_discord_invite: str = Field("https://discord.gg/mmomaid", alias="BRAND_DISCORD_INVITE")
    brand_bot_avatar_url: str = Field("", alias="BRAND_BOT_AVATAR_URL")
    brand_github_url: str = Field("", alias="BRAND_GITHUB_URL")


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    global _settings
    _settings = None
