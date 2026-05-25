"""Единая шина для отправки сообщений в служебную группу (admin chat).

**Зачем существует.** Раньше десятки путей шли в admin chat напрямую через
`bot.send_message(chat_id=cfg.admin_group_id, ...)` — pulse, admin_events,
broadcast progress, operator_reply confirmations, retention notifications.
Каждое такое сообщение физически сдвигает чат вниз, но никто из этих
путей не обновлял `menu_tracker`. Tracker отставал от реального состояния
чата, и freshness-rule (`callback_mid == tracker → edit`) врал:
оператор тапал кнопку на старой карточке, бот edit'ал её на месте далеко
вверху чата, оператор внизу ничего не видел.

**Решение.** Любая отправка в admin chat теперь идёт через `admin_bus.send`.
Шина делает три действия атомарно:
1. `bot.send_message(chat_id=cfg.admin_group_id, ...)`
2. `extract_message_id(sent)` — достаёт mid из ответа MAX API.
3. `menu_tracker.set_last_menu_mid(cfg.admin_group_id, mid)` — двигает
   tracker на свежий mid. После этого любой следующий callback оператора
   на карточку выше будет иметь `callback_mid != tracker` → freshness
   корректно вернёт `can_edit=False` → send_new.

**Что НЕ делает шина:**
- Не интерпретирует attachments / семантику сообщения. Это тонкий
  wrapper, не бизнес-логика.
- Не делает retry / circuit-breaker. Это responsibility вызывающего
  (для broadcast есть `_send_with_retry`, для admin notifications —
  `_send_admin_text_with_retry` в `services/cron.py`).
- Не делает freshness-check на edit. Edit'ить через шину нельзя
  принципиально — карточки с кнопками идут через `admin_card.render`
  (freshness-aware), карточки меню — через `send_or_edit_screen`
  (тоже freshness-aware). Шина — для **новых** сообщений.

**Использование:**

```python
from aemr_bot.services import admin_bus

await admin_bus.send(bot, text="🟢 Pulse: бот живой")
await admin_bus.send(bot, text=text, attachments=[kb])
```

**Incoming admin-message hook.** Отдельная функция
`note_incoming_admin_message(mid)` — вызывается из handler'а на каждое
новое сообщение в admin chat (operator-text, voice, sticker). Она
сдвигает tracker на mid входящего сообщения. Это закрывает дыру
«оператор написал в чат, но tracker по-прежнему на карточке выше —
следующий тап freshness-mismatch не увидит».
"""
from __future__ import annotations

import logging

from aemr_bot.config import settings as cfg
from aemr_bot.utils import menu_tracker
from aemr_bot.utils.event import extract_message_id

log = logging.getLogger(__name__)


async def send(
    bot,
    *,
    text: str,
    attachments: list | None = None,
    link=None,
) -> str | None:
    """Отправить сообщение в admin chat + сдвинуть tracker.

    Возвращает mid отправленного сообщения, либо None если ADMIN_GROUP_ID
    не настроен / send упал.

    Args:
        bot: maxapi Bot.
        text: текст сообщения.
        attachments: опциональный список вложений (клавиатуры, image, etc).
        link: опциональный NewMessageLink (для reply-цитирования).
    """
    if not cfg.admin_group_id:
        log.warning("admin_bus.send: ADMIN_GROUP_ID не задан, пропускаем")
        return None
    kwargs: dict = {"chat_id": cfg.admin_group_id, "text": text}
    if attachments is not None:
        kwargs["attachments"] = attachments
    if link is not None:
        kwargs["link"] = link
    try:
        sent = await bot.send_message(**kwargs)
    except Exception:
        log.exception(
            "admin_bus.send: send_message failed для admin_group_id=%s",
            cfg.admin_group_id,
        )
        return None
    mid = extract_message_id(sent)
    if mid:
        # Атомарный sync: physical chat сдвинулся вниз, tracker должен
        # отразить это сразу. Иначе следующий callback оператора на
        # карточку выше получит freshness false-positive.
        menu_tracker.set_last_menu_mid(cfg.admin_group_id, mid)
    return mid


def note_incoming_admin_message(mid: str | None) -> None:
    """Зарегистрировать факт входящего сообщения в admin chat.

    Вызывается из dispatch hook на каждый MessageCreated в admin chat
    (operator-text, sticker, voice, поговорил в чате, ответ свайпом).
    После этого callback на карточки ВЫШЕ этого сообщения будут идти
    в send_new (freshness увидит mismatch).

    Если mid не извлёкся (None) — no-op, не падаем. Худшее, что может
    случиться при пропуске одного incoming-сообщения — следующий
    operator-callback edit'нет одну карточку на месте, что
    самокорректируется на следующем outgoing-сообщении бота.
    """
    if not cfg.admin_group_id or not mid:
        return
    menu_tracker.set_last_menu_mid(cfg.admin_group_id, mid)
