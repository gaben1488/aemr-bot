from __future__ import annotations

import logging
import subprocess
from datetime import datetime
from pathlib import Path

import aiohttp
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from zoneinfo import ZoneInfo

from aemr_bot.config import settings
from aemr_bot.db.session import session_scope
from aemr_bot.services import stats as stats_service

log = logging.getLogger(__name__)
TZ = ZoneInfo(settings.timezone)


def build_scheduler(send_admin_document, send_admin_text) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=TZ)

    scheduler.add_job(
        _backup_db,
        CronTrigger(hour=3, minute=0, timezone=TZ),
        name="db-backup",
        max_instances=1,
        coalesce=True,
    )

    async def monthly_report():
        try:
            async with session_scope() as session:
                content, title, count = await stats_service.build_xlsx(session, "month")
            filename = f"appeals_month_{datetime.now(TZ):%Y-%m-%d}.xlsx"
            await send_admin_document(filename=filename, content=content, caption=f"📊 Статистика {title} ({count} обращений)")
        except Exception:
            log.exception("monthly report failed")

    scheduler.add_job(
        monthly_report,
        CronTrigger(day=1, hour=9, minute=0, timezone=TZ),
        name="monthly-stats",
        max_instances=1,
        coalesce=True,
    )

    if settings.healthcheck_url:
        scheduler.add_job(
            _ping_healthcheck,
            CronTrigger(minute="*/5", timezone=TZ),
            name="healthcheck-ping",
            max_instances=1,
            coalesce=True,
        )

    return scheduler


async def _ping_healthcheck() -> None:
    if not settings.healthcheck_url:
        return
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            await s.get(settings.healthcheck_url)
    except Exception:
        log.warning("healthcheck ping failed", exc_info=True)


def _backup_db() -> None:
    if not all([settings.backup_s3_bucket, settings.backup_gpg_passphrase]):
        log.info("backup skipped: S3 or GPG passphrase not configured")
        return

    try:
        from urllib.parse import urlparse

        parsed = urlparse(settings.database_url.replace("+asyncpg", ""))
        env = {
            "PGHOST": parsed.hostname or "localhost",
            "PGPORT": str(parsed.port or 5432),
            "PGUSER": parsed.username or "",
            "PGPASSWORD": parsed.password or "",
            "PGDATABASE": (parsed.path or "/").lstrip("/"),
        }

        ts = datetime.now(TZ).strftime("%Y%m%d_%H%M%S")
        out = Path(f"/tmp/aemr-{ts}.sql.gpg")

        cmd = (
            "pg_dump --no-owner --no-acl "
            f"| gpg --batch --yes --passphrase '{settings.backup_gpg_passphrase}' "
            f"--symmetric --cipher-algo AES256 -o {out}"
        )
        subprocess.run(["sh", "-c", cmd], check=True, env={**env})

        rclone_cmd = [
            "rclone", "copy", str(out),
            f":s3,provider=Other,access_key_id={settings.backup_s3_access_key},"
            f"secret_access_key={settings.backup_s3_secret_key},"
            f"endpoint={settings.backup_s3_endpoint}:{settings.backup_s3_bucket}/",
        ]
        subprocess.run(rclone_cmd, check=True)
        out.unlink(missing_ok=True)
        log.info("backup done: %s", out.name)
    except Exception:
        log.exception("backup failed")
