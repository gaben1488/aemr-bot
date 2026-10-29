from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp
from apscheduler.events import EVENT_JOB_ERROR, EVENT_SCHEDULER_SHUTDOWN, JobExecutionEvent
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from sqlalchemy import delete

from aemr_bot import keyboards as kbds
from aemr_bot.config import settings
from aemr_bot.db.models import AuditLog, Event
from aemr_bot.db.session import session_scope
from aemr_bot.health import heartbeat
from aemr_bot.network import session_kwargs
from aemr_bot.services import appeals as appeals_service
from aemr_bot.services import broadcasts as broadcasts_service
from aemr_bot.services import operators as operators_service
from aemr_bot.services import sla as sla_service
from aemr_bot.services import threat_intel
from aemr_bot.services import users as users_service
from aemr_bot.services.backup_verify import (
    verify_latest_backup as _verify_latest_backup,
)
from aemr_bot.services.db_backup import backup_db as _backup_db

log = logging.getLogger(__name__)
TZ = ZoneInfo(settings.timezone)

# Действия, чья audit-запись является ПОДТВЕРЖДЕНИЕМ уничтожения ПДн.
# Для них действует отдельный, более длинный срок хранения
# (audit_erasure_retention_days) — см. _job_audit_log_retention.
#   auto_erase_pdn_retention — уничтожение кроном через 30 дней после
#     отзыва согласия (152-ФЗ ст. 21 ч. 5);
#   erase                    — уничтожение по команде оператора;
#   self_erase               — житель сам нажал /forget;
#   self_consent_revoke      — житель отозвал согласие (событие, с
#     которого стартует 30-дневный срок; без него нечем доказать, что
#     срок соблюдён).
# За сколько дней до истечения 30-дневного срока уничтожения ПДн
# предупреждать оператора о неотвеченных обращениях. Неделя — чтобы
# успеть ответить даже при выходных и отпуске одного из специалистов.
PDN_DEADLINE_WARN_DAYS = 7

ERASURE_ACTIONS: tuple[str, ...] = (
    "auto_erase_pdn_retention",
    "erase",
    "self_erase",
    "self_consent_revoke",
)


# Module-level state для selfcheck — раньше было local dict в closure.
# Хранит последний известный статус «бот отвечает», чтобы шлать алёрт
# при смене состояния (healthy → unhealthy или обратно), а не каждый
# тик cron'а.
_SELFCHECK_HEALTHY = {"healthy": True}

# Окно прощения опоздавшего срабатывания (misfire grace) для всех cron-задач.
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


# ---- Module-level helpers (вынесены из build_scheduler для тестируемости) ----


def _format_appeal_lines(appeals: list, *, max_rows: int = 10,
                         anchors: dict[int, datetime] | None = None) -> list[str]:
    """Show elapsed working time, with the relevant response-period anchor."""
    now = datetime.now(TZ)
    lines: list[str] = []
    for ap in appeals[:max_rows]:
        anchor = ((anchors or {}).get(ap.id) or getattr(ap, "sla_anchor_at", None)
                  or ap.created_at or now)
        elapsed = sla_service.business_seconds_between(anchor, now) / 3600
        name = (ap.user.first_name or "—") if ap.user else "—"
        description = "после первого ответа" if anchors and ap.id in anchors else "с начала периода"
        lines.append(f"• #{ap.id} · {name} · {ap.locality or '—'} · "
                     f"{description}: {elapsed:.1f} рабочих ч")
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
        # Perf «2 RPS»: это прямой bot.send_message на хукнутом инстансе —
        # значит проходит глобальный лимитер исходящих
        # (admin_bus.install_outgoing_tracker_hook) автоматически. НЕ слать
        # в обход bot.* — иначе напоминалка обойдёт общий бюджет темпа.
        await bot.send_message(
            chat_id=settings.admin_group_id,
            text=text,
            attachments=[kb.as_markup()],
        )
    except Exception:
        log.exception("send admin reminder with button failed")


async def _claim_reminder_snapshot(name: str, groups: list[list[int]], total: int) -> bool:
    """Notify on changed queue or once on a new working day, never every tick."""
    from aemr_bot.services import alert_state

    if not settings.admin_group_id:
        return False
    signature = hashlib.sha256(json.dumps(
        [datetime.now(TZ).date().isoformat(), total, groups], separators=(",", ":"),
    ).encode()).hexdigest()
    async with session_scope() as session:
        return await alert_state.claim_snapshot(session, name, signature)


async def _send_admin_text_with_retry(
    send_admin_text,
    text: str,
    *,
    context: str,
    critical: bool = False,
) -> bool:
    """Однократная отправка с подтверждением по идентификатору MAX.

    `critical=True` пробрасывается в `send_admin_text` и заставляет
    `admin_bus.send` игнорировать quiet режим. Используется для алёртов
    (фейл бэкапа, ошибки retention, stale-operators, funnel-watchdog),
    которые должны пробить ночь — иначе 152-ФЗ retention или
    потерянный бэкап замолчат до утра понедельника.
    См. SECURITY_REVIEW_2026-05-28 §A1.
    """
    # A timeout can mean MAX accepted the message but the response was lost.
    # Repeating a changing request blindly would create duplicates.
    try:
        confirmed = bool(await send_admin_text(text, critical=critical))
    except Exception:
        log.exception("%s: результат отправки не подтверждён", context)
        return False
    if not confirmed:
        log.warning("%s: результат отправки не подтверждён", context)
    return confirmed


# ============================================================================
# Cron-задачи модульного уровня
# ============================================================================
# Все задачи принимают зависимости явными параметрами (раньше брали
# через замыкание в build_scheduler). build_scheduler регистрирует
# их через functools.partial.
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
    # critical=True во всех ветках: фейл бэкапа должен пробить
    # quiet режим (sec/A1, SECURITY_REVIEW_2026-05-28). Backup-cron
    # 0 3 * * 6 (сб 03:00) попадает в default quiet окно [18, 9) —
    # без critical алёрт молчит до утра понедельника, окно реакции
    # ~30 часов без свежей точки восстановления.
    if not settings.backup_local_dir:
        log.info("backup skipped: BACKUP_LOCAL_DIR is empty (disabled)")
        return
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
                "еженедельную точку восстановления.",
                critical=True,
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
                "только при защищённом /backups том).",
                critical=True,
            )
        elif result.fail_kind == "config":
            await send_admin_text(
                "⚙️ Еженедельный бэкап БД не выполнен: "
                "в настройках (.env) пуст параметр BACKUP_LOCAL_DIR, "
                "некуда писать. Заполните его и повторите.",
                critical=True,
            )
        else:  # "unknown" — fallback
            await send_admin_text(
                "⚠️ Еженедельный бэкап БД упал с неклассифицированной "
                "ошибкой.\n"
                f"Детали: {result.fail_detail}\n"
                "См. логи бота: `docker compose logs --tail 200 bot`. "
                "Снимите бэкап вручную через /backup.",
                critical=True,
            )
    except Exception:
        log.exception("backup_with_alert wrapper failed")
        await send_admin_text(
            "⚠️ Еженедельный бэкап БД упал с исключением вне самого "
            "backup_db (вероятно, сбой admin-канала или OOM). "
            "Срочно проверьте логи и снимите бэкап вручную через /backup.",
            critical=True,
        )


