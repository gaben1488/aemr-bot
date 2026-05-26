"""Тонкий helper для typing-indicator в долгих операциях.

**Зачем.** maxapi 1.1.0 поддерживает `bot.send_action(chat_id,
SenderAction.TYPING_ON)` — мигающие точки «бот печатает», как в
Telegram. Жителю/оператору сразу понятно: «нажатие услышали, идёт
работа», особенно когда дальше будет несколько сообщений
(listing открытых обращений, broadcast confirm с превью).

Без индикатора оператор тапает «📂 Открытые обращения» и ждёт 1-2
секунды без обратной связи — кажется, что бот завис. С индикатором
точки появляются мгновенно, listing подъезжает следом.

**Контракт `mark_typing(event_or_bot, chat_id)`:**

- На вход: либо event (берём `event.bot` и `chat_id` через `get_ids`),
  либо явный `(bot, chat_id)`.
- Best-effort: любой Exception от MAX API НЕ должен ломать handler.
  `send_action` — UX-улучшение, не критичная часть flow. Логируем на
  DEBUG и продолжаем.
- TYPING_ON автоматически гасится MAX'ом через ~5 секунд или при
  следующем сообщении бота — explicit OFF не нужен.

**Где применять (выявлено CARDS_UX_SWEEP 2026-05-26):**

1. `admin_panel._do_open_tickets` — listing открытых обращений требует
   query + transform → 1-2 сек.
2. `menu.do_my_appeals` — listing обращений жителя.
3. `broadcast._handle_confirm` — перед запуском send-loop рассылки.
4. Любые другие точки, где handler делает >500 мс работы перед
   первым send_message.

**Где НЕ применять:**
- Простые callbacks с одним send_message <100 мс — typing на 5 секунд
  выглядит дольше реакции, UX хуже.
- Cron / pulse — там нет «оператор ждёт реакцию».
- На карточке обращения (force_new) — она сама и есть фидбек.
"""
from __future__ import annotations

import logging

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
            from aemr_bot.utils.event import get_chat_id
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
