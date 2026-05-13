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
            .options(selectinload(Appeal.user))
            .order_by(Appeal.created_at)
        )
        open_appeals = (await session.scalars(query)).all()

    if not open_appeals:
        await send_or_edit_screen(
            event,
            chat_id=cfg.admin_group_id,
            text="🎉 Нет открытых или неотвеченных обращений.",
        )
        return

    await send_or_edit_screen(
        event,
        chat_id=cfg.admin_group_id,
        text=f"⏳ Найдено неотвеченных обращений: {len(open_appeals)}",
    )

    for appeal in open_appeals:
        user_name = appeal.user.first_name if appeal.user else "—"
        user_id_text = appeal.user.max_user_id if appeal.user else "—"
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
        await event.bot.send_message(
            chat_id=cfg.admin_group_id,
            text=text,
            attachments=[
                kbds.appeal_admin_actions(
                    appeal.id,
                    appeal.status,
                    is_it=True,
                    user_blocked=bool(appeal.user and appeal.user.is_blocked),
                    closed_due_to_revoke=bool(appeal.closed_due_to_revoke),
                )
            ],
        )


async def _do_diag(event) -> None:
    """Сводка состояния бота. Общая реализация для /diag и кнопки."""
    from sqlalchemy import func, select

    from aemr_bot.db.models import (
        Appeal,
        AppealStatus,
        Broadcast,
        BroadcastStatus,
        Event,
        User,
    )

    async with session_scope() as session:
        users_total = await session.scalar(select(func.count()).select_from(User))
        users_blocked = await session.scalar(
            select(func.count()).select_from(User).where(User.is_blocked.is_(True))
        )
        users_subscribed = await session.scalar(
            select(func.count()).select_from(User).where(
                User.subscribed_broadcast.is_(True),
                User.is_blocked.is_(False),
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
        events_total = await session.scalar(select(func.count()).select_from(Event))
        last_event = await session.scalar(select(func.max(Event.received_at)))

    await send_or_edit_screen(
        event,
        chat_id=cfg.admin_group_id,
        text=(
            "🛠️ Диагностика:\n"
            f"• Жителей: {users_total or 0} "
            f"(подписаны: {users_subscribed or 0}, заблокированы: {users_blocked or 0})\n"
            f"• Обращений: {appeals_total or 0} "
            f"(в работе: {appeals_in_progress or 0})\n"
            f"• Рассылок: ✅ {broadcasts_done or 0} / ⚠️ {broadcasts_failed or 0}\n"
            f"• События: всего {events_total or 0}, последнее {last_event or '—'}\n"
            f"• Режим: {cfg.bot_mode}\n"
            f"• Лимит ответа: {cfg.answer_max_chars}\n"
            f"• SLA: {cfg.sla_response_hours}ч"
        ),
    )


async def _do_backup(event) -> None:
    """Снять pg_dump прямо сейчас. Общая реализация для /backup и кнопки."""
    from aemr_bot.services import db_backup

    await event.bot.send_message(
        chat_id=cfg.admin_group_id,
        text="🗄️ Запускаю pg_dump… Это может занять несколько секунд.",
    )
    try:
        out = await db_backup.backup_db()
    except Exception as e:
        await event.bot.send_message(
            chat_id=cfg.admin_group_id, text=f"⚠️ Бэкап упал: {e}"
        )
        return
    if out is None:
        await event.bot.send_message(
            chat_id=cfg.admin_group_id,
            text=(
                "⚠️ Бэкап не выполнен. Проверьте логи бота "
                "(`docker compose logs bot --tail 50`)."
            ),
        )
        return
    size_kb = out.stat().st_size // 1024
    await event.bot.send_message(
        chat_id=cfg.admin_group_id,
        text=(
            f"✅ Бэкап готов: `{out.name}` ({size_kb} КБ).\n"
            f"Лежит в named-volume `backups` контейнера."
        ),
    )
