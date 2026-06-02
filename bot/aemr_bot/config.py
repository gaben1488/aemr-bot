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
    # Слушаем внутри контейнера; наружу сервис выставляет Nginx.
    webhook_host: str = Field("0.0.0.0", alias="WEBHOOK_HOST")  # nosec
    webhook_port: int = Field(8080, alias="WEBHOOK_PORT")

    database_url: str = Field(..., alias="DATABASE_URL")

    admin_group_id: int | None = Field(None, alias="ADMIN_GROUP_ID")

    # Cold-start первого IT-оператора без psql. При первом старте, если в
    # таблице operators нет ни одной активной записи с ролью `it`, бот
    # вставит её из этих env-переменных. На повторных стартах — no-op.
    bootstrap_it_max_user_id: int | None = Field(
        None, alias="BOOTSTRAP_IT_MAX_USER_ID"
    )
    bootstrap_it_full_name: str | None = Field(
        None, alias="BOOTSTRAP_IT_FULL_NAME"
    )

    timezone: str = Field("Asia/Kamchatka", alias="TZ")
    sla_response_hours: int = Field(4, alias="SLA_RESPONSE_HOURS")
    appeal_collect_timeout_seconds: int = Field(60, alias="APPEAL_TIMEOUT")
    answer_max_chars: int = Field(300, alias="ANSWER_MAX_CHARS")
    name_max_chars: int = Field(120, alias="NAME_MAX_CHARS")
    address_max_chars: int = Field(500, alias="ADDRESS_MAX_CHARS")
    # Жёсткие ограничения на одно обращение. summary 2000 оставляет запас
    # внутри 4000-символьной карточки в админке; 20 вложений — с запасом
    # для одного обращения от жителя.
    summary_max_chars: int = Field(2000, alias="SUMMARY_MAX_CHARS")
    attachments_max_per_appeal: int = Field(20, alias="ATTACHMENTS_MAX_PER_APPEAL")
    # Лимит на число вложений в одном сообщении сервера MAX не задокументирован;
    # на всякий случай режем пересылку на куски.
    attachments_per_relay_message: int = Field(10, alias="ATTACHMENTS_PER_RELAY_MESSAGE")
    recover_batch_size: int = Field(1000, alias="RECOVER_BATCH_SIZE")

    healthcheck_stale_seconds: int = Field(120, alias="HEALTHCHECK_STALE_SECONDS")
    healthcheck_pulse_seconds: int = Field(30, alias="HEALTHCHECK_PULSE_SECONDS")
    healthcheck_interval_minutes: int = Field(5, alias="HEALTHCHECK_INTERVAL_MIN")

    # Во сколько polling_timeout'ов разрешаем «застояться» last_poll_at,
    # прежде чем /livez покраснеет (perf-resilience finding b). При
    # use_create_task=True зависший handler/backup не гасит heartbeat-таск
    # (он лишь спит и бьёт) → ложно-зелёный /livez, авто-рестарта нет.
    # last_poll_at обновляется polling-обёрткой на каждом успешном
    # get_updates; если poll-цикл встал (заморожен event-loop, мёртвая
    # aiohttp-сессия), таймстемп протухает. 3× даёт запас на один
    # потерянный long-poll + сетевой джиттер, не поднимая ложную тревогу
    # на здоровом простаивающем боте. Это второй, независимый от
    # heartbeat сигнал живости (defense-in-depth).
    livez_poll_stale_factor: float = Field(
        3.0, alias="LIVEZ_POLL_STALE_FACTOR", ge=1.0, le=20.0
    )

    # Таймаут long-polling, передаваемый в MAX getUpdates. Больше — меньше
    # пустых обращений к серверу, когда бот простаивает (лучше для лимита
    # 2 RPS). Меньше — быстрее реагирует окно старта-остановки. Потолок
    # сервера 90 секунд.
    #
    # Держим строго НИЖЕ max_api_timeout_seconds на запас
    # polling_client_timeout_buffer_seconds: серверный hold long-poll
    # (этот параметр) не должен совпадать с клиентским ClientTimeout.total
    # (= max_api_timeout_seconds, общий для всей aiohttp-сессии). Когда оба
    # ~30с, каждый холостой цикл клиент рвёт AsyncioTimeoutError ровно в тот
    # момент, когда сервер собирался ответить пустым [] → переподключение
    # 24/7 (perf-resilience finding c). 20с + 10с буфера < 30с клиентского
    # потолка → клиент всегда ждёт чуть дольше сервера.
    polling_timeout_seconds: int = Field(20, alias="POLLING_TIMEOUT_SECONDS", ge=0, le=90)

    # Запас клиентского ClientTimeout.total над серверным long-poll hold.
    # build_bot() в polling-режиме поднимает потолок сессии до
    # max(max_api_timeout_seconds, polling_timeout_seconds + этот буфер),
    # гарантируя, что клиент не прервёт соединение раньше, чем сервер отдаст
    # пустой ответ. 10с с запасом перекрывают сетевой джиттер и время на
    # формирование ответа сервером.
    polling_client_timeout_buffer_seconds: float = Field(
        10.0, alias="POLLING_CLIENT_TIMEOUT_BUFFER_SECONDS", ge=1.0, le=60.0
    )

    # Таймаут на один HTTP-запрос к MAX API (send_message / edit_message
    # / answers). maxapi default = 150 секунд; при sequential polling
    # один тормозящий запрос блокирует все следующие тапы — видимое
    # «бот завис». 30 секунд — ack/send должны отвечать за секунды,
    # дольше = баг MAX, нет смысла блокировать оператора.
    max_api_timeout_seconds: float = Field(
        30.0, alias="MAX_API_TIMEOUT_SECONDS", gt=0.0, le=180.0
    )
    # Retry на 502/503/504 от MAX. Default maxapi = 3 с
    # экспоненциальным backoff (1s + 2s + 4s = 7s сверх timeout).
    # 1 retry достаточно для transient blips, быстрее провал лучше
    # для интерактивного UX оператора.
    max_api_retries: int = Field(
        1, alias="MAX_API_RETRIES", ge=0, le=5
    )

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

    # Расписание бэкапа: каждое воскресенье в 03:00 (по timezone бота).
    # day_of_week — crontab-style: sun, mon, ..., sat или "*" для каждого дня.
    backup_day_of_week: str = Field("sun", alias="BACKUP_DAY_OF_WEEK")
    backup_hour: int = Field(3, alias="BACKUP_HOUR")
    backup_minute: int = Field(0, alias="BACKUP_MINUTE")

    # Локальный бэкап: путь внутри контейнера, обычно смонтирован в named volume
    # `backups` (см. docker-compose). Если пусто — локальные бэкапы не сохраняются.
    backup_local_dir: str | None = Field("/backups", alias="BACKUP_LOCAL_DIR")
    # Сколько последних файлов хранить. 8 еженедельных ≈ 2 месяца истории.
    backup_keep_count: int = Field(8, alias="BACKUP_KEEP_COUNT")

    # gpg-шифрование опционально: если passphrase пустой — БЭКАП НЕ
    # СОЗДАЁТСЯ (SEC #2): plain SQL содержит phones, names, appeal texts —
    # это PII по 152-ФЗ. Хранить такой дамп на диске (тем более в S3)
    # без шифрования = breach. Чтобы запустить без gpg, явно установите
    # BACKUP_ALLOW_UNENCRYPTED=1 (dev/local-only).
    backup_gpg_passphrase: str | None = Field(None, alias="BACKUP_GPG_PASSPHRASE")
    backup_allow_unencrypted: bool = Field(
        False, alias="BACKUP_ALLOW_UNENCRYPTED"
    )

    # Таймауты дочерних процессов бэкапа (perf-resilience finding d). Без них
    # pg_dump/gpg/rclone ждут голым `await proc.wait()` бесконечно: повисший
    # S3-эндпоинт или зависший Postgres → backup-job висит вечно, точка
    # восстановления не создаётся, а категоризированный admin-алёрт никогда
    # не отправляется (cron-job не возвращается). По таймауту процесс
    # убивается (kill + reap), backup_db возвращает BackupResult(fail_kind=…)
    # → штатный алёрт уходит. pg_dump/gpg щедрее (большая БД может дампиться
    # минуты); rclone — сетевой шаг, режем агрессивнее.
    backup_pg_dump_timeout_seconds: float = Field(
        600.0, alias="BACKUP_PG_DUMP_TIMEOUT_SECONDS", gt=0.0, le=7200.0
    )
    backup_gpg_timeout_seconds: float = Field(
        600.0, alias="BACKUP_GPG_TIMEOUT_SECONDS", gt=0.0, le=7200.0
    )
    backup_rclone_timeout_seconds: float = Field(
        300.0, alias="BACKUP_RCLONE_TIMEOUT_SECONDS", gt=0.0, le=7200.0
    )

    # S3 опционально: если задан endpoint+bucket+keys, доп. заливаем в облако.
    # Пусто — храним только локально. Для self-hosted без облачного хранилища
    # оставить пустыми.
    backup_s3_endpoint: str | None = Field(None, alias="BACKUP_S3_ENDPOINT")
    backup_s3_bucket: str | None = Field(None, alias="BACKUP_S3_BUCKET")
    backup_s3_access_key: str | None = Field(None, alias="BACKUP_S3_ACCESS_KEY")
    backup_s3_secret_key: str | None = Field(None, alias="BACKUP_S3_SECRET_KEY")

    healthcheck_url: str | None = Field(None, alias="HEALTHCHECK_URL")

    # AuditLog retention (152-ФЗ / внутренний регламент): операторские
    # действия (block/unblock/reopen/close/erase/setting_update и пр.)
    # хранятся до N дней, потом ежедневная cron-job удаляет старые.
    # 365 дней — год аудита, типовая глубина расследования инцидента.
    # Внутри окна — полная история действий по жителю / настройке для
    # IT-аудита. После — следы стираются вместе с любым PII в details.
    audit_log_retention_days: int = Field(
        365, alias="AUDIT_LOG_RETENTION_DAYS", ge=30, le=3650
    )

    # SEC #5 — followup flood защита. Житель может закидать админ-чат
    # дополнениями (каждое = полная admin card + relay вложений).
    # Лимит per-appeal: max N follow-up'ов в час + min M секунд между
    # двумя followup'ами. Дефолты подобраны под нормальное общение
    # (несколько уточнений в сутки), но блокируют machine-spam.
    followup_max_per_hour_per_appeal: int = Field(
        5, alias="FOLLOWUP_MAX_PER_HOUR_PER_APPEAL", ge=1, le=100
    )
    followup_min_interval_seconds: int = Field(
        30, alias="FOLLOWUP_MIN_INTERVAL_SECONDS", ge=0, le=3600
    )

    seed_dir: Path = Field(Path("/app/seed"), alias="SEED_DIR")
    log_level: str = Field("INFO", alias="LOG_LEVEL")

    @field_validator(
        "admin_group_id",
        "bootstrap_it_max_user_id",
        "bootstrap_it_full_name",
        "webhook_url",
        "webhook_secret",
        "backup_local_dir",
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
        # Для необязательных полей пустую строку и случайные inline-комментарии из .env считаем None.
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