async def _job_backup_verify_with_alert(send_admin_text) -> None:
    """Ежемесячная проверка пригодности бэкапа с алёртом при провале.

    Еженедельный бэкап доказывает, что файл записался. Эта проверка
    доказывает, что файл можно ОТКРЫТЬ: расшифровывает свежайший дамп
    потоком (на диск ничего не пишет, БД не трогает) и сверяет сигнатуру
    pg_dump. Ловит две молчаливые поломки — потерю парольной фразы (без
    неё все копии бесполезны) и обрезанный файл (у GPG есть контроль
    целостности). Развернуть копию до конца — ручная квартальная
    процедура, см. вику.

    Успех — тихо в лог: ещё одно «всё хорошо» раз в месяц только приучает
    не читать алёрты. Провал — critical, как и у самого бэкапа: узнать о
    непригодных копиях надо до аварии, а не после.
    """
    try:
        result = await _verify_latest_backup()
        if result.ok:
            log.info(
                "backup verify OK: %s (%d КБ после распаковки)",
                result.backup_name, result.decrypted_bytes // 1024,
            )
            return
        if result.fail_kind == "config":
            # Проверка выключена или BACKUP_LOCAL_DIR пуст — осознанный
            # выбор конфигурации, а не поломка. Про пустой BACKUP_LOCAL_DIR
            # и так кричит сам бэкап; второй раз дёргать людей незачем.
            log.info("backup verify пропущена: %s", result.fail_detail)
            return
        if result.fail_kind == "decrypt":
            await send_admin_text(
                "🔑 Проверка бэкапа: НЕ УДАЛОСЬ ОТКРЫТЬ дамп "
                f"({result.backup_name}).\n"
                f"Детали: {result.fail_detail}\n"
                "Либо парольная фраза BACKUP_GPG_PASSPHRASE в .env не "
                "подходит к сохранённым копиям, либо файл повреждён — "
                "восстановиться из него СЕЙЧАС НЕЛЬЗЯ. Проверьте, не "
                "менялась ли фраза (в том числе при переносе сервера), и "
                "держите её резервную запись отдельно от сервера. Снимите "
                "свежий бэкап через /backup.",
                critical=True,
            )
        elif result.fail_kind == "signature":
            await send_admin_text(
                "🔍 Проверка бэкапа: файл открылся, но внутри НЕ ДАМП "
                f"({result.backup_name}).\n"
                f"Детали: {result.fail_detail}\n"
                "Восстановление из такой копии не вернёт обращения. Снимите "
                "свежий бэкап через /backup и проверьте логи pg_dump.",
                critical=True,
            )
        elif result.fail_kind == "no_backup":
            await send_admin_text(
                "📭 Проверка бэкапа: в каталоге копий НЕТ НИ ОДНОГО файла.\n"
                f"Детали: {result.fail_detail}\n"
                "Точки восстановления не существует. Снимите бэкап вручную "
                "через /backup и проверьте, что еженедельный cron живой "
                "(`/pulse` → раздел задач).",
                critical=True,
            )
        else:  # "unknown"
            await send_admin_text(
                "⚠️ Проверка бэкапа упала с неклассифицированной ошибкой.\n"
                f"Детали: {result.fail_detail}\n"
                "См. логи бота: `docker compose logs --tail 200 bot`.",
                critical=True,
            )
    except Exception:
        log.exception("backup_verify_with_alert wrapper failed")
        await send_admin_text(
            "⚠️ Проверка бэкапа упала с исключением вне самой проверки. "
            "Проверьте логи бота.",
            critical=True,
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
    """Удалить записи audit_log по сроку — с ДВУМЯ разными окнами.

    AuditLog хранит операторские действия (block/unblock/reopen/close/
    erase/setting_update/setting_list_add и пр.) с `target` и `details`.
    Обычные записи живут `settings.audit_log_retention_days` (по
    умолчанию 365 дней) — это глубина расследования инцидента; дальше
    следы стираются вместе с любым PII в details (например,
    `details={"value": "..."}` для setting_update).

    ОТДЕЛЬНОЕ ОКНО для записей об УНИЧТОЖЕНИИ ПДн (`ERASURE_ACTIONS`).
    Такая запись — не просто след действия, а ПОДТВЕРЖДЕНИЕ того, что
    оператор исполнил обязанность уничтожить данные: именно ею
    доказывается, что после отзыва согласия ПДн действительно удалены в
    срок. Стирать доказательство раньше, чем истечёт срок его хранения,
    бессмысленно: в момент проверки предъявить будет нечего. Поэтому
    подтверждения живут `settings.audit_erasure_retention_days`
    (по умолчанию 3 года) — срок взят из требований к подтверждению
    уничтожения ПДн при автоматизированной обработке; решение владельца
    2026-07-17.

    Запускается раз в сутки в 04:15 — после events-retention (04:00),
    чтобы не пересекаться по long-running purge, до appeals-5y-retention
    (04:45).
    """
    try:
        now = datetime.now(TZ)
        cutoff = now - timedelta(days=settings.audit_log_retention_days)
        erasure_cutoff = now - timedelta(
            days=settings.audit_erasure_retention_days
        )
        async with session_scope() as session:
            # Обычные записи: старше общего окна И не подтверждение
            # уничтожения.
            result = await session.execute(
                delete(AuditLog).where(
                    AuditLog.created_at < cutoff,
                    AuditLog.action.notin_(ERASURE_ACTIONS),
                )
            )
            purged = result.rowcount or 0
            # Подтверждения уничтожения: своё, более длинное окно.
            erasure_result = await session.execute(
                delete(AuditLog).where(
                    AuditLog.created_at < erasure_cutoff,
                    AuditLog.action.in_(ERASURE_ACTIONS),
                )
            )
            purged_erasure = erasure_result.rowcount or 0
        if purged or purged_erasure:
            log.info(
                "audit_log retention: purged %d обычных (старше %s, "
                "retention=%d дн.) и %d подтверждений уничтожения "
                "(старше %s, retention=%d дн.)",
                purged, cutoff.date(), settings.audit_log_retention_days,
                purged_erasure, erasure_cutoff.date(),
                settings.audit_erasure_retention_days,
            )
    except Exception:
        log.exception("audit_log retention failed")


async def _job_threat_intel_refresh(send_admin_text) -> None:
    """Обновить локальный set threat-intel host'ов из feed'ов.

    Раз в час подтягиваем URLhaus + ThreatFox (+ PhishTank если задан
    PHISHTANK_APP_KEY). Локальный set растёт/уменьшается по факту;
    если все feed'ы отвалились — оставляем старый set, staleness
    счётчик растёт. См. services/threat_intel.py.

    Запуск на :17 — между нашими утренними job'ами и watchdog'ом,
    чтобы не пересекаться по нагрузке.

    Сигнал устаревания: если после обновления данные всё равно старше
    бюджета (6 часов без успешного refresh — источники стабильно
    недоступны), шлём critical-алёрт в служебный чат. Бот продолжает
    работать со старым set'ом, но оператор должен знать, что
    предупреждения о фишинге могут отставать. critical=True пробивает
    quiet hours — безопасность важнее тишины.
    """
    try:
        counts = await threat_intel.refresh_all()
        if counts:
            log.info("threat-intel-refresh ok: %s", counts)
        else:
            log.warning(
                "threat-intel-refresh: ни один feed не отдался"
            )
    except Exception:
        log.exception("threat-intel-refresh crashed")

    # Алёрт устаревания. Отдельный try: даже если refresh упал,
    # проверку и уведомление делаем — это и есть момент, когда
    # оператору важнее всего узнать о проблеме.
    try:
        store = threat_intel.get_store()
        from aemr_bot.services import alert_state

        if store.is_stale():
            # Commit the claim before sending: even a process crash during
            # the request must not result in an automatic duplicate.
            async with session_scope() as session:
                claimed = await alert_state.claim(session, "threat_intel")
            if not claimed:
                return
            sent = await _send_admin_text_with_retry(
                send_admin_text,
                (
                    "⚠️ База угроз (антифишинг) не обновлялась полностью более 6 ч. "
                    "Часть источников или вся база недоступны. Приём обращений продолжается; "
                    "проверка использует доступные локальные данные. Предупреждения о вредоносных "
                    "ссылках в обращениях могут отставать. Проверьте "
                    "доступ к источникам и ABUSECH_AUTH_KEY / PHISHTANK_APP_KEY. "
                    "Повторных уведомлений до восстановления не будет."
                ),
                context="threat-intel-stale",
                critical=True,
            )
            async with session_scope() as session:
                await alert_state.finish(session, "threat_intel", confirmed=sent)
            store.outage_notified = sent
            await asyncio.to_thread(threat_intel.save_cache, store)
        else:
            async with session_scope() as session:
                await alert_state.clear(session, "threat_intel")
            store.outage_notified = False
    except Exception:
        log.exception("threat-intel staleness alert failed")


async def _job_reap_orphaned_drafts() -> None:
    """Перевести зависшие DRAFT-рассылки в FAILED.

    SECURITY_REVIEW F5: между `_handle_cancel_cooldown` и
    `mark_cancelled` есть окно, где БД-апдейт может фейлить — task в
    asyncio отменён, но row остаётся в DRAFT навсегда. Раз в час
    подбираем такие row'ы (старше 30 минут — больше любого реального
    cooldown) и помечаем FAILED. Не CANCELLED — это другая семантика
    («оператор явно отменил»), здесь же скорее «процесс умер во время
    cooldown'а либо отмены».
    """
    try:

        async with session_scope() as session:
            reaped = await broadcasts_service.reap_orphaned_draft(
                session, ttl_minutes=30
            )
        if reaped:
            log.info(
                "broadcast-draft-reaper: %d orphan DRAFT rows → FAILED",
                reaped,
            )
    except Exception:
        log.exception("broadcast-draft-reaper failed")


async def _job_stale_operators_cleanup(bot, send_admin_text) -> None:
    """Деактивировать операторов, ушедших из админ-группы MAX.

    SECURITY_REVIEW M2 (max-threats CVE-9): запись в `operators` остаётся
    `is_active=true` даже когда оператор покинул служебную группу MAX
    (уволился, поменял отдел). Сам по себе доступ блокируется
    `is_admin_chat`-проверкой, но stale-данные — повод для путаницы.
    Здесь раз в сутки сверяем активных операторов с реальным списком
    членов группы и мягко деактивируем тех, кого там нет.

    Не трогает IT-операторов — иначе одна сетевая флуктуация может
    лишить организацию админа без возможности восстановления (некого
    реактивировать). См. cleanup_stale_operators().

    Запускается в 04:20 — между audit-log-retention (04:15) и
    pdn-retention (04:30), не пересекается по нагрузке.
    """
    try:
        # Используем тот же безопасный helper из admin_operators,
        # чтобы любая ошибка MAX API → пустой список → no-op (см.
        # защиту в cleanup_stale_operators при пустом current_member_ids).
        from aemr_bot.handlers.admin_operators import _safe_get_chat_members

        members = await _safe_get_chat_members(bot)
        if not members:
            log.info(
                "stale-operators-cleanup: get_chat_members вернул пусто "
                "— шаг пропущен (safety, чтобы не деактивировать всех "
                "при сетевой ошибке)"
            )
            return
        current_ids = {
            int(getattr(m, "user_id", 0)) for m in members
            if getattr(m, "user_id", None) is not None
        }
        # SECURITY_REVIEW F11 (CVSS 5.7): защита от paginated/частичного
        # ответа MAX API. Если бот получил **меньше** членов чата, чем
        # активных операторов в БД — это либо пагинация (MAX вернул
        # первую страницу), либо отстающая БД (только что добавили
        # оператора). В обоих случаях массовая авто-деактивация =
        # катастрофа (50 легитимных IT-сотрудников разом помечены
        # ушедшими). Лучше пропустить итерацию и подождать следующей.
        async with session_scope() as session:
            active_count = len(await operators_service.list_active(session))
            if active_count > 0 and len(current_ids) < active_count:
                log.warning(
                    "stale-operators-cleanup: получено %d членов чата, "
                    "но активных операторов %d — возможна пагинация MAX "
                    "API или отстающий список. Пропускаем итерацию для "
                    "безопасности (см. SEC_SELF_REVIEW F11).",
                    len(current_ids), active_count,
                )
                return
            deactivated = await operators_service.cleanup_stale_operators(
                session, current_member_ids=current_ids
            )
        if deactivated:
            names = ", ".join(
                f"{op.full_name} ({op.role})" for op in deactivated[:5]
            )
            more = "" if len(deactivated) <= 5 else f" и ещё {len(deactivated) - 5}"
            msg = (
                f"ℹ️ Авто-деактивация stale-операторов: "
                f"{len(deactivated)} человек больше не в служебной группе "
                f"MAX → переведены в is_active=false. ИТ-операторы не "
                f"трогаются (защита от self-lock-out).\n\n"
                f"Деактивированы: {names}{more}\n\n"
                f"Если кто-то ушёл по ошибке — восстановите через меню "
                f"«👥 Операторы» → «➕ Добавить»."
            )
            # critical=True (sec/A1): авто-деактивация stale операторов
            # — событие, которое нужно увидеть в день когда оно произошло,
            # чтобы быстро восстановить ошибочно выкинутых.
            await _send_admin_text_with_retry(
                send_admin_text, msg, context="stale-operators-cleanup",
                critical=True,
            )
        else:
            log.info("stale-operators-cleanup: no changes")
    except Exception:
        log.exception("stale-operators-cleanup failed")


async def _job_selfcheck(send_admin_text) -> None:
    """Алёрт при смене статуса бота: heartbeat fresh ↔ stale.

    critical=True (sec/A1): selfcheck должен пробить quiet режим. Если
    бот завис ночью, оператор должен увидеть это сразу — а не утром
    после простоя 9 часов.

    Раньше это был единственный job без try/except (находка ревью) —
    любое исключение внутри (например, heartbeat.is_fresh() упал на
    чтении файла) прибило бы весь тик scheduler'а вместо аккуратного
    лога. Оборачиваем по общему для всех job'ов паттерну; присваивание
    _SELFCHECK_HEALTHY делаем только после успешной проверки — если
    heartbeat.is_fresh() упал, предыдущее известное состояние не
    затирается мусором.
    """
    try:
        was_healthy = _SELFCHECK_HEALTHY["healthy"]
        is_healthy = heartbeat.is_fresh()
        if was_healthy and not is_healthy:
            await _send_admin_text_with_retry(
                send_admin_text,
                "⚠️ Проверка здоровья: бот перестал отвечать на внутренний heartbeat. "
                "Возможное состояние: завис главный цикл. Проверьте логи и "
                "перезапустите контейнер, если бот не восстановится автоматически.",
                context="health-selfcheck-stale",
                critical=True,
            )
        elif not was_healthy and is_healthy:
            await _send_admin_text_with_retry(
                send_admin_text,
                "✅ Проверка здоровья: бот снова отвечает на внутренний heartbeat.",
                context="health-selfcheck-recovered",
                critical=True,
            )
        _SELFCHECK_HEALTHY["healthy"] = is_healthy
    except Exception:
        log.exception("health-selfcheck crashed")


async def _job_monthly_report(send_admin_document) -> None:
    """1-го числа в 09:00 — XLSX отчёт по месяцу в админ-чат.

    Гейт `admin_notify_monthly_stats` (services/notify_toggles.py):
    выключен — отчёт не формируется и не отправляется.
    """
    try:
        from aemr_bot.services import notify_toggles

        if not notify_toggles.is_enabled("admin_notify_monthly_stats"):
            log.debug("monthly-stats: admin_notify_monthly_stats disabled, suppressed")
            return
        from datetime import UTC, timedelta
        from aemr_bot.services import complete_stats, privacy_epoch

        now = datetime.now(TZ)
        current_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        previous_month = (current_month - timedelta(days=1)).replace(day=1)
        title = f"за {previous_month:%m.%Y}"
        window = (previous_month.astimezone(UTC), current_month.astimezone(UTC), title)
        async with session_scope() as session:
            content, title, count, epoch = await complete_stats.build_xlsx_with_epoch(
                session, "month", window=window
            )
        filename = f"appeals_month_{previous_month:%Y-%m}.xlsx"
        async with session_scope() as guard_session:
            if not await privacy_epoch.hold_export_if_current(guard_session, epoch):
                log.warning("monthly report suppressed: data erased while preparing")
                return
            await send_admin_document(
                filename=filename,
                content=content,
                caption=f"📊 Статистика {title} ({count} обращений)",
            )
    except Exception:
        log.exception("monthly report failed")


async def _job_pulse(send_admin_text) -> None:
    """Шлёт включённый пульс в группу не чаще одного раза в сутки.

    Текущее расписание (после унификации 2026-05-22):
    • `pulse-hourly` — каждый час 24/7 в `:05` (базовый heartbeat).
    • `pulse-workhours-extra` — пн–пт 09:00–17:59 в `:35`.
    Оба задания проверяют общую суточную отметку, а не отправляют два
    сообщения каждый час.

    Это второй контур мониторинга поверх selfcheck: selfcheck ловит
    зависший event-loop, а пульс показывает дежурному, что процесс
    жив и может отправлять сообщения в админ-группу.

    Quiet hours: если в settings включён `admin_quiet_hours_enabled` и
    текущий час попадает в окно — пульс **не отправляется**.
    Дежурный явно попросил «не присылать pulse ночью» —
    `health-selfcheck` и `healthcheck-ping` продолжают работать как
    fallback, оператор всё равно узнает о реальном сбое.

    Модульный тумблер `admin_notify_pulse` (services/notify_toggles.py):
    если выключен — пульс не отправляется НЕЗАВИСИМО от времени суток
    (в отличие от quiet hours, которые подавляют только ночью).
    startup-pulse (`_job_startup_pulse`) этому тумблеру НЕ подчиняется —
    разовое диагностическое событие при рестарте, не рутинный heartbeat.
    """
    # quiet_hours + notify_toggles: refresh cache из БД (этот cron —
    # основное место регулярного обновления обоих кэшей, кроме старта
    # бота и UI-обработчиков тумблеров), затем sync-check.
    try:
        from aemr_bot.db.session import session_scope
        from aemr_bot.services import notify_toggles
        from aemr_bot.services.quiet_hours import (
            is_quiet_hours_now,
            refresh_cache_from_db,
        )

        async with session_scope() as session:
            await refresh_cache_from_db(session)
            await notify_toggles.refresh_cache_from_db(session)
        if is_quiet_hours_now():
            log.info("pulse: quiet hours active, suppressed")
            return
        if not notify_toggles.is_enabled("admin_notify_pulse"):
            log.debug("pulse: admin_notify_pulse disabled, suppressed")
            return
    except Exception:
        # Сбой самой проверки quiet_hours/notify_toggles (например,
        # временная недоступность БД при refresh_cache_from_db) —
        # не должен молчать: на debug это терялось бы в проде, а
        # это сигнал, что кэш quiet-hours/notify-toggles мог не
        # обновиться. Пульс всё равно шлём (fail-open — недоступность
        # хуже, чем лишнее уведомление).
        log.warning("pulse: quiet_hours check failed, отправляем", exc_info=True)
    # The two scheduler ticks provide catch-up opportunities, not two
    # chat messages per hour. Reserve the daily notification before the
    # request: an unknown MAX result must not trigger an automatic resend.
    try:
        if not await _claim_reminder_snapshot("daily-pulse", [], 0):
            return
    except Exception:
        log.warning("pulse: unable to reserve daily notification", exc_info=True)
        return
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
    """Раз в сутки обнуляем текстовое содержимое обращений старше 5 лет.

    Пятилетний срок — НЕ требование закона: его установил ОПЕРАТОР
    локальным актом (152-ФЗ ст. 18.1 ч. 1 п. 2), а ст. 5 ч. 7 задаёт
    только верхнюю границу «не дольше, чем требует цель обработки».
    Доводы за пять лет (смежность с обращениями по 59-ФЗ; отчётность и
    анализ эффективности принятых мер) развёрнуты в docstring
    `appeals_service.purge_old_appeals_content` — решение владельца
    от 2026-07-17.

    Записи appeals и messages не удаляются — остаются для подсчёта
    статистики «было обращение N в N-году», но summary/text/attachments
    чистятся. Жителя к этому моменту уже обезличил pdn-retention, так
    что в БД остаются только метаданные (даты, статусы, числа).
    """
    try:

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


async def _warn_about_approaching_pdn_deadline(send_admin_text) -> None:
    """Предупредить оператора о жителях, чей срок уничтожения ПДн близок.

    Запланированная очистка закроет открытые обращения без ответа. Наличие
    иных оснований обработки и порядок ответа оценивает ответственное лицо.

    Предупреждаем только по тем, у кого реально остались открытые
    обращения — иначе алёрт превратится в шум и его перестанут читать.
    Ошибки глушим: это вспомогательный шаг, он не должен ронять
    основную задачу уничтожения.
    """
    try:
        async with session_scope() as session:
            approaching = await users_service.find_revoked_deadline_approaching(
                session,
                days_after_revoke=30,
                warn_within_days=PDN_DEADLINE_WARN_DAYS,
            )
        if not approaching:
            return
        from sqlalchemy import func, select
        from aemr_bot.db.models import Appeal, AppealStatus, User

        async with session_scope() as session:
            condition = (
                User.max_user_id.in_(approaching),
                Appeal.status.in_([AppealStatus.NEW.value, AppealStatus.IN_PROGRESS.value]),
            )
            count = await session.scalar(
                select(func.count()).select_from(Appeal).join(User).where(*condition)
            ) or 0
            appeal_ids = list(await session.scalars(
                select(Appeal.id).join(User).where(*condition).order_by(Appeal.id).limit(20)
            ))
        if not count:
            return
        ids = ", ".join(f"№ {appeal_id}" for appeal_id in appeal_ids)
        more = f" и ещё {count - 20}" if count > 20 else ""
        await send_admin_text(
            f"⏳ Скоро истекает срок хранения данных по отозванным "
            f"согласиям — осталось не больше {PDN_DEADLINE_WARN_DAYS} дней.\n"
            f"Открытые обращения: {ids}{more}.\n"
            f"Проверьте порядок дальнейшей работы: при очистке рабочей базы "
            f"открытые обращения будут закрыты без ответа.",
            critical=True,
        )
    except Exception:
        log.exception("pdn_deadline_warning failed")


async def _job_pdn_retention_check(send_admin_text, *, warn: bool = True) -> None:
    """Очищать отозванные данные в рабочей базе с запасом до 30 дней.

    По установленной конфигурации поиск начинается через 29 дней и
    повторяется каждый час. Открытые обращения закрываются без ответа;
    оператор получает предупреждение. Недоступность сервиса может задержать
    выполнение, поэтому срок требует организационного контроля. Наличие
    самостоятельного правового основания обработки оценивается отдельно.
    """
    # Pre-deadline warnings must not depend on already overdue rows or cleanup.
    if warn:
        await _warn_about_approaching_pdn_deadline(send_admin_text)
    try:

        async with session_scope() as session:
            candidates = await users_service.find_pending_pdn_retention(
                session, days_after_revoke=29
            )
        if not candidates:
            return
        log.info("pdn_retention: %d жителей под обезличивание", len(candidates))
        erased = 0
        skipped_open = 0
        for max_user_id in candidates:
            try:
                async with session_scope() as session:
                    # Read-only резолв (не get_or_create): между
                    # find_pending_pdn_retention() и этой итерацией строка
                    # жителя могла исчезнуть (другая сессия, житель успел
                    # /forget). get_or_create молча вставил бы фантомного
                    # User и erase_pdn отработал бы повторно с вводящей в
                    # заблуждение audit-записью auto_erase_pdn_retention.
                    user = await users_service.find_by_max_id(
                        session, max_user_id
                    )
                    if user is None:
                        continue
                    # Считаем, у кого срок истёк с ещё открытыми
                    # обращениями — не чтобы пропустить (это и было
                    # нарушением), а чтобы отдельно предупредить
                    # оператора: эти обращения закроются без ответа.
                    had_open = await users_service.has_open_appeals(
                        session, user.id
                    )
                    ok = await users_service.erase_pdn(
                        session, max_user_id, revoked_before=datetime.now(TZ) - timedelta(days=29),
                    )
                    if ok:
                        await operators_service.write_audit(
                            session,
                            operator_max_user_id=None,
                            action="auto_erase_pdn_retention",
                            target=None,
                            details={"reason": "очистка после отзыва согласия; запас до предельного срока"},
                        )
                        erased += 1
                        if had_open:
                            skipped_open += 1
            except Exception:
                log.exception("pdn_retention: не удалось очистить запись жителя")
        # critical=True (sec/A1): уничтожение ПДн по отзыву согласия —
        # compliance-критичное событие. Cron 0 6 * * * попадает в default
        # quiet окно [18, 9). Без critical ответственный не узнает, что
        # обезличивание произошло (либо что оно НЕ произошло из-за бага),
        # до утра следующего рабочего дня. Это ТЕХНИЧЕСКОЕ решение проекта:
        # ст. 21 ч. 5 задаёт срок уничтожения (30 дней), но требования
        # уведомлять ответственное лицо в ней нет — не приписываем закону
        # того, чего он не говорит.
        if skipped_open:
            tail = (
                f" Из них {skipped_open} — с обращениями, закрытыми без "
                f"ответа после отзыва согласия."
                if skipped_open
                else ""
            )
            await send_admin_text(
                f"🛡 Очистка ПДн по сроку отзыва согласия: "
                f"очищено записей в рабочей базе: {erased}.{tail}",
                critical=True,
            )
        elif erased:
            log.info("pdn_retention: очищено записей: %d", erased)

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
                from aemr_bot.services import resident_drafts
                from aemr_bot.utils.resident_lifecycle import guard
                async with guard(max_user_id), session_scope() as session:
                    user = await users_service.find_by_max_id(session, max_user_id, for_update=True)
                    cutoff = datetime.now(UTC) - timedelta(seconds=cutoff_seconds)
                    if (user is None or user.dialog_state != _state or user.updated_at > cutoff
                            or (user.dialog_data or {}).get("_name_resume")):
                        continue
                    if user.appeal_draft:
                        # Do not overwrite a previously paused draft.
                        continue
                    paused = await resident_drafts.pause(session, user)
                    if not paused:
                        await users_service.reset_state(session, max_user_id)
                # Perf «2 RPS»: watchdog шлёт жителям напрямую через
                # bot.send_message (хукнутый инстанс) → каждое уведомление
                # проходит глобальный лимитер исходящих
                # (admin_bus.install_outgoing_tracker_hook). При лавине
                # зависших анкет после простоя это не даёт watchdog'у
                # выжать MAX до 429 и подавить рассылку/интерактив —
                # уведомления растягиваются под общий бюджет ~1.5 msg/s.
                await bot.send_message(
                    user_id=max_user_id,
                    text=("Вы начали обращение, но пока не отправили его. "
                          "Черновик сохранён: продолжить можно из главного меню."
                          if paused else
                          "Вы начали оформление, но не завершили его. "
                          "Чтобы начать снова, откройте главное меню."),
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
            # critical=True (sec/A1): аномальный спайк в воронке —
            # сигнал о UX-баге или DDoS. Должен пробить quiet режим
            # (cron каждый час 24/7, лёгко попадает в [18, 9)).
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
                critical=True,
            )
    except Exception:
        log.exception("funnel_watchdog crashed")


async def _job_working_hours_open_reminder(bot) -> None:
    """Раз в час, ТОЛЬКО в рабочее время АЕМО:
    пн–пт 09:00–11:59 и 13:00–17:59 Камчатка, не в праздники РФ.

    Напоминание о всех неответленных обращениях — без разделения на
    «в SLA» и «просрочено». Если открытых нет — тишина (по нулям не
    пишем, чтобы оператор не привык игнорировать).

    Под сообщением кнопка «📋 Открытые обращения» — тап открывает
    полный список с кнопками действий по каждому.

    Гейт `admin_notify_open_reminder` (services/notify_toggles.py):
    выключен — напоминалка не считается и не отправляется.
    """
    try:
        from aemr_bot.services import notify_toggles
        from aemr_bot.services.quiet_hours import is_quiet_hours_now

        if is_quiet_hours_now() or not notify_toggles.is_enabled("admin_notify_open_reminder"):
            log.debug("open-reminder: admin_notify_open_reminder disabled, suppressed")
            return
        if not sla_service.is_working_time(datetime.now(TZ)):
            return

        async with session_scope() as session:
            appeals = await appeals_service.list_unanswered(session)
            total = await appeals_service.count_open(session)
            first_replies = await appeals_service.first_operator_reply_times(session, [a.id for a in appeals])
        if not appeals:
            await _claim_reminder_snapshot("open_reminder", [], 0)
            return
        # Просрочка — по рабочему времени (services/sla.py), не
        # календарному: обращение из пятничного вечера не «горит» в
        # субботу. Раньше здесь же соседствовали datetime.now(timezone.utc)
        # и datetime.now(TZ) в одной функции (находка ревью) — унифицировано
        # на TZ (локальное время бота), т.к. sla_service сам приводит к TZ
        # внутри, а now(TZ) читается счётчиком возраста ниже без конверсии.
        now = datetime.now(TZ)
        # is_overdue() итерирует по рабочим дням от created_at до now — для
        # годовалого обращения это сотни итераций. Считаем один раз на
        # обращение и переиспользуем флаг, а не зовём дважды на каждое.
        with_reply = [a for a in appeals if a.id in first_replies]
        flags = [
            (a, sla_service.is_overdue(getattr(a, "sla_anchor_at", None) or a.created_at, now,
                                    appeals_service.response_hours_for(a, settings.sla_response_hours)))
            for a in appeals if a.id not in first_replies
        ]
        in_sla = [a for a, is_od in flags if not is_od]
        overdue = [a for a, is_od in flags if is_od]
        signature_groups = [sorted(a.id for a in group) for group in (overdue, in_sla, with_reply)]
        if not await _claim_reminder_snapshot("open_reminder", signature_groups, total):
            return
        if overdue:
            # The main digest already contains this overdue set. The separate
            # overdue job should send only if that set subsequently changes.
            await _claim_reminder_snapshot("overdue_reminder", [sorted(a.id for a in overdue)], len(overdue))
        header = (
            f"📋 Открытых обращений: {total}. "
            f"Без первичного ответа: в срок — {len(in_sla)}, просрочено — {len(overdue)}; "
            f"с промежуточным ответом — {len(with_reply)}."
        )
        if total > len(appeals):
            header += f" Показаны первые {len(appeals)}."
        lines = [header, ""]
        if overdue:
            lines.append("⚠️ Просрочено по внутреннему сроку первого ответа:")
            lines.extend(_format_appeal_lines(overdue))
            lines.append("")
        if in_sla:
            lines.append("🆕 В SLA:")
            lines.extend(_format_appeal_lines(in_sla))
        if with_reply:
            lines.append("💬 В работе после первичного ответа:")
            lines.extend(_format_appeal_lines(with_reply, anchors=first_replies))
        await _send_with_open_tickets_button(bot, "\n".join(lines).rstrip())
    except Exception:
        log.exception("working_hours_open_reminder crashed")


async def _job_working_hours_overdue_reminder(bot) -> None:
    """Раз в час на :40 — отдельное напоминание ТОЛЬКО о просроченных.
    Вместе с :10 даёт «каждые полчаса для просрочки». Если нет
    просроченных — тишина. В госпраздники РФ молчит.

    Гейт `admin_notify_overdue_reminder` (services/notify_toggles.py):
    выключен — напоминалка не считается и не отправляется.
    """
    try:
        from aemr_bot.services import notify_toggles
        from aemr_bot.services.quiet_hours import is_quiet_hours_now

        if is_quiet_hours_now() or not notify_toggles.is_enabled("admin_notify_overdue_reminder"):
            log.debug(
                "overdue-reminder: admin_notify_overdue_reminder disabled, suppressed"
            )
            return
        if not sla_service.is_working_time(datetime.now(TZ)):
            return

        async with session_scope() as session:
            overdue = await appeals_service.find_overdue_unanswered(
                session, settings.sla_response_hours
            )
        if not overdue:
            await _claim_reminder_snapshot("overdue_reminder", [], 0)
            return
        if not await _claim_reminder_snapshot("overdue_reminder", [sorted(a.id for a in overdue)], len(overdue)):
            return
        lines = [
            (f"⚠️ Просрочено по внутреннему сроку первого ответа: "
            f"{len(overdue)} обращений.")
        ]
        lines.extend(_format_appeal_lines(overdue))
        await _send_with_open_tickets_button(bot, "\n".join(lines))
    except Exception:
        log.exception("working_hours_overdue_reminder crashed")


def _on_job_error(event: JobExecutionEvent) -> None:
    """Централизованная сеть безопасности для ошибок ДО входа в try job'а.

    Каждый _job_* сам оборачивает свою логику в try/except и логирует
    через log.exception — но это защищает только от исключений ВНУТРИ
    тела корутины. Ошибки на более раннем этапе (например, сама
    corutine не может быть создана, или APScheduler не смог её
    запустить) раньше просто терялись — их некому было залогировать.
    add_listener на EVENT_JOB_ERROR ловит такие случаи для любого job'а
    без необходимости трогать каждый по отдельности.

    Без отправки в админ-чат: это подстраховка на случай сбоя ДО
    попадания в try, а не основной канал алёртов — не плодим шум,
    основные алёрты уже идут через _send_admin_text_with_retry внутри
    каждого job'а.
    """
    log.error(
        "cron job %s упал вне собственного try/except",
        event.job_id,
        exc_info=event.exception,
    )


# ============================================================================
# build_scheduler — теперь только конфигурация, jobs снаружи
# ============================================================================


class GracefulAsyncIOScheduler(AsyncIOScheduler):
    """Drain our coroutine jobs before APScheduler cancels executor futures.

    APScheduler 3.11's AsyncIOExecutor ignores shutdown(wait=True). Keep an
    application-owned registry instead of depending on executor internals.
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._running_jobs: dict[asyncio.Task, str] = {}

    def track_job(self, func: Callable[[], Awaitable[Any]], name: str):
        @functools.wraps(func)
        async def tracked():
            task = asyncio.current_task()
            assert task is not None
            self._running_jobs[task] = name
            try:
                return await func()
            finally:
                self._running_jobs.pop(task, None)

        return tracked

    async def shutdown_gracefully(self, timeout: float = 10.0) -> list[str]:
        """Stop new runs, allow current jobs a bounded drain, then stop APScheduler."""
        if not self.running:
            return []
        self.pause()
        # Jobs submitted in the preceding loop turn must enter track_job first.
        await asyncio.sleep(0)
        running = dict(self._running_jobs)
        pending: set[asyncio.Task] = set()
        if running:
            _, pending = await asyncio.wait(running, timeout=timeout)
        abandoned = sorted(running[task] for task in pending)
        for task in pending:
            task.cancel()
        if pending:
            # Join cancellation cleanup while DB and HTTP clients are still open.
            await asyncio.wait(pending, timeout=2.0)

        stopped = asyncio.Event()

        def on_shutdown(event) -> None:
            stopped.set()

        self.add_listener(on_shutdown, EVENT_SCHEDULER_SHUTDOWN)
        try:
            self.shutdown(wait=False)
            # AsyncIOScheduler.shutdown schedules its work on the event loop.
            await stopped.wait()
        finally:
            self.remove_listener(on_shutdown)
        return abandoned


def build_scheduler(bot, send_admin_document, send_admin_text) -> GracefulAsyncIOScheduler:
    """Собрать APScheduler со всеми job'ами бота.

    Все jobs вынесены на module-level (см. _job_* выше); здесь —
    декларативная таблица `(func, trigger, name)` и единый цикл
    регистрации. Раньше было 14 копипастных `add_job(...)` с
    идентичными `max_instances=1, coalesce=True, misfire_grace_time`
    — теперь общие параметры заданы один раз в цикле. bot принимаем
    явным параметром, чтобы services не импортировал точку входа
    `main.bot` лазево.
    """
    scheduler = GracefulAsyncIOScheduler(timezone=TZ)
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
        # Ежемесячная проверка восстановимости бэкапа (1-е число, 04:30):
        # расшифровать свежайший дамп, залить во временную БД, снести.
        # Ловит потерю парольной фразы и битые дампы ДО аварии.
        (
            functools.partial(_job_backup_verify_with_alert, send_admin_text),
            CronTrigger(
                day=settings.backup_verify_day,
                hour=settings.backup_verify_hour,
                minute=settings.backup_verify_minute,
                timezone=TZ,
            ),
            "db-backup-verify",
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
        # Reaper зависших DRAFT broadcast'ов (SECURITY_REVIEW F5). Раз
        # в час подбирает row'ы DRAFT старше 30 минут → FAILED. Защита
        # от того что cancel-handler не успел вызвать mark_cancelled
        # из-за DB-сбоя — task в asyncio был отменён, рассылка не
        # пошла, но запись в БД зависла.
        (
            _job_reap_orphaned_drafts,
            CronTrigger(minute=37, timezone=TZ),
            "broadcast-draft-reaper",
        ),
        # Threat-intel refresh: подтянуть URLhaus + ThreatFox (+ опц.
        # PhishTank) host'ы для предупреждения оператора о фишинг-
        # ссылках в обращениях жителей. Раз в час на :17 — позже
        # внутренних утренних job'ов, не пересекается по нагрузке.
        # См. services/threat_intel.py и URL_THREAT_INTEL_2026-05-26.md.
        (
            functools.partial(_job_threat_intel_refresh, send_admin_text),
            CronTrigger(minute=17, timezone=TZ),
            "threat-intel-refresh",
        ),
        # Авто-деактивация stale операторов (CVE-9 из SECURITY_REVIEW_2026-05-26).
        # Сверяет активных операторов с реальными членами админ-группы MAX
        # и мягко деактивирует тех, кого больше нет. IT-операторы не
        # трогаются (защита от self-lock-out). 04:20 — между audit-log
        # retention и pdn-retention.
        (
            functools.partial(_job_stale_operators_cleanup, bot, send_admin_text),
            CronTrigger(hour=4, minute=20, timezone=TZ),
            "stale-operators-cleanup",
        ),
        # Self-check heartbeat: следит за зависанием главного цикла бота
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
        # Pulse — сигнал жизни «бот работает». Технический heartbeat
        # для дежурного IT, не уведомление оператору. Если пульс пропал
        # в течение нескольких циклов — что-то с процессом, надо
        # смотреть logs.
        #
        # **Базовый сигнал (`pulse-hourly`):** каждый час на :05,
        # 24/7 — без исключения каких-либо часов или дней недели.
        # Это постоянная плотность мониторинга: пн-пт в нерабочие часы
        # (0:00-8:59 и 18:00-23:59) и вся суббота-воскресенье целиком
        # дают ровно один пульс в час.
        #
        # **Доп-сигнал в рабочее время (`pulse-workhours-extra`):**
        # дополнительно на :35 ТОЛЬКО в пн-пт с 9:00 до 17:59. Вместе
        # с базовым (:05) даёт ДВА пульса в час → раз в 30 минут в
        # рабочее время — высокая плотность, чтобы быстро поймать сбой
        # в момент активной работы оператора.
        #
        # Госпраздники РФ в пульсе не учитываются — мониторинг бота не
        # зависит от выходного у людей. Раньше pulse шёл только пн-сб
        # и в воскресенье отдельным job'ом, что путало; теперь два
        # ясных юнита без дублирования.
        (
            pulse,
            CronTrigger(
                minute=5,
                timezone=TZ,
            ),
            "pulse-hourly",
        ),
        (
            pulse,
            CronTrigger(
                day_of_week="mon-fri",
                hour="9-17",
                minute=35,
                timezone=TZ,
            ),
            "pulse-workhours-extra",
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
        (
            functools.partial(_job_pdn_retention_check, send_admin_text, warn=False),
            CronTrigger(minute=20, timezone=TZ),
            "pdn-retention-hourly",
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
        # v7 §39 + уточнение в v8-draft: пн-пт + обед). В Сб бот не дёргает
        # оператора — он не на смене. В обед — не дёргает тоже: hour=12
        # выпадает из расписания (`hour="9-11,13-17"`).
        #
        # Pulse-задачи (см. выше) намеренно остаются пн-пт + 24/7 — это
        # технический heartbeat «бот живой», а не уведомление оператора.
        # Heartbeat в обед и в субботу полезен (мониторинг непрерывный).
        (
            functools.partial(_job_working_hours_open_reminder, bot),
            CronTrigger(
                hour="*", minute=10, timezone=TZ
            ),
            "open-reminder-workhours",
        ),
        (
            functools.partial(_job_working_hours_overdue_reminder, bot),
            CronTrigger(
                hour="*", minute=40, timezone=TZ
            ),
            "overdue-reminder-workhours",
        ),
        # Catch-up для четырёх retention-джобов (events/pdn/appeals-5y/
        # audit-log). Misfire grace (_MISFIRE_GRACE_SEC = 120 сек) не
        # спасает от простоя ДОЛЬШЕ 2 минут в момент тика 04:xx — а
        # `docker compose up --build`, обновление хоста или сетевой сбой
        # в 04:00-04:45 запросто длится дольше. Без catch-up такой тик
        # молча теряется до следующих суток (обнаружено ревью: retention
        # 152-ФЗ должен срабатывать надёжно, а не «пока повезёт с
        # аптаймом именно в это окно»).
        #
        # Разово через +30 сек после старта — по образцу startup-pulse.
        # Безопасно запускать при КАЖДОМ старте процесса (а не только
        # после реального пропуска), потому что все четыре job'а
        # идемпотентны: каждый фильтрует по `created_at/closed_at <
        # cutoff` и просто находит 0 совпадений, если предыдущий тик уже
        # отработал. Повторный прогон не создаёт дублей и не портит
        # данные повторно.
        #
        # `db-backup` в catch-up НЕ включён — тяжёлая операция
        # (pg_dump + gpg), не годится на «а вдруг пропустили» при каждом
        # рестарте контейнера; расписание раз в неделю даёт достаточный
        # запас, а пропуск виден по алёрту `_job_backup_with_alert` и
        # отслеживается вручную через /backup.
        (
            _job_events_retention,
            DateTrigger(
                run_date=datetime.now(TZ) + timedelta(seconds=30), timezone=TZ
            ),
            "events-retention-catchup",
        ),
        (
            functools.partial(_job_pdn_retention_check, send_admin_text),
            DateTrigger(
                run_date=datetime.now(TZ) + timedelta(seconds=30), timezone=TZ
            ),
            "pdn-retention-catchup",
        ),
        (
            functools.partial(_job_appeals_5y_retention, send_admin_text),
            DateTrigger(
                run_date=datetime.now(TZ) + timedelta(seconds=30), timezone=TZ
            ),
            "appeals-5y-retention-catchup",
        ),
        (
            _job_audit_log_retention,
            DateTrigger(
                run_date=datetime.now(TZ) + timedelta(seconds=30), timezone=TZ
            ),
            "audit-log-retention-catchup",
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
            scheduler.track_job(func, name),
            trigger,
            id=name,
            name=name,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=_MISFIRE_GRACE_SEC,
            **({"next_run_time": datetime.now(TZ)} if name == "threat-intel-refresh" else {}),
        )

    from aemr_bot.handlers.appeal_runtime import recover_unpublished_appeals
    scheduler.add_job(
        scheduler.track_job(
            functools.partial(recover_unpublished_appeals, bot), "initial-card-recovery"
        ), "interval", seconds=60,
        id="initial-card-recovery", max_instances=1, coalesce=True,
        next_run_time=datetime.now(TZ), misfire_grace_time=_MISFIRE_GRACE_SEC,
    )

    # Централизованный лог ошибок ДО входа в try конкретного job'а
    # (см. docstring _on_job_error). Без send в админ-чат — не плодим шум.
    scheduler.add_listener(_on_job_error, EVENT_JOB_ERROR)

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
# main закрывает session после дренажа cron-задач, до выхода event loop.
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
            **session_kwargs(),
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


async def close_healthcheck_session() -> None:
    """Close the shared ping client after scheduler jobs have stopped."""
    global _HEALTHCHECK_SESSION
    if _HEALTHCHECK_SESSION is not None:
        await _HEALTHCHECK_SESSION.close()
        _HEALTHCHECK_SESSION = None
