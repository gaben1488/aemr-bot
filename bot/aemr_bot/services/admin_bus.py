"""Единая шина для отправки сообщений в admin chat.

`admin_bus.send` оборачивает `bot.send_message` + `note_event` атомарно,
чтобы tracker не отставал от физического состояния чата. Шина — только
для новых сообщений; edit идёт через freshness-aware пути
(`admin_card.render`, `send_or_edit_screen`).

Также: `note_incoming_admin_message(mid)` двигает tracker на mid
входящего сообщения оператора (handler'ом, не самой шиной).

Полная мотивация и контракт «что шина делает и не делает»: см.
`docs/_meta/_archive/CODE_DECISIONS_LOG.md §2`.
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
    """Monkey-patch `bot.send_message`: после успешной отправки в admin
    chat двигает `menu_tracker.note_event(admin_group_id, mid)`.

    Закрывает 62 прямых `bot.send_message(...)` сайта одним hook'ом
    вместо миграции всех через `admin_bus.send` (большая правка, риск
    регрессий). Идемпотентно — повторный install на тот же bot no-op
    (маркер `_aemr_admin_outgoing_tracker_installed`).

    Полная мотивация (жалоба владельца, рассуждения «hook vs миграция»),
    идемпотентность guard, отношения с `admin_card.render` и
    `admin_bus.send`: см. `docs/_meta/_archive/CODE_DECISIONS_LOG.md §3`.

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
