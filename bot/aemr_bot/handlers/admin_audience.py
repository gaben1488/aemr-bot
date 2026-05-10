"""Меню «📊 Аудитория и согласия» — IT-выборки + точечные действия
над жителем (block / unblock / erase).

Выделено из handlers/admin_commands.py (рефакторинг 2026-05-10).
"""
from __future__ import annotations

import logging

from aemr_bot import texts
from aemr_bot.config import settings as cfg
from aemr_bot.db.models import OperatorRole
from aemr_bot.db.session import session_scope
from aemr_bot.handlers._auth import ensure_role
from aemr_bot.services import operators as operators_service
from aemr_bot.services import users as users_service
from aemr_bot.utils.event import get_user_id

log = logging.getLogger(__name__)


async def run_audience_menu(event) -> None:
    """Меню «📊 Аудитория и согласия» для IT — точка входа в три списка."""
    from aemr_bot import keyboards as kbds

    if not await ensure_role(event, OperatorRole.IT):
        return
    await event.bot.send_message(
        chat_id=cfg.admin_group_id,
        text=(
            "📊 Аудитория и согласия\n"
            "────────────────\n"
            "Выберите выборку. Показываем по 20 записей; для большего "
            "объёма используйте /stats или прямой SQL."
        ),
        attachments=[kbds.op_audience_menu_keyboard()],
    )


async def run_audience_action(event, payload: str) -> None:
    """Обработчик `op:aud:*`. Подменю — три категории списков; точечные
    действия рядом с записью — блок/разблок и удаление ПДн.

    Формат payload:
    `op:aud:subs|consent|blocked` — открыть категорию
    `op:aud:block|unblock|erase:<max_user_id>` — действие над пользователем
    """
    from aemr_bot import keyboards as kbds
    from aemr_bot.utils.event import ack_callback

    if not await ensure_role(event, OperatorRole.IT):
        return
    suffix = payload.removeprefix("op:aud:")
    await ack_callback(event)
    actor_id = get_user_id(event)

    # Сначала проверим точечные действия по max_user_id.
    if ":" in suffix:
        action, target_str = suffix.split(":", 1)
        try:
            target_id = int(target_str)
        except ValueError:
            return
        if action == "block":
            async with session_scope() as session:
                ok = await users_service.set_blocked(
                    session, target_id, blocked=True
                )
                if ok:
                    await operators_service.write_audit(
                        session,
                        operator_max_user_id=actor_id,
                        action="block",
                        target=f"user max_id={target_id}",
                    )
            await event.bot.send_message(
                chat_id=cfg.admin_group_id,
                text=texts.OP_USER_BLOCKED.format(max_user_id=target_id)
                if ok
                else "Не удалось.",
            )
            return
        if action == "unblock":
            async with session_scope() as session:
                ok = await users_service.set_blocked(
                    session, target_id, blocked=False
                )
                if ok:
                    await operators_service.write_audit(
                        session,
                        operator_max_user_id=actor_id,
                        action="unblock",
                        target=f"user max_id={target_id}",
                    )
            await event.bot.send_message(
                chat_id=cfg.admin_group_id,
                text=texts.OP_USER_UNBLOCKED.format(max_user_id=target_id)
                if ok
                else "Не удалось.",
            )
            return
        if action == "erase":
            async with session_scope() as session:
                ok = await users_service.erase_pdn(session, target_id)
                if ok:
                    await operators_service.write_audit(
                        session,
                        operator_max_user_id=actor_id,
                        action="erase",
                        target=f"user max_id={target_id}",
                    )
            await event.bot.send_message(
                chat_id=cfg.admin_group_id,
                text=texts.OP_USER_ERASED.format(max_user_id=target_id)
                if ok
                else "Не удалось.",
            )
            return

    # Иначе — открыть выборку.
    async with session_scope() as session:
        if suffix == "subs":
            users = await users_service.list_subscribers(session)
            header = f"📩 Подписчики (показано {len(users)}):"
        elif suffix == "consent":
            users = await users_service.list_consented(session)
            header = f"🔐 Дали согласие на ПДн (показано {len(users)}):"
        elif suffix == "blocked":
            users = await users_service.list_blocked(session)
            header = f"🚫 Заблокированные (показано {len(users)}):"
        else:
            return

    if not users:
        await event.bot.send_message(
            chat_id=cfg.admin_group_id,
            text=f"{header}\n\nСписок пуст.",
        )
        return
    await event.bot.send_message(chat_id=cfg.admin_group_id, text=header)
    for u in users:
        name = u.first_name or "—"
        phone = _mask_phone(u.phone)
        line = f"#{u.max_user_id} · {name} · {phone}"
        await event.bot.send_message(
            chat_id=cfg.admin_group_id,
            text=line,
            attachments=[
                kbds.op_audience_user_actions(u.max_user_id, blocked=u.is_blocked)
            ],
        )


def _mask_phone(phone: str | None) -> str:
    """Маскирование телефона для admin-выборок: «+7***1234».

    PII в admin-чате попадает в backup MAX-серверов и в скриншоты
    операторов; 152-ФЗ erasure эту копию не достанет. Полный номер
    нужен реально только при /erase phone= — точечно. В list-выводах
    оставляем 4 последние цифры и страновой префикс для распознавания.
    Если телефон не задан — «—»; если короче 4 цифр (мусор) —
    показываем как есть, скрывать там нечего.
    """
    if not phone:
        return "—"
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) < 4:
        return phone
    tail = digits[-4:]
    prefix = "+7" if digits[0] in {"7", "8"} and len(digits) >= 11 else "+"
    return f"{prefix}***{tail}"
