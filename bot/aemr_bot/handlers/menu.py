from maxapi import Dispatcher

from aemr_bot import keyboards, texts
from aemr_bot.db.session import session_scope
from aemr_bot.services import appeals as appeals_service
from aemr_bot.services import broadcasts as broadcasts_service
from aemr_bot.services import card_format
from aemr_bot.services import settings_store
from aemr_bot.services import users as users_service
from aemr_bot.utils.event import ack_callback, get_chat_id, get_user_id


async def open_main_menu(event):
    async with session_scope() as session:
        recep_url = await settings_store.get(session, "electronic_reception_url")
    await event.bot.send_message(
        chat_id=get_chat_id(event),
        text=texts.WELCOME,
        attachments=[keyboards.main_menu(recep_url)],
    )


MY_APPEALS_PAGE_SIZE = 5


async def open_my_appeals(event, max_user_id: int, page: int = 1):
    page = max(1, page)
    async with session_scope() as session:
        user = await users_service.get_or_create(session, max_user_id=max_user_id)
        total = await appeals_service.count_for_user(session, user.id)
        if total == 0:
            await event.bot.send_message(
                chat_id=get_chat_id(event),
                text=texts.APPEAL_LIST_EMPTY,
                attachments=[keyboards.back_to_menu_keyboard()],
            )
            return
        total_pages = max(1, (total + MY_APPEALS_PAGE_SIZE - 1) // MY_APPEALS_PAGE_SIZE)
        page = min(page, total_pages)
        offset = (page - 1) * MY_APPEALS_PAGE_SIZE
        appeals = await appeals_service.list_for_user(
            session, user.id, limit=MY_APPEALS_PAGE_SIZE, offset=offset
        )
    items = [(a.id, card_format.appeal_list_label(a)) for a in appeals]
    header = (
        f"Ваши обращения (стр. {page}/{total_pages}, всего {total}):"
        if total_pages > 1
        else f"Ваши обращения (всего {total}):"
    )
    await event.bot.send_message(
        chat_id=get_chat_id(event),
        text=header,
        attachments=[
            keyboards.my_appeals_list_keyboard(items, page=page, total_pages=total_pages)
        ],
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


async def open_useful_info(event):
    max_user_id = get_user_id(event)
    async with session_scope() as session:
        udth = await settings_store.get(session, "udth_schedule_url")
        udth_inter = await settings_store.get(session, "udth_schedule_intermunicipal_url")
        subscribed = (
            await broadcasts_service.is_subscribed(session, max_user_id)
            if max_user_id is not None
            else False
        )
    label = (
        texts.SUBSCRIBE_BUTTON_OFF if subscribed else texts.SUBSCRIBE_BUTTON_ON
    )
    await event.bot.send_message(
        chat_id=get_chat_id(event),
        text=texts.USEFUL_INFO_TITLE,
        attachments=[
            keyboards.useful_info_keyboard(udth, udth_inter, subscribe_label=label)
        ],
    )


async def toggle_subscription(event, max_user_id: int) -> None:
    async with session_scope() as session:
        await users_service.get_or_create(session, max_user_id=max_user_id)
        currently = await broadcasts_service.is_subscribed(session, max_user_id)
        await broadcasts_service.set_subscription(session, max_user_id, not currently)
    confirmation = (
        texts.UNSUBSCRIBE_CONFIRMED if currently else texts.SUBSCRIBE_CONFIRMED
    )
    await event.bot.send_message(chat_id=get_chat_id(event), text=confirmation)


async def handle_broadcast_unsubscribe(event, max_user_id: int) -> None:
    """Отписка в одно нажатие через кнопку под каждым сообщением рассылки."""
    async with session_scope() as session:
        already = await broadcasts_service.is_subscribed(session, max_user_id)
        if already:
            await broadcasts_service.set_subscription(session, max_user_id, False)
    await event.bot.send_message(
        chat_id=get_chat_id(event), text=texts.UNSUBSCRIBE_CONFIRMED
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
        # Группируем по полю 'section', если оно есть; элементы без секции
        # попадают в «Прочее», чтобы старые seed-данные без секций
        # продолжали отображаться.
        grouped: dict[str, list[dict]] = {}
        order: list[str] = []
        for item in contacts:
            section = item.get("section") or "Прочее"
            if section not in grouped:
                grouped[section] = []
                order.append(section)
            grouped[section].append(item)
        blocks: list[str] = ["☎️ Телефоны экстренных и аварийных служб"]
        for section in order:
            blocks.append(f"\n{section}:")
            for item in grouped[section]:
                name = item.get("name", "—")
                phone = item.get("phone", "—")
                blocks.append(f"• {name} — {phone}")
        body = "\n".join(blocks)
    await event.bot.send_message(
        chat_id=get_chat_id(event),
        text=body,
        attachments=[keyboards.back_to_menu_keyboard()],
    )


async def open_dispatchers(event):
    async with session_scope() as session:
        items = await settings_store.get(session, "transport_dispatcher_contacts") or []
    if not items:
        body = "Список диспетчерских скоро появится."
    else:
        body = "📞 Диспетчерские автотранспорта:\n\n" + "\n\n".join(
            f"• {item.get('routes', '—')}\n  {item.get('phone', '—')}"
            for item in items
        )
    await event.bot.send_message(
        chat_id=get_chat_id(event),
        text=body,
        attachments=[keyboards.back_to_menu_keyboard()],
    )


async def handle_callback(event, payload: str, max_user_id: int | None) -> bool:
    """Пробует обработать нажатие меню, контактов или показа обращения. Возвращает True, если обработано."""
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

    if payload.startswith("appeals:page:") and max_user_id is not None:
        suffix = payload.split(":", 2)[2]
        if suffix == "noop":
            await ack_callback(event)
            return True
        try:
            page = int(suffix)
        except ValueError:
            return True
        await ack_callback(event)
        await open_my_appeals(event, max_user_id, page=page)
        return True

    if payload == "menu:useful_info":
        await ack_callback(event)
        await open_useful_info(event)
        return True

    if payload == "menu:appointment":
        await ack_callback(event)
        await open_appointment(event)
        return True

    if payload == "info:emergency":
        await ack_callback(event)
        await open_emergency(event)
        return True

    if payload == "info:dispatchers":
        await ack_callback(event)
        await open_dispatchers(event)
        return True

    if payload == "info:subscribe_toggle" and max_user_id is not None:
        await ack_callback(event)
        await toggle_subscription(event, max_user_id)
        return True

    if payload == "broadcast:unsubscribe" and max_user_id is not None:
        await ack_callback(event)
        await handle_broadcast_unsubscribe(event, max_user_id)
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
    """Заглушка: маршрутизацией нажатий владеет handlers/appeal.py."""
    return None
