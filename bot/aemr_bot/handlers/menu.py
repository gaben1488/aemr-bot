from maxapi import Dispatcher

from aemr_bot import keyboards, texts
from aemr_bot.db.session import session_scope
from aemr_bot.services import appeals as appeals_service
from aemr_bot.services import card_format
from aemr_bot.services import settings_store
from aemr_bot.services import users as users_service
from aemr_bot.utils.event import ack_callback, get_chat_id


async def open_main_menu(event):
    await event.bot.send_message(
        chat_id=get_chat_id(event),
        text=texts.WELCOME,
        attachments=[keyboards.main_menu()],
    )


async def open_my_appeals(event, max_user_id: int):
    async with session_scope() as session:
        user = await users_service.get_or_create(session, max_user_id=max_user_id)
        appeals = await appeals_service.list_for_user(session, user.id, limit=20)
    if not appeals:
        await event.bot.send_message(
            chat_id=get_chat_id(event),
            text=texts.APPEAL_LIST_EMPTY,
            attachments=[keyboards.back_to_menu_keyboard()],
        )
        return
    items = [(a.id, card_format.appeal_list_label(a)) for a in appeals]
    await event.bot.send_message(
        chat_id=get_chat_id(event),
        text="Ваши обращения:",
        attachments=[keyboards.my_appeals_list_keyboard(items)],
    )


async def show_appeal(event, appeal_id: int, max_user_id: int):
    async with session_scope() as session:
        appeal = await appeals_service.get_by_id(session, appeal_id)
        if not appeal or not appeal.user or appeal.user.max_user_id != max_user_id:
            await event.bot.send_message(chat_id=get_chat_id(event), text="Обращение не найдено.")
            return
        text = card_format.user_card(appeal)
    await event.bot.send_message(
        chat_id=get_chat_id(event),
        text=text,
        attachments=[keyboards.back_to_menu_keyboard()],
    )


async def open_contacts(event):
    async with session_scope() as session:
        recep = await settings_store.get(session, "electronic_reception_url")
        udth = await settings_store.get(session, "udth_schedule_url")
    await event.bot.send_message(
        chat_id=get_chat_id(event),
        text=texts.CONTACTS_MENU_TITLE,
        attachments=[keyboards.contacts_menu_keyboard(recep, udth)],
    )


async def open_appointment(event):
    async with session_scope() as session:
        text = await settings_store.get(session, "appointment_text")
    await event.bot.send_message(
        chat_id=get_chat_id(event),
        text=text or "Информация скоро появится.",
        attachments=[keyboards.back_to_menu_keyboard()],
    )


async def open_emergency(event):
    async with session_scope() as session:
        contacts = await settings_store.get(session, "emergency_contacts") or []
    if not contacts:
        body = "Список контактов скоро появится."
    else:
        lines = ["🚨 Экстренные службы:\n"]
        for item in contacts:
            name = item.get("name", "—")
            phone = item.get("phone", "—")
            lines.append(f"• {name}: {phone}")
        body = "\n".join(lines)
    await event.bot.send_message(
        chat_id=get_chat_id(event),
        text=body,
        attachments=[keyboards.back_to_menu_keyboard()],
    )


async def handle_callback(event, payload: str, max_user_id: int | None) -> bool:
    """Try to handle a menu/contacts/appeal-show callback. Return True if handled."""
    if payload == "menu:main":
        await ack_callback(event)
        await open_main_menu(event)
        return True

    if payload == "menu:my_appeals":
        if max_user_id is None:
            return True
        await ack_callback(event)
        await open_my_appeals(event, max_user_id)
        return True

    if payload == "menu:contacts":
        await ack_callback(event)
        await open_contacts(event)
        return True

    if payload == "contacts:appointment":
        await ack_callback(event)
        await open_appointment(event)
        return True

    if payload == "contacts:emergency":
        await ack_callback(event)
        await open_emergency(event)
        return True

    if payload.startswith("appeal:show:") and max_user_id is not None:
        try:
            appeal_id = int(payload.split(":")[2])
        except (IndexError, ValueError):
            return True
        await ack_callback(event)
        await show_appeal(event, appeal_id, max_user_id)
        return True

    return False


def register(dp: Dispatcher) -> None:
    """No-op: callback routing is owned by handlers/appeal.py."""
    return None
