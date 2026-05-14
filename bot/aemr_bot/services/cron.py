from __future__ import annotations

import asyncio
import functools
import logging
from datetime import datetime, timedelta, timezone

import aiohttp
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from sqlalchemy import delete
from zoneinfo import ZoneInfo

from aemr_bot.config import settings
from aemr_bot.db.models import Event
from aemr_bot.db.session import session_scope
from aemr_bot.services import stats as stats_service
from aemr_bot.services.calendar_ru import is_workday
from aemr_bot.services.db_backup import backup_db as _backup_db

log = logging.getLogger(__name__)
TZ = ZoneInfo(settings.timezone)


# Module-level state для selfcheck — раньше было local dict в closure.
# Хранит последний известный статус «бот отвечает», чтобы шлать алёрт
# при смене состояния (healthy → unhealthy или обратно), а не каждый
# тик cron'а.
_SELFCHECK_HEALTHY = {"healthy": True}

# Misfire grace window для всех cron jobs.
#
# APScheduler по умолчанию даёт job 1 секунду на отработку триггера —
# дальше тик помечается misfire и выбрасывается. При типичном
# `docker compose up --build` процесс стартует через 30–90 сек после
# триггера → cron'ы молча теряются (например pulse-tick в :05, а
# реальный старт scheduler в :07). 120 сек закрывают окно типичного
# compose-redeploy. Дублей не будет благодаря `coalesce=True` —
# несколько просроченных тиков сольются в один. Для daily/weekly job
# параметр безвреден — там тик раз в 86400 сек.
_MISFIRE_GRACE_SEC = 120

_ADMIN_SEND_RETRY_DELAYS_SEC = (2, 5, 10)


# ---- Module-level helpers (вынесены из build_scheduler для тестируемости) ----


