"""Адаптер поверх объектов событий maxapi.

Сверено с исходниками love-apples/maxapi (maxapi/types/updates/*):
* MessageCreated имеет event.message (Message); event.get_ids() -> (chat_id, user_id).
* MessageCallback имеет event.callback (с .user, .payload, .callback_id) и
  опциональный event.message; event.get_ids() возвращает chat_id из
  message.recipient, если есть message, иначе None.
* BotStarted имеет event.chat_id и event.user напрямую; event.get_ids() работает.

Когда нужны оба id, всегда предпочитайте event.get_ids(); вспомогательные
функции ниже именно так и делают.
"""

import logging
from typing import Any

log = logging.getLogger(__name__)


def get_ids(event: Any) -> tuple[int | None, int | None]:
    """Вернуть (chat_id, user_id) независимо от типа события."""
    fn = getattr(event, "get_ids", None)
    if callable(fn):
        try:
            chat_id, user_id = fn()
            return chat_id, user_id
        except Exception:
            log.debug("event.get_ids() вернул ошибку, используем fallback", exc_info=True)

    # Запасные варианты для неизвестных форм события
    chat_id = getattr(event, "chat_id", None)
    user_id = None

    msg = getattr(event, "message", None)
    if msg is not None:
        recipient = getattr(msg, "recipient", None)
        if recipient is not None and chat_id is None:
            chat_id = getattr(recipient, "chat_id", None)
        sender = getattr(msg, "sender", None)
        if sender is not None and user_id is None:
            user_id = getattr(sender, "user_id", None)

    cb = getattr(event, "callback", None)
    if cb is not None and user_id is None:
        cb_user = getattr(cb, "user", None)
        if cb_user is not None:
            user_id = getattr(cb_user, "user_id", None)

    user = getattr(event, "user", None)
    if user is not None and user_id is None:
        user_id = getattr(user, "user_id", None)

    return chat_id, user_id


def get_chat_id(event: Any) -> int | None:
    return get_ids(event)[0]


def is_admin_chat(event: Any) -> bool:
    """True если событие пришло из админ-группы, заданной ADMIN_GROUP_ID.

    Используется handler'ами на двух осях: (а) фильтр citizen-flow команд
    (start.py запрещает /start, /menu и т.п. в админ-группе), (б) фильтр
    операторских команд (admin_commands.py / broadcast.py разрешают /reply,
    /broadcast и т.д. только в админ-группе). Канонический источник —
    единственный, чтобы случайное расхождение в условии не сломало одну из
    проверок незаметно.
    """
    # Локальный импорт ради избежания circular dep utils.event ↔ config.
    from aemr_bot.config import settings

    return settings.admin_group_id is not None and get_chat_id(event) == settings.admin_group_id


def get_user_id(event: Any) -> int | None:
    return get_ids(event)[1]


def get_first_name(event: Any) -> str | None:
    user = getattr(event, "user", None)
    if user is not None:
        name = getattr(user, "first_name", None)
        if name:
            return name
    cb = getattr(event, "callback", None)
    if cb is not None:
        cb_user = getattr(cb, "user", None)
        if cb_user is not None:
            name = getattr(cb_user, "first_name", None)
            if name:
                return name
    msg = getattr(event, "message", None)
    if msg is not None:
        sender = getattr(msg, "sender", None)
        if sender is not None:
            return getattr(sender, "first_name", None)
    return None


def get_payload(event: Any) -> str:
    cb = getattr(event, "callback", None)
    if cb is not None:
        p = getattr(cb, "payload", None)
        if p:
            return p
    p = getattr(event, "payload", None)
    return p or ""


def get_message_text(event: Any) -> str:
    msg = getattr(event, "message", None)
    if msg is None:
        return ""
    body = getattr(msg, "body", None)
    if body is not None:
        text = getattr(body, "text", None)
        if text is not None:
            return text
    return ""


def get_message_link(event: Any):
    msg = getattr(event, "message", None)
    if msg is None:
        return None
    return getattr(msg, "link", None)


def get_message_body(event: Any):
    """Вернуть MessageBody (с .text/.attachments/.link) либо None."""
    msg = getattr(event, "message", None)
    if msg is None:
        return None
    return getattr(msg, "body", None)


