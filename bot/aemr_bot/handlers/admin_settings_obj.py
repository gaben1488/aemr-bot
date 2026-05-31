"""Списки объектов настроек (emergency_contacts,
transport_dispatcher_contacts) — CRUD по dict-записям.

Выделено из god-объекта `admin_settings.py`. Связная ответственность:
карточка списка с key-specific подсказкой формата, карточка одной
записи, удаление по индексу, добавление через intent (парсинг 2–3
строк ввода в dict). Валидация (min_items, формат телефона) — в
`settings_store.validate`; изменения пишутся в audit_log.
"""
from __future__ import annotations

from aemr_bot import keyboards as kbds
from aemr_bot.config import settings as cfg
from aemr_bot.db.session import session_scope
from aemr_bot.services import operators as ops_svc
from aemr_bot.services import settings_store
from aemr_bot.utils.event import send_or_edit_screen

# intent на добавление объекта ставится здесь (`_start_obj_add`),
# поэтому нужен `_intent_set` из общего модуля. `_edit_intents` /
# `_intent_get` реэкспортируются для тестов `TestStartObjAdd`, которые
# патчат/читают intent-кэш «по месту» на этом подмодуле.
from aemr_bot.handlers.admin_settings_shared import (  # noqa: F401
    _edit_intents,
    _intent_get,
    _intent_set,
)


async def _show_obj_card(event, key: str) -> None:

    async with session_scope() as session:
        items = await settings_store.get(session, key) or []
    if not isinstance(items, list):
        items = []
    title_map = {
        "emergency_contacts": "🆘 Экстренные службы",
        "transport_dispatcher_contacts": "🚌 Диспетчерские транспорта",
    }
    title = title_map.get(key, key)
    # Pure-функция в services/settings_store — там же и юнит-тесты
    # без зависимости от maxapi.
    body = settings_store.format_obj_list(items)
    hint = ""
    if key == "emergency_contacts":
        hint = (
            "\n\nФормат добавления: пришлите две или три строки —\n"
            "название, телефон и (опционально) раздел.\n"
            "Пример с разделом:\n"
            "Пожарная служба\n"
            "01\n"
            "Экстренные службы"
        )
    elif key == "transport_dispatcher_contacts":
        hint = (
            "\n\nФормат добавления: пришлите две строки —\n"
            "маршруты и телефон.\n"
            "Пример:\n"
            "Автобусы 101, 102, 103\n"
            "+7 (415-31) 7-25-29"
        )
    await send_or_edit_screen(
        event, chat_id=cfg.admin_group_id,
        text=(
            f"{title} ({len(items)})\n"
            f"· · · · · · · ·\n"
            f"{body}"
            f"{hint}"
        ),
        attachments=[kbds.op_settings_obj_keyboard(key, items)],
    )


async def _show_obj_item(event, suffix: str) -> None:

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
    if idx < 0 or idx >= len(items):
        await send_or_edit_screen(
            event, chat_id=cfg.admin_group_id,
            text="Запись не найдена.",
            attachments=[kbds.op_back_to_settings_keyboard()],
        )
        return
    item = items[idx]
    lines = [f"{k}: {v}" for k, v in item.items()]
    await send_or_edit_screen(
        event, chat_id=cfg.admin_group_id,
        text="\n".join(lines),
        attachments=[kbds.op_settings_obj_item_keyboard(key, idx)],
    )


async def _start_obj_add(event, operator_id: int, key: str) -> None:

    _intent_set(operator_id, key=key, kind="obj_add")
    if key == "emergency_contacts":
        hint = (
            "Пришлите две или три строки:\n"
            "1) название\n"
            "2) телефон\n"
            "3) раздел (необязательно — Экстренные службы / Электроэнергия / ...)"
        )
    elif key == "transport_dispatcher_contacts":
        hint = "Пришлите две строки: маршруты и телефон."
    else:
        hint = "Пришлите данные двумя строками."
    await send_or_edit_screen(
        event, chat_id=cfg.admin_group_id,
        text=(
            f"➕ Добавление в «{key}»\n"
            f"· · · · · · · ·\n"
            f"{hint}"
        ),
        attachments=[kbds.op_settings_text_cancel_keyboard(key)],
    )


async def _obj_delete(event, operator_id: int, suffix: str) -> None:

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
                text="Запись не найдена.",
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
            action="setting_obj_del",
            target=key,
            details={"removed": removed, "index": idx},
        )
    await _show_obj_card(event, key)


async def _apply_obj_add(
    event, operator_id: int, key: str, new_text: str
) -> None:

    lines = [ln.strip() for ln in new_text.split("\n") if ln.strip()]
    if len(lines) < 2:
        await event.bot.send_message(
            chat_id=cfg.admin_group_id,
            text="❌ Нужно две строки (название/маршруты и телефон).",
            attachments=[kbds.op_settings_text_cancel_keyboard(key)],
        )
        return
    if key == "emergency_contacts":
        # Третья строка — необязательный раздел (Экстренные службы,
        # Электроэнергия, Отопление и т.п.). Если оператор её прислал,
        # сохраняем — UI потом сгруппирует контакты по разделам. Если
        # не прислал — item уходит в визуальную секцию «Прочее».
        item: dict[str, str] = {"name": lines[0], "phone": lines[1]}
        if len(lines) >= 3 and lines[2]:
            item["section"] = lines[2]
    elif key == "transport_dispatcher_contacts":
        item = {"routes": lines[0], "phone": lines[1]}
    else:
        await event.bot.send_message(
            chat_id=cfg.admin_group_id,
            text=f"❌ Ключ «{key}» не поддерживает добавление через две строки.",
            attachments=[kbds.op_settings_text_cancel_keyboard(key)],
        )
        return
    async with session_scope() as session:
        items = await settings_store.get(session, key) or []
        if not isinstance(items, list):
            items = []
        items.append(item)
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
            action="setting_obj_add",
            target=key,
            details={"added": item},
        )
    await _show_obj_card(event, key)
