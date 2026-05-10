"""Управление настройками бота через /setting и кнопочный меню.

Выделено из handlers/admin_commands.py (рефакторинг 2026-05-10).
"""
from __future__ import annotations

import json
import logging

from aemr_bot.config import settings as cfg
from aemr_bot.db.models import OperatorRole
from aemr_bot.db.session import session_scope
from aemr_bot.handlers._auth import ensure_role
from aemr_bot.services import settings_store

log = logging.getLogger(__name__)


async def run_settings_menu(event) -> None:
    """Меню «⚙️ Настройки бота» в админ-панели для роли it. Список ключей
    с возможностью посмотреть текущее значение и подсказать команду
    для редактирования."""
    from aemr_bot import keyboards as kbds

    if not await ensure_role(event, OperatorRole.IT):
        return
    async with session_scope() as session:
        keys = await settings_store.list_keys(session)
    await event.bot.send_message(
        chat_id=cfg.admin_group_id,
        text=(
            "⚙️ Настройки бота\n"
            "──────────\n"
            "Тапните ключ, чтобы увидеть текущее значение и шаблон команды "
            "для изменения. Сложные ключи (списки, объекты) удобнее править "
            "командой /setting <ключ> <JSON> — для них кнопка пока показывает "
            "только текущее значение."
        ),
        attachments=[kbds.op_settings_keys_keyboard(keys)],
    )


async def run_settings_action(event, payload: str) -> None:
    """`op:setkey:<key>` — показать текущее значение настройки и шаблон
    команды для редактирования. Полный wizard для каждого типа значения
    был бы перегружен; это компромисс между «кнопками» и «текстом»."""
    from aemr_bot.utils.event import ack_callback

    if not await ensure_role(event, OperatorRole.IT):
        return
    key = payload.removeprefix("op:setkey:")
    if not key:
        await ack_callback(event)
        return
    async with session_scope() as session:
        value = await settings_store.get(session, key)
    rendered = (
        json.dumps(value, ensure_ascii=False, indent=2) if value is not None else "—"
    )
    if len(rendered) > 1500:
        rendered = rendered[:1500] + "\n…(значение обрезано)"
    rule = settings_store.SCHEMA.get(key, {})
    expected = rule.get("type", "?")
    expected_name = expected.__name__ if hasattr(expected, "__name__") else str(expected)
    await ack_callback(event)
    await event.bot.send_message(
        chat_id=cfg.admin_group_id,
        text=(
            f"⚙️ Настройка «{key}» (тип {expected_name})\n"
            f"──────────\n"
            f"Текущее значение:\n{rendered}\n"
            f"──────────\n"
            f"Изменить: /setting {key} <новое значение>\n"
            f"Для списков и объектов передавайте JSON."
        ),
    )
