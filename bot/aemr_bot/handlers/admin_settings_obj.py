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
from aemr_bot.db.session import session_scope
from aemr_bot.handlers._common import op_screen, op_send

# intent на добавление объекта ставится здесь (`_start_obj_add`),
# поэтому нужен `_intent_set` из общего модуля. `_edit_intents` /
# `_intent_get` реэкспортируются для тестов `TestStartObjAdd`, которые
# патчат/читают intent-кэш «по месту» на этом подмодуле. Общий парсер
# callback-суффикса читает «<key>:<idx>:<revision>» (просмотр/удаление).
from aemr_bot.handlers.admin_settings_shared import (  # noqa: F401
    _edit_intents,
    _intent_get,
    _intent_set,
    _parse_key_idx_revision,
)
from aemr_bot.services import operators as ops_svc
from aemr_bot.services import settings_store


async def _show_obj_card(event, key: str, *, notice: str = "", page: int = 0) -> None:

    async with session_scope() as session:
        items, version = await settings_store.get_versioned(session, key)
        items = items or []
    if not isinstance(items, list):
        items = []
    title_map = {
        "emergency_contacts": "🆘 Экстренные службы",
        "transport_dispatcher_contacts": "🚌 Диспетчерские транспорта",
    }
    title = title_map.get(key, key)
    # Pure-функция в services/settings_store — там же и юнит-тесты
    # без зависимости от maxapi.
    page = max(0, min(page, max(0, (len(items) - 1) // 8)))
    body = "\n".join(
        f"{i + 1}. {item.get('name') or item.get('routes') or 'Без названия'}"
        f" — {item.get('phone') or 'телефон не указан'}"
        + (f" · {item['section']}" if item.get("section") else "")
        for i, item in enumerate(items[page * 8:page * 8 + 8], page * 8)
    ) or "(список пуст)"
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
    await op_screen(
        event,
        (f"{notice}\n\n" if notice else "")
        + f"{title} ({len(items)}) · страница {page + 1} из {max(1, (len(items) + 7) // 8)}\n"
        f"· · · · · · · ·\n"
        f"{body}"
        f"{hint}",
        kbds.op_settings_obj_keyboard(key, items, version, page),
    )


async def _show_obj_item(event, suffix: str) -> None:

    parsed = _parse_key_idx_revision(suffix)
    if parsed is None:
        return
    key, idx, revision = parsed
    async with session_scope() as session:
        items, version = await settings_store.get_versioned(session, key)
        items = items or []
    if not isinstance(items, list):
        items = []
    if revision != f"v{version}":
        await _show_obj_card(
            event, key, notice="Список изменён другим оператором. Выберите запись заново.",
        )
        return
    if idx < 0 or idx >= len(items):
        await op_screen(
            event,
            "Запись не найдена.",
            kbds.op_back_to_settings_keyboard(),
        )
        return
    item = items[idx]
    labels = {"name": "Название", "routes": "Маршруты", "phone": "Телефон", "section": "Раздел"}
    lines = [f"{labels.get(k, k)}: {v}" for k, v in item.items()]
    await op_screen(
        event,
        "\n".join(lines),
        kbds.op_settings_obj_item_keyboard(key, idx, revision),
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
    await op_screen(
        event,
        f"➕ Добавление в «{key}»\n"
        f"· · · · · · · ·\n"
        f"{hint}",
        kbds.op_settings_text_cancel_keyboard(key),
    )


async def _obj_delete(event, operator_id: int, suffix: str) -> None:

    parsed = _parse_key_idx_revision(suffix)
    if parsed is None:
        return
    key, idx, revision = parsed
    try:
        async with session_scope() as session:
            previous, version = await settings_store.get_versioned(session, key)
            items = list(previous) if isinstance(previous, list) else []
            if revision != f"v{version}":
                raise settings_store.SettingConflictError(key)
            if idx < 0 or idx >= len(items):
                await op_screen(
                    event,
                    "Запись не найдена.",
                    kbds.op_back_to_settings_keyboard(),
                )
                return
            removed = items.pop(idx)
            ok, msg = settings_store.validate(key, items)
            if not ok:
                await op_screen(
                    event,
                    f"Удаление отменено: {msg}",
                    kbds.op_back_to_settings_keyboard(),
                )
                return
            await settings_store.set_value(session, key, items, expected_revision=version)
            await ops_svc.write_audit(
                session,
                operator_max_user_id=operator_id,
                action="setting_obj_del",
                target=key,
                details={"removed": removed, "index": idx},
            )
    except settings_store.SettingConflictError:
        await _show_obj_card(
            event, key, notice="Список изменён другим оператором. Выберите запись заново.",
        )
        return
    await _show_obj_card(event, key)


async def _apply_obj_add(
    event, operator_id: int, key: str, new_text: str
) -> bool:
    """Распарсить строки ввода в dict-item и добавить в список объектов.

    Возвращает True, если запись добавлена; False — если ввод отклонён
    (меньше двух строк / ключ не поддерживает формат / нарушение
    validate, включая формат телефона) и показана ошибка с cancel-
    клавиатурой. На False перехватчик `handle_settings_edit_text`
    сохраняет intent, чтобы оператор мог прислать корректные строки
    следующим сообщением.
    """

    lines = [ln.strip() for ln in new_text.split("\n") if ln.strip()]
    if len(lines) < 2:
        await op_send(
            event,
            "❌ Нужно две строки (название/маршруты и телефон).",
            kbds.op_settings_text_cancel_keyboard(key),
        )
        return False
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
        await op_send(
            event,
            f"❌ Ключ «{key}» не поддерживает добавление через две строки.",
            kbds.op_settings_text_cancel_keyboard(key),
        )
        return False
    try:
        async with session_scope() as session:
            previous, version = await settings_store.get_versioned(session, key)
            items = list(previous) if isinstance(previous, list) else []
            items.append(item)
            ok, msg = settings_store.validate(key, items)
            if not ok:
                await op_send(
                    event, f"❌ {msg}", kbds.op_settings_text_cancel_keyboard(key)
                )
                return False
            await settings_store.set_value(session, key, items, expected_revision=version)
            await ops_svc.write_audit(
                session,
                operator_max_user_id=operator_id,
                action="setting_obj_add",
                target=key,
                details={"added": item},
            )
    except settings_store.SettingConflictError:
        await op_send(
            event,
            "❌ Список изменён другим оператором. Пришлите запись ещё раз.",
            kbds.op_settings_text_cancel_keyboard(key),
        )
        return False
    await _show_obj_card(event, key)
    return True
