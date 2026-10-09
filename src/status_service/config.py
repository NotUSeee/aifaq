from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    probe_base_url: str = Field("https://yourbot.gg", alias="PROBE_BASE_URL")
    probe_interval_seconds: int = Field(60, alias="PROBE_INTERVAL_SECONDS")
    # A failed website check is retried this many times in total before it
    # is believed (delays grow: 2s, then 4s with the defaults).
    probe_attempts: int = Field(3, alias="PROBE_ATTEMPTS")
    probe_retry_delay_seconds: float = Field(2.0, alias="PROBE_RETRY_DELAY_SECONDS")
    # Endpoints on unrelated networks that tell the monitor whether ITS OWN
    # internet connection works. If none answer, a failed check is recorded
    # as "no data" rather than as downtime. Blank disables the self-check.
    monitor_control_urls: str = Field(
        "https://www.gstatic.com/generate_204,https://cloudflare.com/cdn-cgi/trace",
        alias="MONITOR_CONTROL_URLS",
    )
    # Discord's public status summary (Statuspage JSON). Blank hides the row.
    discord_status_url: str = Field(
        "https://discordstatus.com/api/v2/summary.json", alias="DISCORD_STATUS_URL",
    )

    db_path: str = Field("/data/status.db", alias="DB_PATH")
    # Seconds the computed page snapshot is reused. Checks land once a minute,
    # so a short cache costs no freshness and spares the box under load.
    api_cache_seconds: float = Field(10.0, alias="API_CACHE_SECONDS")

    host: str = Field("0.0.0.0", alias="HOST")
    port: int = Field(8081, alias="PORT")

    discord_bot_token: str = Field("", alias="DISCORD_BOT_TOKEN")

    alert_discord_webhook_url: str = Field("", alias="ALERT_DISCORD_WEBHOOK_URL")
    alert_threshold_min: int = Field(3, alias="ALERT_THRESHOLD_MIN")
    alert_cooldown_min: int = Field(15, alias="ALERT_COOLDOWN_MIN")
    # "board" = ONE Discord message kept up to date via webhook edits
    # (no per-service spam). "stream" = legacy one-message-per-event.
    alert_style: str = Field("board", alias="ALERT_STYLE")
    # Board mode only: mention string (e.g. "<@&ROLE_ID>" or "@everyone")
    # posted as a separate ping when overall status becomes "outage";
    # the ping is deleted again on recovery. Empty = never ping.
    alert_outage_mention: str = Field("", alias="ALERT_OUTAGE_MENTION")
    # Cap on registered webhook subscribers (abuse guard).
    max_webhook_subscribers: int = Field(500, alias="MAX_WEBHOOK_SUBSCRIBERS")

    heartbeat_ping_url: str = Field("", alias="HEARTBEAT_PING_URL")

    admin_hmac_secret: str = Field("", alias="ADMIN_HMAC_SECRET")
    # Reports sent TO this service (ingest.py), each signed with its own
    # secret so that a leak of one sender cannot speak for the other, or for
    # the admin API. Blank (or under 32 characters) switches that route off.
    ingest_platform_secret: str = Field("", alias="INGEST_PLATFORM_SECRET")
    ingest_vantage_secret: str = Field("", alias="INGEST_VANTAGE_SECRET")
    # One-time bootstrap secret: while no owner account exists, visiting
    # /admin/setup?token=<this> lets the first owner create their account
    # (username + password + TOTP). Ignored once an owner exists.
    admin_bootstrap_token: str = Field("", alias="ADMIN_BOOTSTRAP_TOKEN")

    sla_target_pct: float = Field(99.9, alias="SLA_TARGET_PCT")

    ssl_warn_days: int = Field(14, alias="RR_SSL_WARN_DAYS")
    ssl_critical_days: int = Field(3, alias="RR_SSL_CRITICAL_DAYS")

    brand_discord_invite: str = Field("https://discord.gg/yourbot", alias="BRAND_DISCORD_INVITE")
    brand_bot_avatar_url: str = Field("", alias="BRAND_BOT_AVATAR_URL")
    brand_logo_url: str = Field("/static/yourbot-logo.png", alias="BRAND_LOGO_URL")
    brand_github_url: str = Field("https://github.com/EmberStream-Studio", alias="BRAND_GITHUB_URL")
    main_site_url: str = Field("https://yourbot.gg", alias="MAIN_SITE_URL")
    # Public URL of THIS status page — used in alert embeds and feed links.
    status_public_url: str = Field("https://status.yourbot.work", alias="STATUS_PUBLIC_URL")
    # Left-hand label on /badge.svg.
    badge_label: str = Field("yourbot", alias="BADGE_LABEL")


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    global _settings
    _settings = None
