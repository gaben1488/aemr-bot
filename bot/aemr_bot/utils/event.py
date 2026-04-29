"""Helpers that smooth over differences between MAX event types.

Some events (BotStarted, BotAdded) do not carry a Message object, so
event.message.answer() does not work for them. Use these helpers to
read user info and send replies regardless of the event variant.
"""

from typing import Any


def get_user_id(event: Any) -> int | None:
    """Return MAX user_id of the event author, or None."""
    user = getattr(event, "user", None)
    if user is not None:
        uid = getattr(user, "user_id", None)
        if uid is not None:
            return uid
    msg = getattr(event, "message", None)
    if msg is not None:
        sender = getattr(msg, "sender", None)
        if sender is not None:
            return getattr(sender, "user_id", None)
    return None


def get_first_name(event: Any) -> str | None:
    user = getattr(event, "user", None)
    if user is not None:
        name = getattr(user, "first_name", None)
        if name:
            return name
    msg = getattr(event, "message", None)
    if msg is not None:
        sender = getattr(msg, "sender", None)
        if sender is not None:
            return getattr(sender, "first_name", None)
    return None


async def reply(event: Any, text: str, attachments: list | None = None):
    """Send a reply that works whether the event is MessageCreated, MessageCallback, or BotStarted."""
    attachments = attachments or []
    msg = getattr(event, "message", None)
    if msg is not None and hasattr(msg, "answer"):
        return await msg.answer(text, attachments=attachments)
    chat_id = getattr(event, "chat_id", None)
    if chat_id is not None and getattr(event, "bot", None) is not None:
        return await event.bot.send_message(chat_id=chat_id, text=text, attachments=attachments)
    return None
