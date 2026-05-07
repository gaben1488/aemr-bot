from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import delete
from zoneinfo import ZoneInfo

from aemr_bot.config import settings
from aemr_bot.db.models import Event
from aemr_bot.db.session import session_scope
from aemr_bot.services import stats as stats_service

log = logging.getLogger(__name__)
TZ = ZoneInfo(settings.timezone)


def build_scheduler(send_admin_document, send_admin_text) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=TZ)

    async def backup_with_alert():
        """Обёртка над _backup_db: упавший еженедельный дамп должен
        быть громким, а не тихим.

        Без этого сломанная цепочка бэкапов (нет места на диске, пропал
        gpg-ключ, поменялись права Postgres) видна только в логах бота,
        а их никто не читает в воскресенье в 03:00. Админ-группа должна
        узнать утром, до следующего еженедельного запуска.
        """
        try:
            out = await _backup_db()
            if out is None:
                await send_admin_text(
                    "⚠️ Еженедельный бэкап БД не выполнен. См. логи бота: "
                    "обычно это либо BACKUP_LOCAL_DIR не задан, либо упал "
                    "pg_dump. Сделайте /backup вручную, как только разберётесь."
                )
        except Exception:
            log.exception("backup_with_alert wrapper failed")
            await send_admin_text(
                "⚠️ Еженедельный бэкап БД упал с исключением. "
                "Срочно проверьте логи и снимите бэкап вручную через /backup."
            )

    scheduler.add_job(
        backup_with_alert,
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

    async def events_retention():
        """Удалить события старше 30 дней.

        Таблица events нужна для защиты от повторов (idempotency): ключ
        нужно помнить ровно столько, сколько MAX может повторно отдать
        тот же Update. Через 30 дней это исчезающе маловероятно, иначе
        таблица растёт без ограничений вместе с полными полезными
        нагрузками (Update payload), которые содержат персональные
        данные граждан.
        """
        try:
            cutoff = datetime.now(TZ) - timedelta(days=30)
            async with session_scope() as session:
                result = await session.execute(
                    delete(Event).where(Event.received_at < cutoff)
                )
                purged = result.rowcount or 0
            if purged:
                log.info("events retention: purged %d rows older than %s", purged, cutoff.date())
        except Exception:
            log.exception("events retention failed")

    scheduler.add_job(
        events_retention,
        CronTrigger(hour=4, minute=0, timezone=TZ),
        name="events-retention",
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

    async def pulse():
        """Шлёт в служебную группу короткое подтверждение «бот жив».

        Расписание двухрежимное:
        • В нерабочее время (22:00–08:59 по Камчатке и воскресенье) —
          раз в час, в минуту :05. Достаточно, чтобы заметить «процесс
          упал и не перезапустился» к началу рабочего дня.
        • В рабочее время (пн–сб, 09:00–17:59) — каждые полчаса
          (минуты :00 и :30). Команде важно видеть, что бот жив именно
          когда жители активно пишут.

        Минута :05 в нерабочем режиме и :00/:30 в рабочем выбраны так,
        чтобы пульс не сливался с SLA-алёртом (минута :10) — операторы
        видят два разных по смыслу сообщения отдельно.

        Это второй контур мониторинга поверх selfcheck: тот ловит
        зависший event-loop, а пульс — ситуацию «процесс упал,
        контейнер не перезапустился». Без этого внешнего сигнала
        тишина легко проходит мимо.
        """
        try:
            now = datetime.now(TZ).strftime("%H:%M")
            await send_admin_text(f"🟢 Бот работает. {now}")
        except Exception:
            log.exception("pulse failed")

    # Нерабочий пульс: раз в час, минута :05. Только когда «не рабочее
    # время» — в крон-выражении ниже это часы 22..23 и 0..8, плюс
    # воскресенье (день недели sun). Часы и минуты вычисляются
    # cron-trigger'ом строго в TZ (Камчатка).
    scheduler.add_job(
        pulse,
        CronTrigger(
            day_of_week="mon-sat",
            hour="0-8,22,23",
            minute=5,
            timezone=TZ,
        ),
        name="pulse-offhours",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        pulse,
        CronTrigger(day_of_week="sun", hour="*", minute=5, timezone=TZ),
        name="pulse-sunday",
        max_instances=1,
        coalesce=True,
    )
    # Рабочий пульс: пн–сб, 09:00–17:59 по Камчатке, каждые 30 мин (:00 и :30).
    scheduler.add_job(
        pulse,
        CronTrigger(
            day_of_week="mon-sat",
            hour="9-17",
            minute="0,30",
            timezone=TZ,
        ),
        name="pulse-workhours",
        max_instances=1,
        coalesce=True,
    )

    async def sla_overdue_check():
        """Раз в час проверяем, какие обращения висят дольше SLA без ответа,
        и пушим список в служебную группу. Тишина в чате намеренна:
        если ничего не просрочено — нет сообщения. Иначе оператор
        получит «по нулям» каждый час, привыкнет, перестанет читать.

        Лимит до 10 строк в одном сообщении: больше — значит у команды
        проблема нагрузки, нужно открывать /open_tickets и разбираться.
        """
        try:
            from aemr_bot.services import appeals as appeals_service

            async with session_scope() as session:
                overdue = await appeals_service.find_overdue_unanswered(
                    session, settings.sla_response_hours
                )
            if not overdue:
                return
            now = datetime.now(TZ)
            lines = [
                f"⚠️ Просрочено по SLA ({settings.sla_response_hours}ч): "
                f"{len(overdue)} обращ."
            ]
            for ap in overdue[:10]:
                created_local = ap.created_at.astimezone(TZ) if ap.created_at else now
                age_h = int((now - created_local).total_seconds() // 3600)
                name = (ap.user.first_name or "—") if ap.user else "—"
                lines.append(
                    f"• #{ap.id} · {name} · {ap.locality or '—'} · "
                    f"висит {age_h}ч"
                )
            if len(overdue) > 10:
                lines.append(f"… и ещё {len(overdue) - 10}. Откройте «📋 Открытые обращения».")
            await send_admin_text("\n".join(lines))
        except Exception:
            log.exception("sla_overdue_check failed")

    scheduler.add_job(
        sla_overdue_check,
        # На 10-й минуте каждого часа — чтобы пульс (минута :00) и
        # SLA-проверка не сливались в одно неразличимое сообщение.
        CronTrigger(minute=10, timezone=TZ),
        name="sla-overdue-check",
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
    """Удалить самые старые файлы бэкапов сверх `keep`. Сортировка по mtime по убыванию."""
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
    """Простой `pg_dump > out_path` через asyncio.subprocess, чтобы
    цикл событий не блокировался. Используется, когда шифрование gpg
    выключено."""
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
    """`pg_dump | gpg --symmetric > out_path`. Парольная фраза через
    os.pipe, чтобы она не попала в argv и в shell."""
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
            stdin=dump.stdout,  # type: ignore[arg-type]  # StreamReader работает в рантайме
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
    """Опциональная загрузка в S3 через rclone. Бросает исключение при
    сбое, вызывающий код сам решает, проглатывать его или нет.

    Учётные данные передаются через переменные окружения
    RCLONE_CONFIG_*, а не в строке подключения через argv. Форма с argv
    (`access_key=...`) утекает через `ps`, `/proc/<pid>/cmdline` и любой
    сборщик логов docker-compose, делающий снимки процессов: это совсем
    не то место, где должны лежать секреты. Форма с env-переменными
    держит их внутри процесса rclone.
    """
    if not (
        settings.backup_s3_bucket
        and settings.backup_s3_endpoint
        and settings.backup_s3_access_key
        and settings.backup_s3_secret_key
    ):
        return
    env = os.environ.copy()
    env.update({
        "RCLONE_CONFIG_BACKUPS3_TYPE": "s3",
        "RCLONE_CONFIG_BACKUPS3_PROVIDER": "Other",
        "RCLONE_CONFIG_BACKUPS3_ACCESS_KEY_ID": settings.backup_s3_access_key,
        "RCLONE_CONFIG_BACKUPS3_SECRET_ACCESS_KEY": settings.backup_s3_secret_key,
        "RCLONE_CONFIG_BACKUPS3_ENDPOINT": settings.backup_s3_endpoint,
    })
    rclone = await asyncio.create_subprocess_exec(
        "rclone", "copy", str(out_path),
        f"backups3:{settings.backup_s3_bucket}/",
        env=env,
    )
    rc = await rclone.wait()
    if rc != 0:
        raise RuntimeError(f"rclone failed with code {rc}")
    log.info("backup uploaded to s3: %s", out_path.name)


async def _backup_db() -> Path | None:
    """Еженедельный pg_dump → опционально gpg → сохранить локально →
    опционально S3.

    Сделано под self-hosted: по умолчанию сохраняет только в локальный
    том (`/backups`) с ротацией. S3 и gpg включаются через переменные
    окружения. Возвращает путь к успешно записанному бэкапу либо None
    при сбое. Вызывающий код `_backup_db_job` глотает исключения и
    пишет в лог.
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
        # Ужесточить права до 0600. Дамп содержит телефоны пользователей,
        # тексты обращений и операторский audit-лог. Стандартная umask
        # контейнера оставляет файл доступным на чтение всем, а это
        # неприемлемо для артефакта с персональными данными даже внутри
        # тома одного арендатора.
        try:
            os.chmod(out, 0o600)
        except OSError:
            log.warning("could not chmod backup %s to 0600", out.name)
        log.info("backup written: %s (%d bytes)", out.name, out.stat().st_size)
    except Exception:
        log.exception("backup failed during pg_dump")
        try:
            out.unlink(missing_ok=True)
        except Exception:
            pass
        return None

    # Ротация: оставляем последние N файлов в этом каталоге.
    _rotate_backups(target_dir, settings.backup_keep_count, suffix)

    # Опциональная отправка наружу: сбой здесь не отменяет локальную копию.
    try:
        await _upload_to_s3(out)
    except Exception:
        log.exception("backup s3 upload failed (local copy still intact)")

    return out