def _format_appeal_lines(appeals: list, *, max_rows: int = 10) -> list[str]:
    """Унифицированный рендер строк «# id · имя · локалити · висит Nч»
    для напоминалок. Список ограничен max_rows; если приходится
    обрезать — добавляется хвостик «… ещё K».
    """
    now = datetime.now(TZ)
    lines: list[str] = []
    for ap in appeals[:max_rows]:
        created_local = (
            ap.created_at.astimezone(TZ) if ap.created_at else now
        )
        age_h = int((now - created_local).total_seconds() // 3600)
        name = (ap.user.first_name or "—") if ap.user else "—"
        lines.append(
            f"• #{ap.id} · {name} · {ap.locality or '—'} · "
            f"висит {age_h}ч"
        )
    if len(appeals) > max_rows:
        lines.append(f"… ещё {len(appeals) - max_rows}.")
    return lines


async def _send_with_open_tickets_button(bot, text: str) -> None:
    """Сообщение в админ-группу с кнопкой «📋 Открытые обращения»
    под ним. Используется напоминалками: оператор тапает и попадает
    в полный список с действиями.
    """
    if not settings.admin_group_id:
        return
    try:
        from maxapi.types import CallbackButton
        from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

        kb = InlineKeyboardBuilder()
        kb.row(
            CallbackButton(
                text="📋 Открытые обращения",
                payload="op:open_tickets",
            )
        )
        await bot.send_message(
            chat_id=settings.admin_group_id,
            text=text,
            attachments=[kb.as_markup()],
        )
    except Exception:
        log.exception("send admin reminder with button failed")


async def _send_admin_text_with_retry(send_admin_text, text: str, *, context: str) -> bool:
    """Отправить служебное сообщение в админ-группу с коротким retry.

    Пульс и сообщение о рестарте не должны теряться из-за одного
    сетевого сбоя MAX, короткого rate-limit или задержки сразу после
    старта контейнера. Если все попытки не удались, job не роняет
    scheduler-loop, но оставляет понятный лог.
    """
    attempts = len(_ADMIN_SEND_RETRY_DELAYS_SEC) + 1
    for attempt in range(1, attempts + 1):
        try:
            await send_admin_text(text)
            return True
        except Exception:
            if attempt >= attempts:
                log.exception(
                    "%s: не удалось отправить служебное сообщение после %d попыток",
                    context, attempts,
                )
                return False
            delay = _ADMIN_SEND_RETRY_DELAYS_SEC[attempt - 1]
            log.warning(
                "%s: отправка служебного сообщения не удалась, повтор через %s сек. "
                "Попытка %d/%d",
                context, delay, attempt, attempts,
                exc_info=True,
            )
            await asyncio.sleep(delay)
    return False


# ============================================================================
# Module-level cron jobs
# ============================================================================
# Все jobs принимают зависимости явными параметрами (раньше были
# captured через замыкание в build_scheduler). build_scheduler
# регистрирует их через functools.partial.
#
# Преимущества:
# - Тестируемость: можно мокать send_admin_text через AsyncMock.
# - Читаемость: build_scheduler стал ~80 строк конфигурации.
# - Импорты: каждая job делает свои локальные импорты внутри (как и
#   раньше), это разрывает потенциальные циклы services↔main.
# ============================================================================


async def _job_backup_with_alert(send_admin_text) -> None:
    """Обёртка над _backup_db: упавший еженедельный дамп должен быть
    громким, а не тихим.

    Без этого сломанная цепочка бэкапов (нет места на диске, пропал
    gpg-ключ, поменялись права Postgres) видна только в логах бота, а их
    никто не читает в воскресенье в 03:00. Админ-группа должна узнать
    утром, до следующего еженедельного запуска.
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


async def _job_events_retention() -> None:
    """Удалить события старше 30 дней.

    Таблица events нужна для защиты от повторов (idempotency): ключ
    нужно помнить ровно столько, сколько MAX может повторно отдать тот
    же Update. Через 30 дней это исчезающе маловероятно, иначе таблица
    растёт без ограничений вместе с полными полезными нагрузками
    (Update payload), которые содержат персональные данные граждан.
    """
    try:
        cutoff = datetime.now(TZ) - timedelta(days=30)
        async with session_scope() as session:
            result = await session.execute(
                delete(Event).where(Event.received_at < cutoff)
            )
            purged = result.rowcount or 0
        if purged:
            log.info(
                "events retention: purged %d rows older than %s",
                purged, cutoff.date(),
            )
    except Exception:
        log.exception("events retention failed")


async def _job_selfcheck(send_admin_text) -> None:
    """Алёрт при смене статуса бота: heartbeat fresh ↔ stale."""
    from aemr_bot.health import heartbeat
    was_healthy = _SELFCHECK_HEALTHY["healthy"]
    is_healthy = heartbeat.is_fresh()
    if was_healthy and not is_healthy:
        await _send_admin_text_with_retry(
            send_admin_text,
            "⚠️ Проверка здоровья: бот перестал отвечать на внутренний heartbeat. "
            "Возможное состояние: завис главный цикл. Проверьте логи и "
            "перезапустите контейнер, если бот не восстановится автоматически.",
            context="health-selfcheck-stale",
        )
    elif not was_healthy and is_healthy:
        await _send_admin_text_with_retry(
            send_admin_text,
            "✅ Проверка здоровья: бот снова отвечает на внутренний heartbeat.",
            context="health-selfcheck-recovered",
        )
    _SELFCHECK_HEALTHY["healthy"] = is_healthy


async def _job_monthly_report(send_admin_document) -> None:
    """1-го числа в 09:00 — XLSX отчёт по месяцу в админ-чат."""
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


async def _job_pulse(send_admin_text) -> None:
    """Шлёт в служебную группу короткое подтверждение «бот жив».

    Расписание:
    • В рабочее время (пн–сб, 09:00–17:59 по Камчатке) — каждые
      полчаса, в минуты :00 и :30.
    • В остальное время понедельника–субботы и весь день воскресенья —
      раз в час, в минуту :05.

    Это второй контур мониторинга поверх selfcheck: selfcheck ловит
    зависший event-loop, а пульс показывает дежурному, что процесс
    жив и может отправлять сообщения в админ-группу.
    """
    now = datetime.now(TZ).strftime("%H:%M")
    sent = await _send_admin_text_with_retry(
        send_admin_text,
        f"🟢 Пульс: бот работает. Время проверки: {now}.",
        context="pulse",
    )
    if sent:
        log.info("pulse: sent admin heartbeat at %s", now)


async def _job_startup_pulse(send_admin_text) -> None:
    """Catch-up pulse при старте/рестарте процесса.

    APScheduler не догоняет тики, пропущенные пока контейнер был
    остановлен (`docker compose up --build` гасит процесс на 30–90 сек).
    Если рестарт пришёлся на момент cron-триггера — регулярный pulse
    может потеряться. Поэтому отдельно отправляем сообщение о старте.

    Запускается через `scheduler.add_job(..., trigger=DateTrigger(...))`
    с задержкой 5 секунд после старта — даём scheduler'у инициализироваться
    и MAX-сессии установиться.
    """
    now = datetime.now(TZ).strftime("%H:%M")
    sent = await _send_admin_text_with_retry(
        send_admin_text,
        f"🔄 Рестарт: процесс бота запущен заново. Время запуска: {now}.",
        context="startup-pulse",
    )
    if sent:
        log.info("startup-pulse: sent recovery heartbeat at %s", now)


async def _job_appeals_5y_retention(send_admin_text) -> None:
    """Раз в сутки обнуляем текстовое содержимое обращений старше
    5 лет (152-ФЗ ст. 5 ч. 7 + Приказ Минкультуры о номенклатуре дел).

    Записи appeals и messages не удаляются — остаются для подсчёта
    статистики «было обращение N в N-году», но summary/text/attachments
    чистятся. Жителя к этому моменту уже обезличил pdn-retention, так
    что в БД остаются только метаданные (даты, статусы, числа).
    """
    try:
        from aemr_bot.services import appeals as appeals_service

        async with session_scope() as session:
            purged_a, purged_m = await appeals_service.purge_old_appeals_content(
                session, years=5
            )
        if purged_a or purged_m:
            log.info(
                "appeals_5y_retention: обнулено обращений=%d, сообщений=%d",
                purged_a, purged_m,
            )
            await send_admin_text(
                f"📜 Архивная очистка по 5-летнему сроку: обнулён "
                f"текст у {purged_a} обращений и {purged_m} сообщений. "
                f"Метаданные (даты, статусы) сохранены."
            )
    except Exception:
        log.exception("appeals_5y_retention crashed")


async def _job_pdn_retention_check(send_admin_text) -> None:
    """152-ФЗ ст. 21 ч. 5: после отзыва согласия оператор обязан
    прекратить обработку и уничтожить ПДн в срок 30 дней.

    Раз в сутки ищем жителей, у которых consent_revoked_at старше 30
    дней, и обезличиваем их персоналку (erase_pdn). Открытые обращения
    по 59-ФЗ должны быть закрыты до обезличивания — пропускаем таких
    жителей до следующего дня.

    Без этого крона ПДн отозвавших согласие висели бы в БД бессрочно,
    что — формально — нарушение закона.
    """
    try:
        from aemr_bot.services import operators as ops_service
        from aemr_bot.services import users as users_service

        async with session_scope() as session:
            candidates = await users_service.find_pending_pdn_retention(
                session, days_after_revoke=30
            )
        if not candidates:
            return
        log.info("pdn_retention: %d жителей под обезличивание", len(candidates))
        erased = 0
        erased_ids: list[int] = []
        skipped_open = 0
        for max_user_id in candidates:
            try:
                async with session_scope() as session:
                    user = await users_service.get_or_create(
                        session, max_user_id=max_user_id
                    )
                    if await users_service.has_open_appeals(session, user.id):
                        skipped_open += 1
                        continue
                    ok = await users_service.erase_pdn(session, max_user_id)
                    if ok:
                        await ops_service.write_audit(
                            session,
                            operator_max_user_id=None,
                            action="auto_erase_pdn_retention",
                            target=f"user max_id={max_user_id}",
                            details={"reason": "152-FZ ст.21 ч.5, 30 дней после отзыва"},
                        )
                        erased += 1
                        erased_ids.append(max_user_id)
            except Exception:
                log.exception(
                    "pdn_retention: не удалось обезличить max_user_id=%s",
                    max_user_id,
                )
        for erased_id in erased_ids:
            await _send_admin_text_with_retry(
                send_admin_text,
                (
                    "🛡 Данные по отозванному согласию фактически обезличены.\n"
                    f"MAX user id: {erased_id}\n"
                    "Основание: прошло 30 дней после отзыва согласия, "
                    "открытых обращений нет."
                ),
                context="pdn_retention",
            )
        if erased or skipped_open:
            await send_admin_text(
                f"🛡 Архивная очистка ПДн по сроку: "
                f"обезличено {erased}, отложено {skipped_open} "
                f"(есть открытые обращения)."
            )
    except Exception:
        log.exception("pdn_retention_check crashed")


async def _job_funnel_watchdog(bot) -> None:
    """Раз в час смотрим, кто завис в воронке (AWAITING_*) дольше 24
    часов. Сбрасываем состояние в IDLE и шлём короткое напоминание с
    кнопкой «открыть меню». Без этого житель, начавший воронку и
    забывший про неё на неделю, при следующем «привет» попадал в
    обработчик зависшего шага: «привет» записывалось как имя или адрес.

    Лимит cfg.recover_batch_size защищает от лавины при первом запуске
    после простоя.
    """
    try:
        from aemr_bot import keyboards as kbds
        from aemr_bot.services import users as users_service

        cutoff_seconds = 24 * 3600  # сутки
        async with session_scope() as session:
            stuck = await users_service.find_stuck_in_funnel(
                session, idle_seconds=cutoff_seconds
            )
        if not stuck:
            return
        log.info(
            "funnel_watchdog: %d жителей зависли в воронке, сбрасываем",
            len(stuck),
        )
        for max_user_id, _state in stuck:
            try:
                async with session_scope() as session:
                    await users_service.reset_state(session, max_user_id)
                await bot.send_message(
                    user_id=max_user_id,
                    text=(
                        "Похоже, вы начали оформлять обращение, но не "
                        "закончили. Я сбросил черновик — если хотите "
                        "снова, откройте меню кнопкой ниже."
                    ),
                    attachments=[kbds.back_to_menu_keyboard()],
                )
            except Exception:
                log.exception(
                    "funnel_watchdog: не удалось сбросить max_user_id=%s",
                    max_user_id,
                )
    except Exception:
        log.exception("funnel_watchdog crashed")


async def _job_working_hours_open_reminder(bot) -> None:
    """Раз в час, ТОЛЬКО в рабочее время (пн–сб 09:00–17:59 Камчатка)
    и НЕ в государственные праздники РФ.

    Напоминание о всех неответленных обращениях — без разделения на
    «в SLA» и «просрочено». Если открытых нет — тишина (по нулям не
    пишем, чтобы оператор не привык игнорировать).

    Под сообщением кнопка «📋 Открытые обращения» — тап открывает
    полный список с кнопками действий по каждому.
    """
    try:
        if not is_workday(datetime.now(TZ).date()):
            return
        from aemr_bot.services import appeals as appeals_service

        async with session_scope() as session:
            appeals = await appeals_service.list_unanswered(session)
        if not appeals:
            return
        threshold = datetime.now(timezone.utc) - timedelta(
            hours=settings.sla_response_hours
        )
        in_sla = [a for a in appeals if a.created_at > threshold]
        overdue = [a for a in appeals if a.created_at <= threshold]
        header = (
            f"📋 Открытых обращений: {len(appeals)} "
            f"(в SLA — {len(in_sla)}, просрочено — {len(overdue)})"
        )
        lines = [header, ""]
        if overdue:
            lines.append(f"⚠️ Просрочено по SLA ({settings.sla_response_hours}ч):")
            lines.extend(_format_appeal_lines(overdue))
            lines.append("")
        if in_sla:
            lines.append("🆕 В SLA:")
            lines.extend(_format_appeal_lines(in_sla))
        await _send_with_open_tickets_button(bot, "\n".join(lines).rstrip())
    except Exception:
        log.exception("working_hours_open_reminder crashed")


async def _job_working_hours_overdue_reminder(bot) -> None:
    """Раз в час на :40 — отдельное напоминание ТОЛЬКО о просроченных.
    Вместе с :10 даёт «каждые полчаса для просрочки». Если нет
    просроченных — тишина. В госпраздники РФ молчит.
    """
    try:
        if not is_workday(datetime.now(TZ).date()):
            return
        from aemr_bot.services import appeals as appeals_service

        async with session_scope() as session:
            overdue = await appeals_service.find_overdue_unanswered(
                session, settings.sla_response_hours
            )
        if not overdue:
            return
        lines = [
            f"⚠️ Просрочено по SLA ({settings.sla_response_hours}ч): "
            f"{len(overdue)} обращений."
        ]
        lines.extend(_format_appeal_lines(overdue))
        await _send_with_open_tickets_button(bot, "\n".join(lines))
    except Exception:
        log.exception("working_hours_overdue_reminder crashed")


# ============================================================================
# build_scheduler — теперь только конфигурация, jobs снаружи
# ============================================================================


def build_scheduler(bot, send_admin_document, send_admin_text) -> AsyncIOScheduler:
    """Собрать APScheduler со всеми job'ами бота.

    Все jobs вынесены на module-level (см. _job_* выше); здесь —
    декларативная таблица `(func, trigger, name)` и единый цикл
    регистрации. Раньше было 14 копипастных `add_job(...)` с
    идентичными `max_instances=1, coalesce=True, misfire_grace_time`
    — теперь общие параметры заданы один раз в цикле. bot принимаем
    явным параметром, чтобы services не импортировал точку входа
    `main.bot` лазево.
    """
    scheduler = AsyncIOScheduler(timezone=TZ)
    pulse = functools.partial(_job_pulse, send_admin_text)

    # (func, trigger, name). Общие max_instances=1 / coalesce=True /
    # misfire_grace_time навешиваются циклом регистрации ниже —
    # единообразно, без копипасты.
    jobs: list[tuple] = [
        # Еженедельный бэкап
        (
            functools.partial(_job_backup_with_alert, send_admin_text),
            CronTrigger(
                day_of_week=settings.backup_day_of_week,
                hour=settings.backup_hour,
                minute=settings.backup_minute,
                timezone=TZ,
            ),
            "db-backup",
        ),
        # Ежедневная очистка events (idempotency-ключи)
        (
            _job_events_retention,
            CronTrigger(hour=4, minute=0, timezone=TZ),
            "events-retention",
        ),
        # Selfcheck heartbeat
        (
            functools.partial(_job_selfcheck, send_admin_text),
            CronTrigger(
                minute=f"*/{settings.healthcheck_interval_minutes}", timezone=TZ
            ),
            "health-selfcheck",
        ),
        # Месячный отчёт
        (
            functools.partial(_job_monthly_report, send_admin_document),
            CronTrigger(day=1, hour=9, minute=0, timezone=TZ),
            "monthly-stats",
        ),
        # Startup pulse — однократно через 5 секунд после старта.
        # Закрывает gap «pulse 21:05 не пришёл» при docker compose up
        # --build: APScheduler не догоняет cron-триггеры, пропущенные
        # пока процесс был остановлен. См. docstring _job_startup_pulse.
        (
            functools.partial(_job_startup_pulse, send_admin_text),
            DateTrigger(
                run_date=datetime.now(TZ) + timedelta(seconds=5), timezone=TZ
            ),
            "startup-pulse",
        ),
        # Pulse — три расписания. См. docstring _job_pulse.
        # Пн–сб вечером 18:00–21:59 раньше выпадали из расписания:
        # workhours кончался в 17:59, offhours начинался в 22:00 —
        # пульсы могли пропадать на четыре часа без реального сбоя.
        (
            pulse,
            CronTrigger(
                day_of_week="mon-sat", hour="0-8,18-23", minute=5, timezone=TZ
            ),
            "pulse-offhours",
        ),
        (
            pulse,
            CronTrigger(day_of_week="sun", hour="*", minute=5, timezone=TZ),
            "pulse-sunday",
        ),
        (
            pulse,
            CronTrigger(
                day_of_week="mon-sat", hour="9-17", minute="0,30", timezone=TZ
            ),
            "pulse-workhours",
        ),
        # 5-летняя архивация обращений
        (
            functools.partial(_job_appeals_5y_retention, send_admin_text),
            CronTrigger(hour=4, minute=45, timezone=TZ),
            "appeals-5y-retention",
        ),
        # PDn-retention (152-ФЗ 30 дней после revoke)
        (
            functools.partial(_job_pdn_retention_check, send_admin_text),
            CronTrigger(hour=4, minute=30, timezone=TZ),
            "pdn-retention",
        ),
        # Funnel watchdog: сброс зависших воронок
        (
            functools.partial(_job_funnel_watchdog, bot),
            CronTrigger(minute=15, timezone=TZ),
            "funnel-watchdog",
        ),
        # Напоминалки в рабочее время Камчатки
        (
            functools.partial(_job_working_hours_open_reminder, bot),
            CronTrigger(
                day_of_week="mon-sat", hour="9-17", minute=10, timezone=TZ
            ),
            "open-reminder-workhours",
        ),
        (
            functools.partial(_job_working_hours_overdue_reminder, bot),
            CronTrigger(
                day_of_week="mon-sat", hour="9-17", minute=40, timezone=TZ
            ),
            "overdue-reminder-workhours",
        ),
    ]

    # Внешний healthcheck-ping — только если URL задан в конфиге.
    if settings.healthcheck_url:
        jobs.append((
            _ping_healthcheck,
            CronTrigger(
                minute=f"*/{settings.healthcheck_interval_minutes}", timezone=TZ
            ),
            "healthcheck-ping",
        ))

    for func, trigger, name in jobs:
        scheduler.add_job(
            func,
            trigger,
            name=name,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=_MISFIRE_GRACE_SEC,
        )

    return scheduler


# ============================================================================
# Helpers
# Backup-логика выделена в services/db_backup.py (этап 4 рефакторинга).
# ============================================================================


async def _ping_healthcheck() -> None:
    """Внешний healthcheck (Healthchecks.io / Uptime Kuma и т.п.)."""
    if not settings.healthcheck_url:
        return
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            await s.get(settings.healthcheck_url)
    except Exception:
        log.warning("healthcheck ping failed", exc_info=True)
