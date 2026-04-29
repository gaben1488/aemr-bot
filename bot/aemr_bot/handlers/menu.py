from maxapi import Dispatcher
from maxapi.types import MessageCallback

from aemr_bot import keyboards, texts
from aemr_bot.db.session import session_scope
from aemr_bot.services import appeals as appeals_service
from aemr_bot.services import card_format
from aemr_bot.services import settings_store
from aemr_bot.services import users as users_service


async def open_main_menu(event):
    await event.bot.send_message(
        chat_id=event.chat_id,
        text=texts.WELCOME,
        attachments=[keyboards.main_menu()],
    )


async def open_my_appeals(event, max_user_id: int):
    async with session_scope() as session:
        user = await users_service.get_or_create(session, max_user_id=max_user_id)
        appeals = await appeals_service.list_for_user(session, user.id, limit=20)
    if not appeals:
        await event.bot.send_message(
            chat_id=event.chat_id,
            text=texts.APPEAL_LIST_EMPTY,
            attachments=[keyboards.back_to_menu_keyboard()],
        )
        return
    items = [(a.id, card_format.appeal_list_label(a)) for a in appeals]
    await event.bot.send_message(
        chat_id=event.chat_id,
        text="Ваши обращения:",
        attachments=[keyboards.my_appeals_list_keyboard(items)],
    )


async def show_appeal(event, appeal_id: int, max_user_id: int):
    async with session_scope() as session:
        appeal = await appeals_service.get_by_id(session, appeal_id)
        if not appeal or not appeal.user or appeal.user.max_user_id != max_user_id:
            await event.bot.send_message(chat_id=event.chat_id, text="Обращение не найдено.")
            return
        text = card_format.user_card(appeal)
    await event.bot.send_message(
        chat_id=event.chat_id,
        text=text,
        attachments=[keyboards.back_to_menu_keyboard()],
    )


async def open_contacts(event):
    async with session_scope() as session:
        recep = await settings_store.get(session, "electronic_reception_url")
        udth = await settings_store.get(session, "udth_schedule_url")
    await event.bot.send_message(
        chat_id=event.chat_id,
        text=texts.CONTACTS_MENU_TITLE,
        attachments=[keyboards.contacts_menu_keyboard(recep, udth)],
    )


async def open_appointment(event):
    async with session_scope() as session:
        text = await settings_store.get(session, "appointment_text")
    await event.bot.send_message(
        chat_id=event.chat_id,
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
        chat_id=event.chat_id,
        text=body,
        attachments=[keyboards.back_to_menu_keyboard()],
    )


def register(dp: Dispatcher) -> None:
    @dp.message_callback()
    async def on_callback(event: MessageCallback):
        payload = (event.callback.payload or "") if hasattr(event, "callback") else ""
        max_user_id = getattr(event.user, "user_id", None) if getattr(event, "user", None) else None

        if payload == "menu:main":
            await event.answer_on_callback(notification="")
            await open_main_menu(event)
            return

        if payload == "menu:my_appeals":
            if max_user_id is None:
                return
            await event.answer_on_callback(notification="")
            await open_my_appeals(event, max_user_id)
            return

        if payload == "menu:contacts":
            await event.answer_on_callback(notification="")
            await open_contacts(event)
            return

        if payload == "contacts:appointment":
            await event.answer_on_callback(notification="")
            await open_appointment(event)
            return

        if payload == "contacts:emergency":
            await event.answer_on_callback(notification="")
            await open_emergency(event)
            return

        if payload.startswith("appeal:show:") and max_user_id is not None:
            try:
                appeal_id = int(payload.split(":")[2])
            except (IndexError, ValueError):
                return
            await event.answer_on_callback(notification="")
            await show_appeal(event, appeal_id, max_user_id)
            return
