"""Общие операции админ-панели: меню /op_help, диагностика, бэкап,
список открытых обращений.

Выделено из handlers/admin_commands.py (рефакторинг 2026-05-10).
Сюда попало то, что не привязано к конкретному домену (статистика /
операторы / настройки / аудитория) и используется как entry-point
для оператора."""
from __future__ import annotations

import logging

from aemr_bot.config import settings as cfg
from aemr_bot.db.session import session_scope
from aemr_bot.handlers._auth import ensure_operator, get_operator
from aemr_bot.utils.event import get_message_text, send_or_edit_screen

log = logging.getLogger(__name__)


def parse_arg(text: str) -> str:
    """Достать аргумент после команды («/cmd arg…» → «arg…»)."""
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def get_text(event) -> str:
    return get_message_text(event)


async def show_op_menu(event, *, pin: bool = False) -> None:
    """Показать памятку оператора с клавиатурой быстрых действий.

    pin=True — закрепляем сообщение (для /op_help). MAX держит одно
    закреплённое сообщение на чат. /menu, /start, /help в админке зовут
    эту же функцию с pin=False — это «открой меню сейчас».

    Перед показом смотрим, сколько обращений висит без ответа, и какая
    роль у автора события: счётчик и админ-ряд кнопок собираются по
    этим данным.
    """
    from aemr_bot import keyboards as kbds, texts
    from aemr_bot.db.models import OperatorRole
    from aemr_bot.services import appeals as appeals_service
    from aemr_bot.utils.event import extract_message_id

    is_it = False
    can_broadcast = False
    open_count: int | None = None
    async with session_scope() as session:
        op = await get_operator(event)
        if op is not None:
            is_it = op.role == OperatorRole.IT.value
            can_broadcast = op.role in {
                OperatorRole.IT.value,
                OperatorRole.COORDINATOR.value,
            }
        try:
            open_count = await appeals_service.count_open(session)
        except Exception:
            log.exception("count_open failed; кнопку без счётчика покажем")

    sent = await send_or_edit_screen(
        event,
        chat_id=cfg.admin_group_id,
        text=texts.OP_HELP.format(answer_limit=cfg.answer_max_chars),
        attachments=[
            kbds.op_help_keyboard(
                open_count=open_count, is_it=is_it, can_broadcast=can_broadcast
            )
        ],
        force_new_message=pin,
    )
    if not pin:
        return
    mid = extract_message_id(sent)
    if mid:
        try:
            await event.bot.pin_message(
                chat_id=cfg.admin_group_id, message_id=mid, notify=False
            )
        except Exception:
            log.exception("pin_message для /op_help не удался")


async def run_open_tickets(event) -> None:
    """Кнопочный аналог /open_tickets. Доступен любой роли."""
    if not await ensure_operator(event):
        return
    await _do_open_tickets(event)


async def run_diag(event) -> None:
    """Кнопочный аналог /diag — короткая сводка состояния бота."""
    if not await ensure_operator(event):
        return
    await _do_diag(event)


async def run_backup(event) -> None:
    """Кнопочный аналог /backup. Только IT."""
    from aemr_bot.db.models import OperatorRole
    from aemr_bot.handlers._auth import ensure_role

    if not await ensure_role(event, OperatorRole.IT):
        return
    await _do_backup(event)


