"""Event-log публикация admin appeal card.

**Mental model (DDD-pivot 2026-05-25):**

Карточка обращения в админ-чате — это **запись о событии**, не
живое окно. Каждое изменение состояния (finalize / followup жителя
/ reply оператора / reopen / close / block / unblock / erase) =
**новая** карточка снизу чата с актуальным timeline'ом. Старые
карточки выше остаются как audit-trail — никогда не редактируются.

**Контракт:**

`Appeal.admin_message_id` = mid ПЕРВОЙ карточки (finalize). Не
редактируется, не двигается. Используется только как reply-link при
relay вложений.

`Appeal.last_admin_card_mid` = mid ПОСЛЕДНЕЙ event-карточки.
Обновляется каждый render. Используется для:

- **stale-detection**: callback.message.mid != last_admin_card_mid →
  карточка устарела (оператор тапнул на старой вверху чата), ack
  «устарела» + render новой внизу.
- **точка свайп-reply**: оператор отвечает свайпом на актуальную.

**Mental model для оператора**: каждая карточка показывает «вот что
произошло на момент времени T». Кнопки на старых карточках устарели
— система не даст случайно сделать действие на старом контексте, и
актуальная карточка всегда внизу.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aemr_bot import keyboards
from aemr_bot.config import settings as cfg
from aemr_bot.db.session import session_scope
from aemr_bot.services import appeals as appeals_service
from aemr_bot.services import card_format
from aemr_bot.utils.event import extract_message_id

if TYPE_CHECKING:
    from aemr_bot.db.models import Appeal

log = logging.getLogger(__name__)


async def render(
    bot,
    appeal: "Appeal",
    *,
    is_first_publication: bool = False,
) -> str | None:
    """Опубликовать event-карточку обращения в админ-чат.

    Args:
        bot: maxapi Bot.
        appeal: Appeal с подгруженными user и messages (для timeline
            и attachment_count). Передавайте через
            `appeals_service.get_by_id_with_messages`.
        is_first_publication: True только при finalize обращения —
            тогда обновляем admin_message_id (нужно для reply-link
            при relay вложений). На всех остальных event'ах
            admin_message_id НЕ двигается.

    Returns:
        mid отправленной карточки, или None при сбое.

    Эту функцию НЕЛЬЗЯ использовать для edit. Карточки иммутабельны.
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

    # text и attachment_count считаем устойчиво (detached lazy-load
    # может бросить MissingGreenlet). Главное — карточка дойдёт.
    try:
        text = card_format.admin_card(appeal, user)
    except Exception:
        log.exception(
            "admin_card.render: card_format.admin_card failed for #%s, "
            "fallback на минимальный текст",
            appeal.id,
        )
        text = f"Обращение #{appeal.id}\nЖитель: {user.first_name or '—'}"
    try:
        attachment_count = _count_attachments(appeal)
    except Exception:
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

    # Всегда send_new. Старая карточка остаётся в чате как audit-trail.
    try:
        sent = await bot.send_message(
            chat_id=cfg.admin_group_id,
            text=text,
            attachments=[kb],
        )
    except Exception:
        log.exception(
            "admin_card.render: send_message failed for appeal #%s", appeal.id
        )
        return None

    new_mid = extract_message_id(sent)
    if new_mid:
        # Обновляем last_admin_card_mid — точка stale-detection и
        # точка свайп-reply. Для первой публикации (finalize) — также
        # admin_message_id (используется для reply-link при relay).
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

    Устойчиво к detached-state: appeal.messages может бросить
    MissingGreenlet вне сессии. На failure считаем только scalar
    appeal.attachments — лучше unter-count, чем не доставить карточку.
    """
    try:
        from aemr_bot.services.admin_relay import _collect_all_user_attachments

        return len(_collect_all_user_attachments(appeal))
    except Exception:
        log.debug(
            "admin_card._count_attachments: lazy-load fail for appeal #%s, "
            "fallback to attachments-only",
            appeal.id,
            exc_info=False,
        )
        return len(getattr(appeal, "attachments", None) or [])
