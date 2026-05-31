"""Списки строк настроек (topics, localities) — CRUD по строкам.

Выделено из god-объекта `admin_settings.py`. Связная ответственность:
карточка списка, удаление строки по индексу, добавление строки через
intent. Валидация (min/max items) — в `settings_store.validate`;
каждое изменение пишется в audit_log.

Intent на добавление ставится в диспетчере `_route_set_action`
(фасад), сюда приходит уже применение `_apply_list_add`.
"""
from __future__ import annotations

from aemr_bot import keyboards as kbds
from aemr_bot.config import settings as cfg
from aemr_bot.db.session import session_scope
from aemr_bot.services import operators as ops_svc
from aemr_bot.services import settings_store
from aemr_bot.utils.event import send_or_edit_screen


async def _show_list_card(event, key: str) -> None:

    async with session_scope() as session:
        items = await settings_store.get(session, key) or []
    if not isinstance(items, list):
        items = []
    title_map = {
        "topics": "🏷 Тематики обращений",
        "localities": "📍 Населённые пункты",
    }
    title = title_map.get(key, key)
    if items:
        body = "\n".join(f"{i+1}. {x}" for i, x in enumerate(items))
    else:
        body = "(список пуст)"
    await send_or_edit_screen(
        event, chat_id=cfg.admin_group_id,
        text=(
            f"{title} ({len(items)})\n"
            f"· · · · · · · ·\n"
            f"{body}\n"
            f"· · · · · · · ·\n"
            f"Тап «🗑 N» — удалить запись.\n"
            f"Тап «➕ Добавить» — добавить новую."
        ),
        attachments=[kbds.op_settings_list_keyboard(key, items)],
    )


async def _list_delete(event, operator_id: int, suffix: str) -> None:

    parts = suffix.split(":", 1)
    if len(parts) != 2:
        return
    key, idx_str = parts[0], parts[1]
    try:
        idx = int(idx_str)
    except ValueError:
        return
    async with session_scope() as session:
        items = await settings_store.get(session, key) or []
        if not isinstance(items, list) or idx < 0 or idx >= len(items):
            await send_or_edit_screen(
                event, chat_id=cfg.admin_group_id,
                text="Элемент не найден.",
                attachments=[kbds.op_back_to_settings_keyboard()],
            )
            return
        removed = items.pop(idx)
        ok, msg = settings_store.validate(key, items)
        if not ok:
            await send_or_edit_screen(
                event, chat_id=cfg.admin_group_id,
                text=f"Удаление отменено: {msg}",
                attachments=[kbds.op_back_to_settings_keyboard()],
            )
            return
        await settings_store.set_value(session, key, items)
        await ops_svc.write_audit(
            session,
            operator_max_user_id=operator_id,
            action="setting_list_del",
            target=key,
            details={"removed": removed, "index": idx},
        )
    await _show_list_card(event, key)


async def _apply_list_add(
    event, operator_id: int, key: str, new_text: str
) -> None:

    if len(new_text) < 1:
        await event.bot.send_message(
            chat_id=cfg.admin_group_id,
            text="❌ Пустая строка.",
            attachments=[kbds.op_settings_text_cancel_keyboard(key)],
        )
        return
    async with session_scope() as session:
        items = await settings_store.get(session, key) or []
        if not isinstance(items, list):
            items = []
        if new_text in items:
            await event.bot.send_message(
                chat_id=cfg.admin_group_id,
                text="❌ Такая запись уже есть.",
                attachments=[kbds.op_settings_text_cancel_keyboard(key)],
            )
            return
        items.append(new_text)
        ok, msg = settings_store.validate(key, items)
        if not ok:
            await event.bot.send_message(
                chat_id=cfg.admin_group_id,
                text=f"❌ {msg}",
                attachments=[kbds.op_settings_text_cancel_keyboard(key)],
            )
            return
        await settings_store.set_value(session, key, items)
        await ops_svc.write_audit(
            session,
            operator_max_user_id=operator_id,
            action="setting_list_add",
            target=key,
            details={"added": new_text},
        )
    await _show_list_card(event, key)
