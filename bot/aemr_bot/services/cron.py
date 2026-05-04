from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

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
        CronTrigger(
            day_of_week=settings.backup_day_of_week,
            hour=settings.backup_hour,
            minute=settings.backup_minute,
            timezone=TZ,
        ),
        name="db-backup",
        max_instances=1,
        coalesce=True,
    )

    last_alert_state = {"healthy": True}

    async def selfcheck():
        from aemr_bot.health import heartbeat
        was_healthy = last_alert_state["healthy"]
        is_healthy = heartbeat.is_fresh()
        if was_healthy and not is_healthy:
            await send_admin_text(
                "⚠️ Бот не отвечает на проверку здоровья (heartbeat stale). "
                "Возможно завис главный цикл — проверьте логи и перезапустите контейнер."
            )
        elif not was_healthy and is_healthy:
            await send_admin_text("✅ Бот восстановил отзывчивость.")
        last_alert_state["healthy"] = is_healthy

    scheduler.add_job(
        selfcheck,
        CronTrigger(minute=f"*/{settings.healthcheck_interval_minutes}", timezone=TZ),
        name="health-selfcheck",
        max_instances=1,
        coalesce=True,
    )

    async def monthly_report():
        try:
            async with session_scope() as session:
                content, title, count = await stats_service.build_xlsx(session, "month")
            filename = f"appeals_month_{datetime.now(TZ):%Y-%m-%d}.xlsx"
            await send_admin_document(
                filename=filename,
                content=content,
                caption=f"📊 Статистика {title} ({count} обращений)",
            )
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
            CronTrigger(minute=f"*/{settings.healthcheck_interval_minutes}", timezone=TZ),
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


def _build_pg_env() -> dict[str, str]:
    parsed = urlparse(settings.database_url.replace("+asyncpg", ""))
    return {
        **os.environ,
        "PGHOST": parsed.hostname or "localhost",
        "PGPORT": str(parsed.port or 5432),
        "PGUSER": parsed.username or "",
        "PGPASSWORD": parsed.password or "",
        "PGDATABASE": (parsed.path or "/").lstrip("/"),
    }


def _rotate_backups(directory: Path, keep: int, suffix: str) -> None:
    """Drop oldest backup files beyond `keep`. Sorted by mtime descending."""
    files = sorted(
        directory.glob(f"aemr-*{suffix}"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in files[keep:]:
        try:
            old.unlink()
            log.info("backup rotated: removed %s", old.name)
        except Exception:
            log.warning("failed to remove old backup %s", old, exc_info=True)


async def _run_pg_dump(out_path: Path, env: dict[str, str]) -> None:
    """Plain `pg_dump > out_path` via asyncio.subprocess so the event loop
    keeps spinning. Used when gpg encryption is disabled."""
    with open(out_path, "wb") as f:
        proc = await asyncio.create_subprocess_exec(
            "pg_dump", "--no-owner", "--no-acl",
            stdout=f,
            env=env,
        )
        rc = await proc.wait()
    if rc != 0:
        raise RuntimeError(f"pg_dump failed with code {rc}")


async def _run_pg_dump_encrypted(
    out_path: Path, env: dict[str, str], passphrase: str
) -> None:
    """`pg_dump | gpg --symmetric > out_path`. Passphrase via os.pipe to keep
    it out of argv and shell."""
    r_fd, w_fd = os.pipe()
    os.write(w_fd, passphrase.encode())
    os.close(w_fd)

    dump = await asyncio.create_subprocess_exec(
        "pg_dump", "--no-owner", "--no-acl",
        stdout=asyncio.subprocess.PIPE,
        env=env,
    )
    if dump.stdout is None:
        os.close(r_fd)
        raise RuntimeError("pg_dump did not provide stdout pipe")
    try:
        gpg = await asyncio.create_subprocess_exec(
            "gpg", "--batch", "--yes",
            "--passphrase-fd", str(r_fd),
            "--symmetric", "--cipher-algo", "AES256",
            "-o", str(out_path),
            stdin=dump.stdout,  # type: ignore[arg-type]  # StreamReader works at runtime
            pass_fds=(r_fd,),
        )
    finally:
        os.close(r_fd)

    gpg_rc, dump_rc = await asyncio.gather(gpg.wait(), dump.wait())
    if gpg_rc != 0:
        raise RuntimeError(f"gpg failed with code {gpg_rc}")
    if dump_rc != 0:
        raise RuntimeError(f"pg_dump failed with code {dump_rc}")


async def _upload_to_s3(out_path: Path) -> None:
    """Optional S3 upload via rclone. Raises on failure — caller decides
    whether to swallow."""
    if not (
        settings.backup_s3_bucket
        and settings.backup_s3_endpoint
        and settings.backup_s3_access_key
        and settings.backup_s3_secret_key
    ):
        return
    rclone = await asyncio.create_subprocess_exec(
        "rclone", "copy", str(out_path),
        f":s3,provider=Other,"
        f"access_key_id={settings.backup_s3_access_key},"
        f"secret_access_key={settings.backup_s3_secret_key},"
        f"endpoint={settings.backup_s3_endpoint}:{settings.backup_s3_bucket}/",
    )
    rc = await rclone.wait()
    if rc != 0:
        raise RuntimeError(f"rclone failed with code {rc}")
    log.info("backup uploaded to s3: %s", out_path.name)


async def _backup_db() -> Path | None:
    """Weekly pg_dump → optional gpg → save locally → optional S3.

    Self-hosted-friendly: by default only saves to a local volume
    (`/backups`) with rotation. S3 and gpg are both opt-in via env.
    Returns the path on disk if a backup was successfully written, None
    on failure. Caller `_backup_db_job` swallows exceptions and logs.
    """
    local_dir = (
        Path(settings.backup_local_dir) if settings.backup_local_dir else None
    )
    if local_dir is None:
        log.info("backup skipped: BACKUP_LOCAL_DIR is empty")
        return None

    target_dir = local_dir
    target_dir.mkdir(parents=True, exist_ok=True)

    passphrase = settings.backup_gpg_passphrase
    encrypt = bool(passphrase)
    suffix = ".sql.gpg" if encrypt else ".sql"
    ts = datetime.now(TZ).strftime("%Y%m%d_%H%M%S")
    out = target_dir / f"aemr-{ts}{suffix}"

    env = _build_pg_env()
    try:
        if encrypt:
            await _run_pg_dump_encrypted(out, env, passphrase or "")
        else:
            await _run_pg_dump(out, env)
        log.info("backup written: %s (%d bytes)", out.name, out.stat().st_size)
    except Exception:
        log.exception("backup failed during pg_dump")
        try:
            out.unlink(missing_ok=True)
        except Exception:
            pass
        return None

    # Rotation — keep last N files in this directory.
    _rotate_backups(target_dir, settings.backup_keep_count, suffix)

    # Optional remote upload — failure here doesn't invalidate the local copy.
    try:
        await _upload_to_s3(out)
    except Exception:
        log.exception("backup s3 upload failed (local copy still intact)")

    return out
