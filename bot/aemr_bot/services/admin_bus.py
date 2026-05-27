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
        # 2026-05-27 dual-tracker: admin_bus.send используется для
        # **historic events** (pulse, audit-уведомления, retention,
        # operator confirmations, audit-notice). Это НЕ редактируемые
        # меню — клик кнопки на них не должен превращать их в меню.
        # Поэтому двигаем только `physical_mid` через `note_event`,
        # `editable_mid` остаётся на mid предыдущего меню (если было).
        # Следующий тап оператора по кнопке на этом events callback_mid
        # != editable_mid → can_edit=False → send_new. Event остаётся
        # в чате как историчная запись.
        menu_tracker.note_event(cfg.admin_group_id, mid)
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
    # 2026-05-27 dual-tracker: incoming op-message — двигает только
    # physical_mid. Editable_mid остаётся на mid предыдущего меню;
    # клик оператора по нему всё ещё может редактировать его, но
    # callback_mid != physical_mid (потому что op написал текст ниже)
    # → freshness откажет → send_new.
    menu_tracker.note_incoming(cfg.admin_group_id, mid)


# Маркер: к одному и тому же bot.send_message hook ставим только один
# раз. Без этого повторный install (например, в тестах) обернул бы send
# рекурсивно — каждое сообщение прошло бы tracker.set N раз. Маркер
# хранится на самом bot-объекте как атрибут.
_HOOK_INSTALLED_ATTR = "_aemr_admin_outgoing_tracker_installed"


def install_outgoing_tracker_hook(bot) -> None:
    """Обернуть `bot.send_message` декоратором, который синхронизирует
    `menu_tracker[admin_group_id]` после любой успешной отправки в admin chat.

    **Зачем.** Жалоба владельца 2026-05-27: «меню в админ-чате
    редактируется при тапе кнопки на не-последнем сообщении». Корень —
    62 прямых `bot.send_message(chat_id=admin_group_id, ...)` в коде
    (handlers + services), большинство из них не вызывают
    `menu_tracker.set_last_menu_mid` после send. Tracker отстаёт от
    физического состояния чата. Любой следующий тап на «карточку выше»
    callback_mid (старой карточки) == tracker → freshness ошибочно
    edit'ит вверху, ниже всё остаётся.

    Раньше единственное место с правильным sync — `admin_bus.send` —
    мигрировать все 62 sites через шину было бы 200+ строк правок в
    14 файлах, с риском регрессий. Этот hook решает проблему один раз
    на старте бота: оборачивает оригинальный `bot.send_message`, после
    каждого успешного `send_message(chat_id=admin_group_id, ...)`
    извлекает mid и двигает tracker.

    **Что делает hook:**
    1. Если `chat_id != admin_group_id` — пробрасывает вызов без
       изменений (citizen-chat tracker имеет свой sync через
       `_send_or_edit_menu`).
    2. Если `chat_id == admin_group_id` — выполняет оригинальный send,
       извлекает mid, обновляет tracker. Возвращает результат как был.
    3. Ошибки send_message не глотает — пробрасывает caller'у. Tracker
       обновляет только при успешном send.

    **Идемпотентность.** Повторный вызов на тот же bot — no-op (маркер
    на bot-объекте). Иначе hook оборачивал бы себя рекурсивно и каждое
    сообщение проходило бы N tracker.set.

    **Где НЕ применять:**
    - `admin_card.render` сам делает `menu_tracker.clear()` после
      send_new (sacred event log, карточка обращения не должна
      участвовать в tracker как меню). Hook сначала set'нет tracker
      на mid карточки — потом сразу clear перезатрёт. Финальное
      состояние = None, корректно.
    - `admin_bus.send` сам делает set_last_menu_mid после send. Hook
      повторит то же действие — это идемпотентно (tracker сидит на
      том же mid). Не дублируем явный set, оставляем для читаемости
      `admin_bus.send`.

    Args:
        bot: maxapi Bot, на котором будет установлен hook.
    """
    if not cfg.admin_group_id:
        log.info(
            "install_outgoing_tracker_hook: ADMIN_GROUP_ID не задан, "
            "hook не устанавливаем (для citizen-only-окружения OK)."
        )
        return
    if getattr(bot, _HOOK_INSTALLED_ATTR, False):
        log.debug(
            "install_outgoing_tracker_hook: hook уже установлен на этом "
            "bot, повторный вызов проигнорирован."
        )
        return

    original_send = bot.send_message

    async def _wrapped_send(*args, **kwargs):
        # Hook должен быть совместим с любой формой вызова — позиционной,
        # keyword, mixed. Реальные использования в коде — keyword only
        # (chat_id=..., text=..., attachments=...), но защита от
        # внезапных позиционных аргументов нужна, чтобы hook не отвалился
        # на нестандартном usage и не сломал send.
        result = await original_send(*args, **kwargs)
        try:
            target_chat_id = kwargs.get("chat_id")
            if (
                target_chat_id == cfg.admin_group_id
                and target_chat_id is not None
            ):
                mid = extract_message_id(result)
                if mid:
                    # 2026-05-27 dual-tracker: hook ловит ВСЕ исходящие
                    # в admin chat. Большинство — historic events (pulse,
                    # audit, broadcast progress, прямые sends в коде).
                    # Двигаем только physical_mid. Editable_mid должен
                    # обновляться явно через send_or_edit_screen или
                    # _send_or_edit_menu (которые сами знают, что они
                    # шлют редактируемый экран).
                    menu_tracker.note_event(cfg.admin_group_id, mid)
                    log.debug(
                        "outgoing-tracker-hook: admin chat send → "
                        "physical_mid = %s", mid,
                    )
        except Exception:
            # tracker-sync best-effort, никогда не ломает caller.
            log.debug(
                "install_outgoing_tracker_hook: post-send sync failed",
                exc_info=False,
            )
        return result

    bot.send_message = _wrapped_send  # type: ignore[assignment]
    setattr(bot, _HOOK_INSTALLED_ATTR, True)
    log.info(
        "install_outgoing_tracker_hook: установлен hook на bot.send_message "
        "для admin_group_id=%s — каждый исходящий в admin chat будет "
        "автоматически двигать menu_tracker.",
        cfg.admin_group_id,
    )
