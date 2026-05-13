"""Единый рендер интерактивных карточек бота.

Правило UX:
- нажатие inline-кнопки (`MessageCallback`) редактирует карточку, на
  которой нажали кнопку;
- видимый ввод пользователя (`MessageCreated`: текст, контакт,
  геолокация, файл, фото и т.п.) порождает новую карточку ниже ввода;
- если MAX не дал отредактировать сообщение, безопасно падаем обратно
  на обычную отправку нового сообщения.

Служебные уведомления и журнал админ-группы могут продолжать
использовать `send_message` напрямую: это не пользовательские карточки,
а отдельные события.
"""
from __future__ import annotations

import logging
from typing import Any

from aemr_bot.utils.event import extract_message_id, get_chat_id, get_user_id

log = logging.getLogger(__name__)


def callback_message_id(event) -> str | None:
    """Вернуть mid сообщения, на котором нажата inline-кнопка.

    У команд и обычных сообщений callback отсутствует, поэтому там
    редактировать нечего и вызывающий код должен отправить новую карточку.
    """
    if getattr(event, "callback", None) is None:
        return None
    body = getattr(getattr(event, "message", None), "body", None)
    mid = getattr(body, "mid", None)
    return str(mid) if mid else None


async def send_or_edit_card(
    event,
    *,
    text: str,
    attachments: list | None = None,
    force_new_message: bool = False,
    format: Any | None = None,
) -> tuple[str | None, bool]:
    """Показать пользовательскую карточку по единому UX-правилу.

    Возвращает `(message_id, edited)`. `message_id` полезен для воронок,
    где следующий callback должен редактировать именно созданную карточку.
    """
    attachments = attachments or []
    mid = None if force_new_message else callback_message_id(event)

    if mid and hasattr(event.bot, "edit_message"):
        try:
            await event.bot.edit_message(
                message_id=mid,
                text=text,
                attachments=attachments,
                format=format,
            )
            return mid, True
        except Exception:
            log.info(
                "send_or_edit_card: edit_message %s failed, fallback to send",
                mid,
                exc_info=False,
            )

    chat_id = get_chat_id(event)
    user_id = None if chat_id is not None else get_user_id(event)
    sent = await event.bot.send_message(
        chat_id=chat_id,
        user_id=user_id,
        text=text,
        attachments=attachments,
        format=format,
    )
    return extract_message_id(sent), False
