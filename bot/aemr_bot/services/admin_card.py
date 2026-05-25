"""Admin appeal card с freshness-rule (унифицированное правило для
карточек с кнопками — меню и admin appeal).

**Унифицированное правило (Freshness)**:

Любая карточка с кнопками редактируется ТОЛЬКО если она физически
последнее сообщение бота в этом чате прямо сейчас. Если ниже неё
что-то появилось (другая карточка, событие, другое обращение) —
send new card.

Реализуется через общий `menu_tracker[admin_group_id]` — все
карточки (меню, wizard, admin appeal) пишут туда свой mid при
send/edit. Перед edit'ом сверяем `callback_mid` с tracker:
- callback_mid == tracker → это последнее сообщение → edit OK;
- callback_mid != tracker → старая карточка → send new.

Это **то же** правило, что у `send_or_edit_screen` для меню. Тут
оно применено к admin appeal card.

**Что НЕ карточка** (события — без кнопок, иммутабельные): ответ
оператора жителю, followup-уведомления, подписки/отписки/erase ack.
Идут через прямой `bot.send_message` без trackers.

**Контракт `Appeal`-полей:**

- `admin_message_id` — mid ПЕРВОЙ публикации карточки (finalize).
  Используется как reply-link при relay вложений жителя. Не меняется
  после finalize.
- `last_admin_card_mid` — mid последней опубликованной карточки
  этого обращения. Обновляется при send_new и при edit-с-new-mid
  (edit fail fallback).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy.orm.exc import DetachedInstanceError

from aemr_bot import keyboards
from aemr_bot.config import settings as cfg
from aemr_bot.db.session import session_scope
from aemr_bot.services import appeals as appeals_service
from aemr_bot.services import card_format
from aemr_bot.utils import menu_tracker
from aemr_bot.utils.event import extract_message_id

if TYPE_CHECKING:
    from aemr_bot.db.models import Appeal

log = logging.getLogger(__name__)


async def render(
    bot,
    appeal: "Appeal",
    *,
    callback_mid: str | None = None,
    is_first_publication: bool = False,
    force_new: bool = False,
    event_header: str | None = None,
) -> str | None:
    """Опубликовать/обновить admin appeal card с freshness-rule.

    Args:
        bot: maxapi Bot.
        appeal: Appeal с подгруженными user и messages.
        callback_mid: mid сообщения, на котором оператор тапнул кнопку
            (callback.message.mid). Если None — это не callback (например
            finalize, followup от жителя) → форсим send_new.
        is_first_publication: True только при finalize — обновляет
            admin_message_id (sacred ссылка для relay вложений).
        force_new: True для событий «появилось новое» (followup
            жителя): даже если карточка ещё последняя в чате,
            нужна новая запись внизу для маркера событий.
        event_header: опциональный маркер-заголовок над карточкой —
            например «📩 Новое дополнение по обращению #N от жителя».
            Нужен, когда карточка приходит как РЕАКЦИЯ на событие, а
            не просто как переотрисовка статуса: оператор сразу видит,
            что именно случилось, без поиска в timeline. Шапка отделена
            от карточки разделителем. Только для send_new веток; на
            edit-in-place не применяется (карточка остаётся «обычной»).

    Returns:
        mid опубликованной/отредактированной карточки, None при сбое.

    Логика freshness:
    1. force_new=True → send new.
    2. callback_mid задан И равен menu_tracker[admin_group_id] → edit
       (карточка последняя в чате).
    3. Иначе → send new (карточка не последняя, или это не callback).
    """
    if not cfg.admin_group_id:
        log.warning("admin_card.render: ADMIN_GROUP_ID не установлен")
        return None

    user = getattr(appeal, "user", None)
    if user is None:
        log.warning(
            "admin_card.render: appeal #%s без user, не могу отрендерить",
            appeal.id,
        )
        return None

    # text и attachment_count устойчиво к detached lazy-load.
    # Reliability-pass: сузили `except Exception` до конкретного набора
    # причин. card_format читает .messages / .events / .user.*; на
    # detached instance это AttributeError либо DetachedInstanceError
    # из sqlalchemy.orm. TypeError ловит случай None в форматтере
    # (например appeal.created_at=None после миграции). Любые другие
    # exception'ы (asyncpg DataError, OperationalError) — баг, пусть
    # всплывает в логе вызывающего, чтобы был виден стек.
    try:
        text = card_format.admin_card(appeal, user)
    except (AttributeError, TypeError, DetachedInstanceError):
        log.exception(
            "admin_card.render: card_format.admin_card failed for #%s",
            appeal.id,
        )
        text = f"Обращение #{appeal.id}\nЖитель: {user.first_name or '—'}"
    try:
        attachment_count = _count_attachments(appeal)
    except (AttributeError, TypeError, DetachedInstanceError):
        log.debug(
            "admin_card.render: attachment_count failed for #%s",
            appeal.id, exc_info=False,
        )
        attachment_count = 0

    kb = keyboards.appeal_admin_actions(
        appeal.id,
        appeal.status,
        is_it=True,
        user_blocked=bool(user.is_blocked),
        closed_due_to_revoke=bool(getattr(appeal, "closed_due_to_revoke", False)),
        attachment_count=attachment_count,
    )
    attachments = [kb]

    # Freshness check.
    last_in_chat = menu_tracker.get_last_menu_mid(cfg.admin_group_id)
    can_edit = (
        not force_new
        and callback_mid is not None
        and last_in_chat is not None
        and callback_mid == last_in_chat
    )

    # На edit event_header НЕ применяется: карточку правят in-place,
    # маркер «новое событие» теряет смысл — оператор сам только что
    # тапнул кнопку, контекст у него уже в голове.
    text_for_send = (
        f"{event_header}\n────────────────\n{text}"
        if event_header and not can_edit
        else text
    )

    if can_edit:
        try:
            await bot.edit_message(
                message_id=callback_mid,
                text=text,
                attachments=attachments,
            )
            # На edit мы НЕ обновляем last_admin_card_mid в БД и не
            # меняем menu_tracker (mid и так остался прежним).
            return callback_mid
        except Exception:
            log.info(
                "admin_card.render: edit_message %s failed for #%s — "
                "fallback to send_new",
                callback_mid, appeal.id, exc_info=False,
            )
            # На fail edit'а tracker может быть невалиден — очистим,
            # чтобы следующий callback тоже пошёл в send_new.
            menu_tracker.clear(cfg.admin_group_id)

    # Send new card.
    try:
        sent = await bot.send_message(
            chat_id=cfg.admin_group_id,
            text=text_for_send,
            attachments=attachments,
        )
    except Exception:
        log.exception(
            "admin_card.render: send_message failed for appeal #%s", appeal.id
        )
        return None

    new_mid = extract_message_id(sent)
    if new_mid:
        # Обновляем общий tracker — теперь это последнее бот-сообщение
        # в чате (до следующего render любой системы).
        menu_tracker.set_last_menu_mid(cfg.admin_group_id, new_mid)
        async with session_scope() as session:
            await appeals_service.set_last_admin_card_mid(
                session, appeal.id, new_mid
            )
            if is_first_publication:
                await appeals_service.set_admin_message_id(
                    session, appeal.id, new_mid
                )
    return new_mid


def _count_attachments(appeal: "Appeal") -> int:
    """Сколько вложений у обращения (исходные + дополнения жителя).
    Устойчиво к detached state (lazy-fail → fallback на scalar)."""
    try:
        from aemr_bot.services.admin_relay import _collect_all_user_attachments

        return len(_collect_all_user_attachments(appeal))
    except Exception:
        log.debug(
            "admin_card._count_attachments: lazy-load fail for appeal #%s",
            appeal.id, exc_info=False,
        )
        return len(getattr(appeal, "attachments", None) or [])
