"""Тонкий helper `mark_typing(event_or_bot, chat_id)` для индикатора
«бот печатает» (`maxapi.SenderAction.TYPING_ON`) перед длинными
операциями (>500 мс): listing обращений, broadcast confirm.

Best-effort: любой Exception от MAX API НЕ ломает handler — typing
лишь UX-улучшение, логируем DEBUG и продолжаем. MAX гасит TYPING_ON
сам через ~5 сек или при следующем сообщении — explicit OFF не нужен.

Контракт где применять / не применять и full мотивация: см.
`docs/_meta/_archive/CODE_DECISIONS_LOG.md §4`.
"""
from __future__ import annotations

import logging

from aemr_bot.utils.event import get_chat_id

log = logging.getLogger(__name__)


async def mark_typing(event_or_bot, chat_id: int | None = None) -> None:
    """Best-effort typing-indicator.

    Args:
        event_or_bot: либо MAX event (берём `.bot`), либо Bot instance.
        chat_id: если передан event — можно опустить (возьмём из
            `event.get_ids()`). Если передан bot — обязателен.
    """
    # Lazy-import — SenderAction нужен только тут, не платим за импорт
    # на старте бота.
    try:
        from maxapi.enums.sender_action import SenderAction
    except Exception:
        log.debug("mark_typing: maxapi.enums.sender_action unavailable")
        return

    bot = getattr(event_or_bot, "bot", event_or_bot)
    if bot is None:
        return

    if chat_id is None:
        # Попробуем достать из event.
        try:
            chat_id = get_chat_id(event_or_bot)
        except Exception:
            log.debug("mark_typing: не смог извлечь chat_id из event")
            return

    if not chat_id:
        return

    try:
        await bot.send_action(chat_id=chat_id, action=SenderAction.TYPING_ON)
    except Exception:
        # UX-улучшение, не критично — глушим ошибку MAX'а.
        log.debug(
            "mark_typing: send_action failed for chat_id=%s", chat_id,
            exc_info=False,
        )
