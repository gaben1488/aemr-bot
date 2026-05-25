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
from aemr_bot.db.models import AuditLog, Event
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

    Алёрт **категоризирован** по `result.fail_kind` — каждый сценарий
    подсказывает оператору, КУДА смотреть и ЧТО проверять. Раньше было
    одно общее сообщение «бэкап не выполнен», по нему было неясно,
    проблема в pg_dump (БД), в gpg (шифрование) или в .env (конфиг).
    """
    try:
        result = await _backup_db()
        if result.ok:
            return
        if result.fail_kind == "pg_dump":
            await send_admin_text(
                "⚠️ Еженедельный бэкап БД упал на pg_dump.\n"
                f"Детали: {result.fail_detail}\n"
                "Возможные причины: Postgres недоступен, нет места на "
                "диске, сменились права. Снимите бэкап вручную через "
                "/backup, как только разберётесь — мы пропустили "
                "еженедельную точку восстановления."
            )
        elif result.fail_kind == "gpg":
            await send_admin_text(
                "🔐 Еженедельный бэкап БД: pg_dump прошёл, но gpg-"
                "шифрование упало.\n"
                f"Детали: {result.fail_detail}\n"
                "Файл .sql.gpg НЕ создан, незашифрованный дамп удалён "
                "(он содержал ПДн — оставлять на диске нельзя). "
                "Проверьте BACKUP_GPG_PASSPHRASE и логи. Снимите бэкап "
                "вручную через /backup, либо временно работайте без "
                "gpg (пустой BACKUP_GPG_PASSPHRASE → plain SQL — "
                "только при защищённом /backups том)."
            )
        elif result.fail_kind == "config":
            await send_admin_text(
                "⚙️ Еженедельный бэкап БД не выполнен: "
                "BACKUP_LOCAL_DIR пуст в .env — некуда писать. "
                "Проверьте конфигурацию (`docs/SYSADMIN.md §5.4`)."
            )
        else:  # "unknown" — fallback
            await send_admin_text(
                "⚠️ Еженедельный бэкап БД упал с неклассифицированной "
                "ошибкой.\n"
                f"Детали: {result.fail_detail}\n"
                "См. логи бота: `docker compose logs --tail 200 bot`. "
                "Снимите бэкап вручную через /backup."
            )
    except Exception:
        log.exception("backup_with_alert wrapper failed")
        await send_admin_text(
            "⚠️ Еженедельный бэкап БД упал с исключением вне самого "
            "backup_db (вероятно, сбой admin-канала или OOM). "
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


async def _job_audit_log_retention() -> None:
    """Удалить записи audit_log старше `settings.audit_log_retention_days`.

    AuditLog хранит операторские действия (block/unblock/reopen/close/
    erase/setting_update/setting_list_add и пр.) с `target` и `details`.
    Внутри окна — глубина расследования инцидента (по умолчанию 365
    дней). Дальше следы стираются вместе с любым PII в details
    (например, `details={"value": "..."}` для setting_update).

    Запускается раз в сутки в 04:15 — после events-retention (04:00),
    чтобы не пересекаться по long-running purge, до appeals-5y-retention
    (04:45).
    """
    try:
        cutoff = datetime.now(TZ) - timedelta(
            days=settings.audit_log_retention_days
        )
        async with session_scope() as session:
            result = await session.execute(
                delete(AuditLog).where(AuditLog.created_at < cutoff)
            )
            purged = result.rowcount or 0
        if purged:
            log.info(
                "audit_log retention: purged %d rows older than %s "
                "(retention=%d days)",
                purged, cutoff.date(), settings.audit_log_retention_days,
            )
    except Exception:
        log.exception("audit_log retention failed")


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


# Порог для admin-уведомления о массовых зависаниях. 1-4 застрявших за
# час — нормальная статистика «житель открыл, отвлёкся». 5+ — может
# сигналить о UX-баге в воронке или о DDoS-попытке. Уведомление шлётся
# для аудита, без него массовая аномалия остаётся только в логах бота.
_FUNNEL_WATCHDOG_ADMIN_ALERT_THRESHOLD = 5


async def _job_funnel_watchdog(bot, send_admin_text) -> None:
    """Раз в час смотрим, кто завис в воронке (AWAITING_*) дольше 24
    часов. Сбрасываем состояние в IDLE и шлём короткое напоминание с
    кнопкой «открыть меню». Без этого житель, начавший воронку и
    забывший про неё на неделю, при следующем «привет» попадал в
    обработчик зависшего шага: «привет» записывалось как имя или адрес.

    Если зависших ≥ `_FUNNEL_WATCHDOG_ADMIN_ALERT_THRESHOLD`, дополнительно
    шлём служебной группе summary — это сигнал, что в воронке может быть
    проблема (стабильно ≥5/час обычно означает UX-регрессию или поломку).

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
        reset_ok = 0
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
                reset_ok += 1
            except Exception:
                log.exception(
                    "funnel_watchdog: не удалось сбросить max_user_id=%s",
                    max_user_id,
                )
        # Аномальный массовый зашпил — сообщаем в служебную группу.
        # Под порогом — тишина, чтобы не флудить (1-4 застрявших в час
        # — норма «житель отвлёкся, не дошёл до конца»).
        if len(stuck) >= _FUNNEL_WATCHDOG_ADMIN_ALERT_THRESHOLD:
            await _send_admin_text_with_retry(
                send_admin_text,
                (
                    f"🧹 Funnel watchdog: за час сброшено "
                    f"{reset_ok}/{len(stuck)} зависших анкет (>24ч). "
                    f"Обычная норма 1-4 — массовое число (≥"
                    f"{_FUNNEL_WATCHDOG_ADMIN_ALERT_THRESHOLD}) может "
                    f"указывать на проблему в воронке. Проверьте логи "
                    f"бота, шаги анкеты и оповестите ИТ при повторении."
                ),
                context="funnel-watchdog-anomaly",
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
        # Ежедневная очистка audit_log (retention по конфигу, default 365)
        (
            _job_audit_log_retention,
            CronTrigger(hour=4, minute=15, timezone=TZ),
            "audit-log-retention",
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
        # Funnel watchdog: сброс зависших воронок + admin-alert при
        # массовом зашпиле (≥5/час) — нужен send_admin_text в партиале.
        (
            functools.partial(_job_funnel_watchdog, bot, send_admin_text),
            CronTrigger(minute=15, timezone=TZ),
            "funnel-watchdog",
        ),
        # Напоминалки операторам по неотвеченным/просроченным обращениям.
        # Расписание соответствует фактическому рабочему времени АЕМО:
        # пн-пт 09:00-18:00 с обеденным перерывом 12:00-13:00 (Регламент
        # v5 §39 + уточнение v6: пн-пт + обед). В Сб бот не дёргает
        # оператора — он не на смене. В обед — не дёргает тоже: hour=12
        # выпадает из расписания (`hour="9-11,13-17"`).
        #
        # Pulse-задачи (см. выше) намеренно остаются пн-сб + 24/7 — это
        # технический heartbeat «бот живой», а не уведомление оператора.
        # Heartbeat в обед и в субботу полезен (мониторинг непрерывный).
        (
            functools.partial(_job_working_hours_open_reminder, bot),
            CronTrigger(
                day_of_week="mon-fri", hour="9-11,13-17", minute=10, timezone=TZ
            ),
            "open-reminder-workhours",
        ),
        (
            functools.partial(_job_working_hours_overdue_reminder, bot),
            CronTrigger(
                day_of_week="mon-fri", hour="9-11,13-17", minute=40, timezone=TZ
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


# Reliability-pass: lazy-singleton aiohttp.ClientSession для healthcheck.
# Раньше `async with aiohttp.ClientSession(...)` создавался на каждый тик
# (раз в `healthcheck_interval_minutes`). На каждый коннект — TLS handshake,
# DNS resolve, fresh connector pool, потом всё закрывается. Для cron-job
# с интервалом 1-5 мин и URL'ом на тот же эндпоинт это сотни-тысячи
# никому не нужных сокетов в день. Singleton переиспользует keep-alive
# соединение пока процесс жив — один handshake на старте, дальше HTTP/1.1
# pipelining либо новый коннект только если сервер закрыл предыдущий.
#
# Закрытия не делаем — session живёт до конца процесса. APScheduler при
# shutdown отменяет джобы, висящий `await s.get(...)` свернётся через
# timeout=10. Утечки не будет (один session на процесс).
_HEALTHCHECK_SESSION: aiohttp.ClientSession | None = None


def _get_healthcheck_session() -> aiohttp.ClientSession:
    """Lazy-init shared aiohttp.ClientSession для healthcheck-ping.

    Создавать на module-level нельзя: ClientSession в __init__ читает
    `asyncio.get_event_loop()`, а на import-time loop'а ещё нет.
    """
    global _HEALTHCHECK_SESSION
    if _HEALTHCHECK_SESSION is None or _HEALTHCHECK_SESSION.closed:
        _HEALTHCHECK_SESSION = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10),
        )
    return _HEALTHCHECK_SESSION


async def _ping_healthcheck() -> None:
    """Внешний healthcheck (Healthchecks.io / Uptime Kuma и т.п.)."""
    if not settings.healthcheck_url:
        return
    try:
        session = _get_healthcheck_session()
        await session.get(settings.healthcheck_url)
    except Exception:
        log.warning("healthcheck ping failed", exc_info=True)
