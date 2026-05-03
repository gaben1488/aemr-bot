from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    bot_token: str = Field(..., alias="BOT_TOKEN")
    bot_mode: Literal["polling", "webhook"] = Field("polling", alias="BOT_MODE")
    webhook_url: str | None = Field(None, alias="WEBHOOK_URL")
    webhook_secret: str | None = Field(None, alias="WEBHOOK_SECRET")
    webhook_host: str = Field("0.0.0.0", alias="WEBHOOK_HOST")  # nosec B104 — bind inside container, Nginx fronts the public port
    webhook_port: int = Field(8080, alias="WEBHOOK_PORT")

    database_url: str = Field(..., alias="DATABASE_URL")

    admin_group_id: int | None = Field(None, alias="ADMIN_GROUP_ID")
    coordinator_max_user_id: int | None = Field(None, alias="COORDINATOR_MAX_USER_ID")

    timezone: str = Field("Asia/Kamchatka", alias="TZ")
    sla_response_hours: int = Field(4, alias="SLA_RESPONSE_HOURS")
    appeal_collect_timeout_seconds: int = Field(60, alias="APPEAL_TIMEOUT")
    answer_max_chars: int = Field(300, alias="ANSWER_MAX_CHARS")
    name_max_chars: int = Field(120, alias="NAME_MAX_CHARS")
    address_max_chars: int = Field(500, alias="ADDRESS_MAX_CHARS")
    # Per-appeal hard caps. summary 2000 leaves headroom inside the 4000-char
    # admin card; 20 attachments is generous for a single citizen complaint.
    summary_max_chars: int = Field(2000, alias="SUMMARY_MAX_CHARS")
    attachments_max_per_appeal: int = Field(20, alias="ATTACHMENTS_MAX_PER_APPEAL")
    # MAX server attachment-per-message limit isn't documented; chunk relay
    # output to be safe.
    attachments_per_relay_message: int = Field(10, alias="ATTACHMENTS_PER_RELAY_MESSAGE")
    recover_batch_size: int = Field(1000, alias="RECOVER_BATCH_SIZE")

    healthcheck_stale_seconds: int = Field(120, alias="HEALTHCHECK_STALE_SECONDS")
    healthcheck_pulse_seconds: int = Field(30, alias="HEALTHCHECK_PULSE_SECONDS")
    healthcheck_interval_minutes: int = Field(5, alias="HEALTHCHECK_INTERVAL_MIN")

    # Long-polling timeout passed to MAX getUpdates. Higher = fewer empty
    # round-trips when the bot is idle (better for the 2 RPS rate limit);
    # lower = faster startup-shutdown reaction window. Server cap is 90s.
    polling_timeout_seconds: int = Field(30, alias="POLLING_TIMEOUT_SECONDS", ge=0, le=90)

    # Broadcast / subscription. Rate-limit стоит ниже MAX-лимита 2 RPS, чтобы
    # обычная активность бота (ответы оператора, новые карточки) не упиралась
    # в потолок одновременно с рассылкой.
    broadcast_max_chars: int = Field(1000, alias="BROADCAST_MAX_CHARS")
    broadcast_rate_limit_per_sec: float = Field(
        1.0, alias="BROADCAST_RATE_LIMIT_PER_SEC"
    )
    broadcast_progress_update_sec: int = Field(
        5, alias="BROADCAST_PROGRESS_UPDATE_SEC"
    )
    broadcast_wizard_ttl_sec: int = Field(300, alias="BROADCAST_WIZARD_TTL_SEC")

    backup_hour: int = Field(3, alias="BACKUP_HOUR")
    backup_minute: int = Field(0, alias="BACKUP_MINUTE")
    backup_tmp_dir: str = Field("/tmp", alias="BACKUP_TMP_DIR")  # nosec B108 — container-local, override via env

    backup_s3_endpoint: str | None = Field(None, alias="BACKUP_S3_ENDPOINT")
    backup_s3_bucket: str | None = Field(None, alias="BACKUP_S3_BUCKET")
    backup_s3_access_key: str | None = Field(None, alias="BACKUP_S3_ACCESS_KEY")
    backup_s3_secret_key: str | None = Field(None, alias="BACKUP_S3_SECRET_KEY")
    backup_gpg_passphrase: str | None = Field(None, alias="BACKUP_GPG_PASSPHRASE")

    healthcheck_url: str | None = Field(None, alias="HEALTHCHECK_URL")

    seed_dir: Path = Field(Path("/app/seed"), alias="SEED_DIR")
    log_level: str = Field("INFO", alias="LOG_LEVEL")

    @field_validator(
        "admin_group_id",
        "coordinator_max_user_id",
        "webhook_url",
        "webhook_secret",
        "backup_s3_endpoint",
        "backup_s3_bucket",
        "backup_s3_access_key",
        "backup_s3_secret_key",
        "backup_gpg_passphrase",
        "healthcheck_url",
        mode="before",
    )
    @classmethod
    def _empty_str_to_none(cls, v):
        # Treat empty string and stray inline comments from .env as None for optional fields.
        if isinstance(v, str):
            stripped = v.strip()
            if not stripped or stripped.startswith("#"):
                return None
        return v

    @model_validator(mode="after")
    def _enforce_webhook_secret(self):
        if self.bot_mode == "webhook":
            if not self.webhook_url:
                raise ValueError("WEBHOOK_URL is required when BOT_MODE=webhook")
            if not self.webhook_secret or len(self.webhook_secret) < 16:
                raise ValueError(
                    "WEBHOOK_SECRET is required and must be at least 16 chars when BOT_MODE=webhook. "
                    "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(32))\""
                )
        return self


settings = Settings()
