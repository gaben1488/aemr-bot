"""Adapter over maxapi event objects.

Verified against love-apples/maxapi sources (maxapi/types/updates/*):
* MessageCreated has event.message (Message); event.get_ids() -> (chat_id, user_id).
* MessageCallback has event.callback (with .user, .payload, .callback_id) and
  optional event.message; event.get_ids() returns chat_id from message.recipient
  if message is present, else None.
* BotStarted has event.chat_id and event.user directly; event.get_ids() works.

Always prefer event.get_ids() when both ids are needed; the helpers below do that.
"""

from typing import Any


def get_ids(event: Any) -> tuple[int | None, int | None]:
    """Return (chat_id, user_id) regardless of event type."""
    fn = getattr(event, "get_ids", None)
    if callable(fn):
        try:
            chat_id, user_id = fn()
            return chat_id, user_id
        except Exception:
            pass

    # Fallbacks for unknown event shapes
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
    """Return the MessageBody (with .text/.attachments/.link), or None."""
    msg = getattr(event, "message", None)
    if msg is None:
        return None
    return getattr(msg, "body", None)


def extract_message_id(sent: Any) -> str | None:
    """Pull a message_id from anything maxapi might hand back.

    Verified shapes (in order of likelihood given current maxapi):
    * `SendedMessage` from bot.send_message — has `.message: Message`,
      where `Message.body: MessageBody` and `MessageBody.mid: str`.
    * Bare `Message` — has `.body.mid` directly.
    * `EditedMessage` and other wrappers — usually mirror SendedMessage.

    We also keep the legacy `.message_id` / direct `.body.mid` fallbacks
    so older revisions of maxapi still parse correctly.
    """
    if sent is None:
        return None

    # SendedMessage / EditedMessage wrapper.
    inner = getattr(sent, "message", None)
    if inner is not None:
        body = getattr(inner, "body", None)
        if body is not None:
            mid = getattr(body, "mid", None)
            if mid is not None:
                return str(mid)

    # Bare Message.
    body = getattr(sent, "body", None)
    if body is not None:
        mid = getattr(body, "mid", None)
        if mid is not None:
            return str(mid)

    # Legacy: object with `.message_id` directly.
    mid = getattr(sent, "message_id", None)
    if mid is not None:
        return str(mid)

    return None


def get_message_attachments(event: Any) -> list:
    msg = getattr(event, "message", None)
    if msg is None:
        return []
    body = getattr(msg, "body", None)
    if body is None:
        return []
    return getattr(body, "attachments", None) or []


async def send(event: Any, text: str, attachments: list | None = None):
    """Send a message in response to any event type.

    Routes through event.bot.send_message with whichever id is available.
    Prefers chat_id (works for groups and dialogs), falls back to user_id.
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
    """Send a message to an explicit target via event.bot."""
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
    """Acknowledge a callback button press.

    For MessageCallback, the verified method is event.ack(notification=...).
    """
    fn = getattr(event, "ack", None)
    if callable(fn):
        try:
            await fn(notification=notification or None)
        except Exception:
            pass


# Legacy alias kept for clarity
async def reply(event: Any, text: str, attachments: list | None = None):
    return await send(event, text, attachments)