async def _do_open_tickets(event) -> None:
    """Список открытых обращений в админ-группу. Общая реализация для
    команды /open_tickets и кнопки «📋 Открытые обращения»."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from aemr_bot import keyboards as kbds
    from aemr_bot.db.models import Appeal, AppealStatus

    async with session_scope() as session:
        query = (
            select(Appeal)
            .where(
                Appeal.status.in_(
                    [AppealStatus.NEW.value, AppealStatus.IN_PROGRESS.value]
                )
            )
            # selectinload(Appeal.messages) нужен для repeat-relay
            # вложений ниже — без него `appeal.messages` лениво ходит в
            # БД из-под закрытой сессии и валится `MissingGreenlet`.
            .options(
                selectinload(Appeal.user),
                selectinload(Appeal.messages),
            )
            .order_by(Appeal.created_at)
        )
        open_appeals = (await session.scalars(query)).all()

    if not open_appeals:
        await send_or_edit_screen(
            event,
            chat_id=cfg.admin_group_id,
            text="🎉 Нет открытых или неотвеченных обращений.",
            attachments=[kbds.op_back_to_menu_keyboard()],
        )
        return

    await send_or_edit_screen(
        event,
        chat_id=cfg.admin_group_id,
        text=f"⏳ Найдено неотвеченных обращений: {len(open_appeals)}",
        attachments=[kbds.op_back_to_menu_keyboard()],
    )

    # Sticky-tracker: tracker встаёт на mid header'а (через
    # send_or_edit_screen выше), а карточки обращений печатаются ниже
    # через `event.bot.send_message`. Без сдвига tracker оператор внизу
    # чата тапает кнопку header'а — `callback_mid == tracker` → edit
    # вверху → внизу ничего не меняется. Сдвигаем tracker на последнюю
    # отправленную карточку, чтобы любой тап выше → send_new.
    from aemr_bot.utils import menu_tracker
    from aemr_bot.utils.event import extract_message_id

    last_mid: str | None = None
    for appeal in open_appeals:
        user_name = appeal.user.first_name if appeal.user else "—"
        user_id_text = appeal.user.max_user_id if appeal.user else "—"
        # PR-fix-hang: НЕ переотправляем вложения автоматически. До этого
        # в цикле под каждое обращение шёл render_appeal_attachments
        # (1-N доп. send_message). На 20+ обращениях с фото набегало
        # 50-80 sequential bot.send_message подряд — handler «висел»
        # 30-60 секунд под одной операторской командой, livez-пинги
        # health-watch таймаутили. Теперь вложения вызываются явно
        # кнопкой «📎 Вложения (N)» в карточке.
        from aemr_bot.services.admin_relay import _collect_all_user_attachments  # noqa: PLC0415

        attachment_count = len(_collect_all_user_attachments(appeal))
        # Служебный маркер `🆔 №N` в конце — стабильный токен, по которому
        # handlers/operator_reply.py находит обращение при свайп-ответе.
        text = (
            f"❗️ Обращение #{appeal.id}\n"
            f"👤 От: {user_name}\n"
            f"📞 ID жителя: {user_id_text}\n"
            f"📍 Населённый пункт: {appeal.locality or '—'}\n"
            f"🏠 Адрес: {appeal.address or '—'}\n"
            f"🏷️ Тематика: {appeal.topic or '—'}\n\n"
            f"📝 Текст обращения:\n{appeal.summary or '—'}\n\n"
            f"🆔 №{appeal.id}"
        )
        sent = await event.bot.send_message(
            chat_id=cfg.admin_group_id,
            text=text,
            attachments=[
                kbds.appeal_admin_actions(
                    appeal.id,
                    appeal.status,
                    is_it=True,
                    user_blocked=bool(appeal.user and appeal.user.is_blocked),
                    closed_due_to_revoke=bool(appeal.closed_due_to_revoke),
                    attachment_count=attachment_count,
                )
            ],
        )
        mid = extract_message_id(sent)
        if mid:
            last_mid = mid
    if last_mid is not None and cfg.admin_group_id:
        menu_tracker.set_last_menu_mid(cfg.admin_group_id, last_mid)


async def _do_diag(event) -> None:
    """Сводка состояния бота с actionable indicators (PR I).

    Расширено по сравнению с v1:
    - 24-часовая активность (новые жители / новые обращения / ответы /
      рассылки) — показывает «живёт ли система»;
    - Pulse-индикатор (минут с последнего события + ✅/⚠️) — отвечает на
      вопрос «бот в порядке прямо сейчас?»;
    - Зависшие SENDING-рассылки (>10 мин без обновления прогресса) —
      явный warning, чтобы оператор знал, что нужно остановить + clean-up;
    - Срез по failed-доставкам за 24ч — индикатор проблем с MAX-API;
    - 24-часовой список warnings ниже отдельным блоком (пусто = «всё ок»).

    Конфиг (режим, лимит ответа, SLA) сохраняем в конце — статика.
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import func, select

    from aemr_bot import keyboards as kbds
    from aemr_bot.db.models import (
        Appeal,
        AppealStatus,
        Broadcast,
        BroadcastDelivery,
        BroadcastStatus,
        Event,
        Message,
        User,
    )

    now = datetime.now(timezone.utc)
    since_24h = now - timedelta(hours=24)
    stuck_threshold = now - timedelta(minutes=10)

    # broadcasts_service._eligible_filter — то же что фильтр фактической
    # рассылки. Используем здесь, чтобы /diag показывал именно тех, кому
    # реально уйдёт следующий broadcast (а не просто
    # subscribed_broadcast=True). Раньше /diag показывал больше, чем
    # рассылка доставляла — оператор удивлялся «подписан 100, ушло 80».
    from aemr_bot.services import broadcasts as broadcasts_service

    async with session_scope() as session:
        users_total = await session.scalar(select(func.count()).select_from(User))
        # «Активные» — без обезличенных (first_name='Удалено' после
        # /forget или PDN-retention). Это «живые жители», которые
        # могут писать боту. users_total включает их для прозрачности
        # «сколько записей в БД всего».
        users_active = await session.scalar(
            select(func.count()).select_from(User).where(
                User.first_name != "Удалено",
            )
        )
        users_blocked = await session.scalar(
            select(func.count()).select_from(User).where(User.is_blocked.is_(True))
        )
        # Eligible subscribers: ровно те, кому уйдёт рассылка.
        # Старый счётчик (только subscribed_broadcast=True) расходился
        # с фактической доставкой, потому что не учитывал
        # consent_broadcast_at IS NOT NULL и first_name != 'Удалено'.
        users_subscribed = await broadcasts_service.count_subscribers(session)
        users_new_24h = await session.scalar(
            select(func.count()).select_from(User).where(
                User.created_at >= since_24h
            )
        )
        appeals_total = await session.scalar(select(func.count()).select_from(Appeal))
        appeals_in_progress = await session.scalar(
            select(func.count()).select_from(Appeal).where(
                Appeal.status.in_([
                    AppealStatus.NEW.value,
                    AppealStatus.IN_PROGRESS.value,
                ])
            )
        )
        appeals_new_24h = await session.scalar(
            select(func.count()).select_from(Appeal).where(
                Appeal.created_at >= since_24h
            )
        )
        # Direction в БД — MessageDirection enum (from_user / from_operator /
        # system). До этого фикса было "to_user" — невалидное значение,
        # счётчик ВСЕГДА показывал 0. Это причина того, что /diag
        # «Ответов оператора за 24ч» всегда был 0 несмотря на ответы.
        from aemr_bot.db.models import MessageDirection

        replies_24h = await session.scalar(
            select(func.count()).select_from(Message).where(
                Message.direction == MessageDirection.FROM_OPERATOR.value,
                Message.created_at >= since_24h,
            )
        )
        broadcasts_done = await session.scalar(
            select(func.count()).select_from(Broadcast).where(
                Broadcast.status == BroadcastStatus.DONE.value
            )
        )
        broadcasts_failed = await session.scalar(
            select(func.count()).select_from(Broadcast).where(
                Broadcast.status == BroadcastStatus.FAILED.value
            )
        )
        broadcasts_24h = await session.scalar(
            select(func.count()).select_from(Broadcast).where(
                Broadcast.created_at >= since_24h
            )
        )
        broadcasts_stuck = await session.scalar(
            select(func.count()).select_from(Broadcast).where(
                Broadcast.status == BroadcastStatus.SENDING.value,
                Broadcast.created_at < stuck_threshold,
            )
        )
        delivery_failed_24h = await session.scalar(
            select(func.count()).select_from(BroadcastDelivery).where(
                BroadcastDelivery.error.isnot(None),
                BroadcastDelivery.delivered_at.is_(None),
                # broadcast_deliveries не имеет created_at; используем
                # связку через broadcast.created_at >= since_24h.
            )
            .join(Broadcast, Broadcast.id == BroadcastDelivery.broadcast_id)
            .where(Broadcast.created_at >= since_24h)
        )
        events_total = await session.scalar(select(func.count()).select_from(Event))
        last_event = await session.scalar(select(func.max(Event.received_at)))

    # Pulse-индикатор: сколько минут назад был последний event. Бот
    # шлёт heartbeat по cron, поэтому «давно не было событий» — явный
    # signal проблемы. Граница 15 мин выбрана с запасом: pulse-cron
    # стреляет :00, :30 или подобными интервалами, окно 15 мин ловит
    # «один пропущенный pulse-цикл», но не дёргает на нормальный idle.
    # last_event=None трактуем как WARN: либо свежий старт без событий,
    # либо events таблица только что purge'нута retention-cron. В обоих
    # случаях оператору полезно знать «pulse событий нет вовсе» —
    # раньше /diag показывал «—» без warn, на свежем кластере
    # выглядело как «всё ок».
    pulse_warn = last_event is None
    if last_event is None:
        pulse_line = "⚠️ событий нет вовсе (свежий старт?)"
    else:
        if last_event.tzinfo is None:
            last_event = last_event.replace(tzinfo=timezone.utc)
        minutes_ago = int((now - last_event).total_seconds() // 60)
        if minutes_ago < 1:
            pulse_line = "< 1 мин назад"
        elif minutes_ago < 60:
            pulse_line = f"{minutes_ago} мин назад"
        else:
            hours = minutes_ago // 60
            pulse_line = f"{hours} ч {minutes_ago % 60} мин назад"
        if minutes_ago > 15:
            pulse_warn = True
            pulse_line = f"⚠️ {pulse_line}"
        else:
            pulse_line = f"✅ {pulse_line}"

    warnings_lines: list[str] = []
    if pulse_warn:
        warnings_lines.append(
            f"⚠️ Pulse молчит {pulse_line.replace('⚠️ ', '')} — проверить, "
            f"что cron здоров."
        )
    if (broadcasts_stuck or 0) > 0:
        warnings_lines.append(
            f"⚠️ Зависших рассылок в SENDING (старше 10 мин): "
            f"{broadcasts_stuck}. Остановите кнопкой ⛔ или проверьте бот."
        )
    if (delivery_failed_24h or 0) >= 20:
        warnings_lines.append(
            f"⚠️ За 24ч {delivery_failed_24h} неуспешных доставок рассылок — "
            f"проверьте «👥 Не доставлено» у недавних рассылок."
        )

    body = (
        "🛠️ Диагностика\n"
        "\n"
        "Pulse:\n"
        f"• Последнее событие: {pulse_line}\n"
        f"• Всего событий: {events_total or 0}\n"
        "\n"
        "Жители:\n"
        f"• Записей всего: {users_total or 0} "
        f"(активных: {users_active or 0}, заблокированы: {users_blocked or 0})\n"
        f"• Получателей рассылки: {users_subscribed or 0} "
        f"(подписаны + согласие + не заблокированы + не обезличены)\n"
        f"• Новых за 24ч: {users_new_24h or 0}\n"
        "\n"
        "Обращения:\n"
        f"• Всего: {appeals_total or 0} (в работе: {appeals_in_progress or 0})\n"
        f"• Новых за 24ч: {appeals_new_24h or 0}\n"
        f"• Ответов оператора за 24ч: {replies_24h or 0}\n"
        "\n"
        "Рассылки:\n"
        f"• ✅ DONE: {broadcasts_done or 0}  ⚠️ FAILED: {broadcasts_failed or 0}\n"
        f"• За 24ч запущено: {broadcasts_24h or 0}\n"
        f"• Зависших SENDING >10мин: {broadcasts_stuck or 0}\n"
        f"• Неуспешных доставок за 24ч: {delivery_failed_24h or 0}\n"
        "\n"
        "Конфигурация:\n"
        f"• Режим: {cfg.bot_mode}\n"
        f"• Лимит ответа: {cfg.answer_max_chars} симв.\n"
        f"• SLA: {cfg.sla_response_hours} ч"
    )
    if warnings_lines:
        body += "\n\nВнимание:\n" + "\n".join(warnings_lines)
    else:
        body += "\n\n✅ Аномалий не обнаружено."

    await send_or_edit_screen(
        event,
        chat_id=cfg.admin_group_id,
        text=body,
        attachments=[kbds.op_back_to_menu_keyboard()],
    )


async def _do_backup(event) -> None:
    """Снять pg_dump прямо сейчас. Общая реализация для /backup и кнопки."""
    from aemr_bot import keyboards as kbds
    from aemr_bot.services import db_backup

    await send_or_edit_screen(
        event,
        chat_id=cfg.admin_group_id,
        text="🗄️ Запускаю pg_dump… Это может занять несколько секунд.",
        attachments=[kbds.op_back_to_menu_keyboard()],
    )
    try:
        result = await db_backup.backup_db()
    except Exception as e:
        await send_or_edit_screen(
            event,
            chat_id=cfg.admin_group_id,
            text=f"⚠️ Бэкап упал: {e}",
            attachments=[kbds.op_back_to_menu_keyboard()],
        )
        return
    if not result.ok:
        # Категоризированное сообщение по типу провала: то же различение,
        # что в cron-алёртах (см. cron._job_backup_with_alert).
        if result.fail_kind == "pg_dump":
            err_text = (
                f"⚠️ pg_dump упал: {result.fail_detail}\n"
                "Проверьте Postgres и место на диске."
            )
        elif result.fail_kind == "gpg":
            err_text = (
                f"🔐 pg_dump прошёл, но gpg-шифрование упало: "
                f"{result.fail_detail}\n"
                "Незашифрованный дамп удалён (ПДн нельзя оставлять). "
                "Проверьте BACKUP_GPG_PASSPHRASE."
            )
        elif result.fail_kind == "config":
            err_text = (
                "⚙️ Бэкап не выполнен: BACKUP_LOCAL_DIR пуст. "
                "Проверьте `.env` (`docs/SYSADMIN.md §5.4`)."
            )
        else:
            err_text = (
                f"⚠️ Бэкап не выполнен ({result.fail_kind}): "
                f"{result.fail_detail}\n"
                "Проверьте логи: `docker compose logs bot --tail 50`."
            )
        await send_or_edit_screen(
            event,
            chat_id=cfg.admin_group_id,
            text=err_text,
            attachments=[kbds.op_back_to_menu_keyboard()],
        )
        return
    # result.ok гарантирует result.path не None (см. BackupResult.ok),
    # но mypy этого не выводит — assert закрывает union-narrowing.
    out = result.path
    assert out is not None
    size_kb = out.stat().st_size // 1024
    await send_or_edit_screen(
        event,
        chat_id=cfg.admin_group_id,
        text=(
            f"✅ Бэкап готов: `{out.name}` ({size_kb} КБ).\n"
            f"Лежит в named-volume `backups` контейнера."
        ),
        attachments=[kbds.op_back_to_menu_keyboard()],
    )