def extract_message_id(sent: Any) -> str | None:
    """Вытащить message_id из чего угодно, что может вернуть maxapi.

    Подтверждённые формы (в порядке вероятности для текущего maxapi):
    * `SendedMessage` из bot.send_message — имеет `.message: Message`,
      где `Message.body: MessageBody` и `MessageBody.mid: str`.
    * Голый `Message` — имеет `.body.mid` напрямую.
    * `EditedMessage` и прочие обёртки — повторяют форму SendedMessage.

    Также оставляем запасные пути через `.message_id` и прямой `.body.mid`,
    чтобы старые версии maxapi тоже разбирались корректно.
    """
    if sent is None:
        return None

    # Обёртка SendedMessage / EditedMessage.
    inner = getattr(sent, "message", None)
    if inner is not None:
        body = getattr(inner, "body", None)
        if body is not None:
            mid = getattr(body, "mid", None)
            if mid is not None:
                return str(mid)

    # Голый Message.
    body = getattr(sent, "body", None)
    if body is not None:
        mid = getattr(body, "mid", None)
        if mid is not None:
            return str(mid)

    # Унаследованный путь: объект с `.message_id` напрямую.
    mid = getattr(sent, "message_id", None)
    if mid is not None:
        return str(mid)

    return None


def get_callback_message_id(event: Any) -> str | None:
    """mid сообщения, на котором нажали кнопку.

    MAX умеет редактировать это сообщение через edit_message. Командные
    сообщения и обычный текст callback не имеют — для них отправляем новый
    экран.
    """
    if getattr(event, "callback", None) is None:
        return None
    body = getattr(getattr(event, "message", None), "body", None)
    mid = getattr(body, "mid", None)
    return str(mid) if mid else None


async def send_or_edit_screen(
    event: Any,
    *,
    text: str,
    attachments: list | None = None,
    force_new_message: bool = False,
    chat_id: int | None = None,
    user_id: int | None = None,
):
    """Показать экран бота без лишнего шума в чате.

    Правило UX: нажатие кнопки меняет текущую карточку, а видимое
    сообщение человека (текст, гео, файл, фото) приводит к новому ответу
    бота ниже этого сообщения. Поэтому callback сначала пробуем
    редактировать, а команды/текстовые шаги отправляем новым сообщением.
    """
    bot = getattr(event, "bot", None)
    if bot is None:
        return None
    attachments = attachments or []
    mid = None if force_new_message else get_callback_message_id(event)
    if mid and hasattr(bot, "edit_message"):
        try:
            return await bot.edit_message(
                message_id=mid,
                text=text,
                attachments=attachments,
            )
        except Exception:
            log.info(
                "edit_message %s failed, fallback to send_message",
                mid,
                exc_info=False,
            )

    event_chat_id, event_user_id = get_ids(event)
    target_chat_id = chat_id if chat_id is not None else event_chat_id
    target_user_id = user_id if user_id is not None else event_user_id
    return await bot.send_message(
        chat_id=target_chat_id,
        user_id=None if target_chat_id is not None else target_user_id,
        text=text,
        attachments=attachments,
    )


async def send(event: Any, text: str, attachments: list | None = None):
    """Отправить сообщение в ответ на событие любого типа.

    Маршрутизирует через event.bot.send_message с тем id, который
    доступен. Предпочитает chat_id (работает и для групп, и для
    диалогов), при его отсутствии откатывается к user_id.
    """
    bot = getattr(event, "bot", None)
    if bot is None:
        return None
    chat_id, user_id = get_ids(event)
    return await bot.send_message(
        chat_id=chat_id,
        user_id=None if chat_id is not None else user_id,
        text=text,
        attachments=attachments or [],
    )


async def send_to(event: Any, *, chat_id: int | None = None, user_id: int | None = None,
                  text: str = "", attachments: list | None = None):
    """Отправить сообщение явно указанному адресату через event.bot."""
    bot = getattr(event, "bot", None)
    if bot is None:
        return None
    return await bot.send_message(
        chat_id=chat_id,
        user_id=user_id,
        text=text,
        attachments=attachments or [],
    )


async def ack_callback(event: Any, notification: str = "") -> None:
    """Подтвердить нажатие кнопки (callback).

    Для MessageCallback подтверждённый метод — event.ack(notification=...).
    """
    fn = getattr(event, "ack", None)
    if callable(fn):
        try:
            await fn(notification=notification or None)
        except Exception:
            log.debug("не удалось подтвердить callback", exc_info=True)


# Унаследованный псевдоним, оставлен ради ясности
async def reply(event: Any, text: str, attachments: list | None = None):
    return await send(event, text, attachments)
