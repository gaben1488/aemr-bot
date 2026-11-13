"""Explicit confirmation of a proposed resident-facing text or URL."""
from secrets import token_hex

from maxapi.types import CallbackButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from aemr_bot.handlers import admin_settings_text as editor
from aemr_bot.handlers._common import op_screen
from aemr_bot.handlers.admin_settings_shared import _intent_drop, _intent_get, _intent_set
from aemr_bot.utils.text_pages import split_text_units


async def prepare(event, operator_id: int, key: str, value: str) -> None:
    intent = _intent_get(operator_id)
    if not intent or intent.get("kind") != "single" or intent.get("key") != key:
        return
    if intent.get("preview_busy"):
        await op_screen(event, "Сохранение уже выполняется. Дождитесь результата.")
        return
    valid, reason = editor.settings_store.validate(key, value)
    if not valid:
        await op_screen(event, f"❌ {reason}. Прежнее значение не изменено.")
        return
    if intent.get("revision") is None or "baseline" not in intent:
        await op_screen(event, "Редактирование устарело. Откройте настройку заново.")
        return
    _intent_set(operator_id, **{**intent, "proposed_text": value, "preview_token": token_hex(8)})
    await show(event, operator_id)


async def show(event, operator_id: int, page: int = 0) -> None:
    intent = _intent_get(operator_id)
    if not intent or "proposed_text" not in intent:
        return
    pages = split_text_units(intent["proposed_text"], 2400)
    page = min(max(page, 0), len(pages) - 1)
    token = intent["preview_token"]
    kb = InlineKeyboardBuilder()
    def button(text, action):
        return CallbackButton(text=text, payload=f"op:set:textpreview:{action}:{token}")
    nav = []
    if page:
        nav.append(button("◀️", f"page{page - 1}"))
    if page + 1 < len(pages):
        nav.append(button("▶️", f"page{page + 1}"))
    if nav:
        kb.row(*nav)
    kb.row(button("✅ Сохранить", "confirm"), button("❌ Отменить", "cancel"))
    await op_screen(event,
        f"Предпросмотр новой редакции · {editor._TITLE_MAP.get(intent['key'], intent['key'])}\n"
        f"Часть {page + 1}/{len(pages)}\n\n{pages[page]}\n\n"
        "Пока не сохранено. После подтверждения новое значение будет применяться ботом; "
        "ранее принятые редакции согласия не меняются. Можно прислать исправленный вариант.",
        kb.as_markup())


async def callback(event, operator_id: int, suffix: str) -> None:
    bits = suffix.split(":")
    if len(bits) != 2:
        return
    action, token = bits
    intent = _intent_get(operator_id)
    if (not intent or intent.get("kind") != "single" or "proposed_text" not in intent
            or intent.get("preview_token") != token or intent.get("preview_busy")):
        await op_screen(event, "Кнопка устарела. Текущая настройка и новый черновик не изменены.")
        return
    if action.startswith("page") and action[4:].isdigit():
        await show(event, operator_id, int(action[4:]))
    elif action == "cancel":
        _intent_drop(operator_id)
        await op_screen(event, "Изменение отменено. Прежнее значение сохранено.")
    elif action == "confirm":
        # Consume this confirmation before yielding; another click cannot
        # save twice. The existing writer retains its revision and audit.
        intent["preview_busy"] = True
        try:
            done = await editor._apply_single_edit(
                event, operator_id, intent["key"], intent["proposed_text"], edit_intent=intent)
        finally:
            intent.pop("preview_busy", None)
        if done and _intent_get(operator_id) is intent:
            _intent_drop(operator_id)
