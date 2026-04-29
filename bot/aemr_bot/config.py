from pathlib import Path
from typing import Literal

from pydantic import Field
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
    webhook_host: str = Field("0.0.0.0", alias="WEBHOOK_HOST")
    webhook_port: int = Field(8080, alias="WEBHOOK_PORT")

    database_url: str = Field(..., alias="DATABASE_URL")

    admin_group_id: int | None = Field(None, alias="ADMIN_GROUP_ID")
    coordinator_max_user_id: int | None = Field(None, alias="COORDINATOR_MAX_USER_ID")

    timezone: str = Field("Asia/Kamchatka", alias="TZ")
    sla_response_hours: int = Field(4, alias="SLA_RESPONSE_HOURS")
    appeal_collect_timeout_seconds: int = Field(60, alias="APPEAL_TIMEOUT")
    answer_max_chars: int = Field(300, alias="ANSWER_MAX_CHARS")

    backup_s3_endpoint: str | None = Field(None, alias="BACKUP_S3_ENDPOINT")
    backup_s3_bucket: str | None = Field(None, alias="BACKUP_S3_BUCKET")
    backup_s3_access_key: str | None = Field(None, alias="BACKUP_S3_ACCESS_KEY")
    backup_s3_secret_key: str | None = Field(None, alias="BACKUP_S3_SECRET_KEY")
    backup_gpg_passphrase: str | None = Field(None, alias="BACKUP_GPG_PASSPHRASE")

    healthcheck_url: str | None = Field(None, alias="HEALTHCHECK_URL")

    seed_dir: Path = Field(Path("/app/seed"), alias="SEED_DIR")
    log_level: str = Field("INFO", alias="LOG_LEVEL")


settings = Settings()
